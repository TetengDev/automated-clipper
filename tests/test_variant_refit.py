"""Stored variants re-fitted on load are reported, and a variant holds no inline
disclosure-like tag (CLP-118)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from clipper.metadata import (
    PLATFORMS,
    Variant,
    caption_room,
    coerce_variants,
    coerce_variants_with_notes,
    derive_variants,
)

TAGS = ["#mrbeast", "#island", "#money"]
EMOJI = "\U0001f525"  # one code point, two UTF-16 units


def entry(**kw: Any) -> dict[str, Any]:
    return {"title": "Title", "caption": "Caption", "hashtags": TAGS, **kw}


def stored(**per_platform: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p: per_platform.get(p, entry()) for p in PLATFORMS}


# --- Variant rejects inline disclosure-like tags ------------------------------------------------


@pytest.mark.parametrize("field", ["title", "caption"])
@pytest.mark.parametrize(
    "text",
    [
        "great clip #paidpartnership",
        "great clip #Sponsored_2024",
        "great clip #аd",  # Cyrillic a: a lookalike of #ad
        "see x.co/#ad",  # after a bare domain a # starts a tag
    ],
)
def test_variant_rejects_an_inline_disclosure_like_tag(field, text):
    with pytest.raises(ValidationError, match="disclosure"):
        Variant(**entry(**{field: text}))


def test_variant_keeps_a_url_with_a_disclosure_like_fragment_whole():
    """A `#` inside a URL is judged by the link rules: the URL is never cut, and the
    pre-flight holds the post (CLP-112)."""
    text = "see https://x.co/#ad now"
    variant = Variant(**entry(title=text, caption=text))
    assert (variant.title, variant.caption) == (text, text)


def test_variant_still_strips_generic_inline_tags():
    assert Variant(**entry(caption="great clip #fyp")).caption == "great clip"


def test_fit_strips_inline_disclosure_like_tags():
    out = derive_variants("great clip #paidpartnership", "x #ad y\n#Sponsored z", TAGS)
    for p in PLATFORMS:
        assert (out[p].title, out[p].caption) == ("great clip", "x y\nz")


@pytest.mark.parametrize(
    "raw",
    [
        # A hard cut of a title with no space ends mid-tag: #adventure becomes #ad.
        stored(**{p: entry(title="a" * 97 + "#adventure") for p in PLATFORMS}),
        stored(**{p: entry(caption="b" * 2190 + "#adventure") for p in PLATFORMS}),
        # Taking out < and > joins #a<d into #ad.
        stored(youtube=entry(title="clip #a<d", caption="#sponso<red")),
        stored(
            **{
                p: entry(caption="great clip #paidpartnership", hashtags=["#collab"])
                for p in PLATFORMS
            }
        ),
        stored(tiktok=entry(caption=EMOJI * 1200)),
    ],
)
def test_coerce_variants_never_raises_on_stored_data(raw):
    out = coerce_variants(raw, None, None, [])
    for p in PLATFORMS:
        Variant.model_validate(out[p].model_dump(), context={"platform": p})


# --- the re-fit notes ---------------------------------------------------------------------------


def test_a_valid_stored_entry_has_no_note():
    variants, notes = coerce_variants_with_notes(stored(), None, None, [])
    assert notes == {}
    assert variants == coerce_variants(stored(), None, None, [])


def test_a_derived_entry_has_no_note():
    """Clips from before CLP-96 have no variants: deriving them is not an adjustment."""
    _, notes = coerce_variants_with_notes(None, "x" * 300, "Caption", ["#fyp", *TAGS])
    assert notes == {}


def test_a_tiktok_caption_over_the_limit_is_noted_in_its_units():
    raw = stored(tiktok=entry(caption=EMOJI * 1200))
    variants, notes = coerce_variants_with_notes(raw, None, None, [])
    room = caption_room("tiktok", TAGS)
    assert notes == {"tiktok": [f"cut the caption to fit {room:,} UTF-16 units"]}
    assert variants["tiktok"].caption == EMOJI * (room // 2)


def test_a_long_title_is_noted_in_characters():
    _, notes = coerce_variants_with_notes(
        stored(facebook=entry(title="word " * 30)), None, None, []
    )
    assert notes == {"facebook": ["cut the title to fit 100 characters"]}


def test_dropped_hashtags_are_noted():
    raw = stored(instagram=entry(hashtags=["#kai", "#collab", "#sponsoredpost", "#KAI"]))
    variants, notes = coerce_variants_with_notes(raw, None, None, [])
    assert variants["instagram"].hashtags == ["#kai"]
    assert notes == {"instagram": ["dropped hashtags #collab #sponsoredpost #KAI"]}


def test_an_inline_disclosure_tag_removed_from_the_text_is_noted():
    raw = stored(youtube=entry(title="clip #Sponsored", caption="great clip #paidpartnership"))
    variants, notes = coerce_variants_with_notes(raw, None, None, [])
    assert (variants["youtube"].title, variants["youtube"].caption) == ("clip", "great clip")
    assert notes == {
        "youtube": [
            "removed #Sponsored from the title",
            "removed #paidpartnership from the caption",
        ]
    }


def test_angle_brackets_removed_on_youtube_are_noted():
    raw = stored(youtube=entry(title="a <b> c"))
    variants, notes = coerce_variants_with_notes(raw, None, None, [])
    assert variants["youtube"].title == "a b c"
    assert notes == {"youtube": ["removed < and > from the title"]}
