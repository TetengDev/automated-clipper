"""Post metadata hardening: TikTok lengths in UTF-16 units (CLP-102)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from clipper.metadata import (
    PLATFORM_LIMITS,
    PLATFORMS,
    PostMetadata,
    Variant,
    build_post_metadata,
    caption_room,
    coerce_variants,
    derive_variants,
    preflight,
    truncate_words,
    utf16_len,
)

TAGS = ["#mrbeast", "#island", "#money"]
EMOJI = "\U0001f525"  # four bytes in UTF-8, two UTF-16 units


def variant(**kw: Any) -> Variant:
    base = {"title": "Title", "caption": "Caption", "hashtags": TAGS}
    return Variant(**{**base, **kw})


def variants(**kw: Any) -> dict[str, Variant]:
    return {p: variant(**kw) for p in PLATFORMS}


def post(caption: str, title: str = "T") -> PostMetadata:
    return PostMetadata(title=title, caption=caption, hashtags=TAGS, flags={})


# --- CLP-102: TikTok counts UTF-16 units -------------------------------------------------------


def test_utf16_len_counts_astral_characters_twice():
    assert utf16_len("abc") == 3
    assert utf16_len(EMOJI) == 2
    assert utf16_len("é" + EMOJI * 3) == 7
    assert utf16_len("") == 0


def test_tiktok_measures_in_utf16_units_and_the_others_in_code_points():
    assert PLATFORM_LIMITS["tiktok"].length(EMOJI * 2) == 4
    for p in ("instagram", "youtube", "facebook"):
        assert PLATFORM_LIMITS[p].length(EMOJI * 2) == 2


def test_a_tiktok_caption_of_1100_emoji_fails_preflight():
    """AC: 1,100 four-byte emoji are 1,100 code points but 2,200 UTF-16 units; with the
    hashtag line that is over TikTok's 2,200."""
    meta = build_post_metadata(
        {"tiktok": Variant.model_construct(title="T", caption=EMOJI * 1100, hashtags=TAGS)},
        "tiktok",
    )
    problems = preflight(meta, "tiktok", 35.0)
    assert any("caption is longer" in p for p in problems)
    # A bare 1,100-emoji caption (no hashtag line) is exactly 2,200 units: still allowed.
    assert not any("caption is longer" in p for p in preflight(post(EMOJI * 1100), "tiktok", 35.0))
    assert any("caption is longer" in p for p in preflight(post(EMOJI * 1101), "tiktok", 35.0))
    # The same text is fine where the platform counts code points.
    assert not any(
        "caption is longer" in p for p in preflight(post(EMOJI * 1101), "instagram", 35.0)
    )


def test_tiktok_title_is_measured_in_utf16_units():
    limit = PLATFORM_LIMITS["tiktok"].title_max
    problems = preflight(post("C", title=EMOJI * (limit // 2 + 1)), "tiktok", 35.0)
    assert any("title is longer" in p for p in problems)


def test_tiktok_caption_room_counts_the_hashtag_line_in_utf16_units():
    astral = ["#\U00020000ab", "#b1", "#c1"]  # a CJK Extension B letter is \w and astral
    assert caption_room("tiktok", astral) == 2200 - utf16_len("\n\n" + " ".join(astral))
    assert caption_room("instagram", astral) == 2200 - len("\n\n" + " ".join(astral))


def test_an_edited_tiktok_caption_over_2200_utf16_units_is_rejected():
    room = caption_room("tiktok", TAGS)
    fits = EMOJI * (room // 2)
    Variant.model_validate(
        {"title": "T", "caption": fits, "hashtags": TAGS}, context={"platform": "tiktok"}
    )
    with pytest.raises(ValidationError, match="tiktok caption"):
        Variant.model_validate(
            {"title": "T", "caption": fits + EMOJI, "hashtags": TAGS},
            context={"platform": "tiktok"},
        )
    # Instagram counts code points: the same caption passes there.
    Variant.model_validate(
        {"title": "T", "caption": fits + EMOJI, "hashtags": TAGS},
        context={"platform": "instagram"},
    )


def test_an_emoji_tiktok_title_over_the_limit_is_rejected():
    with pytest.raises(ValidationError, match="tiktok title"):
        Variant.model_validate(
            {"title": EMOJI * 51, "caption": "", "hashtags": []}, context={"platform": "tiktok"}
        )


def test_truncate_words_measures_with_the_given_length():
    assert utf16_len(truncate_words(EMOJI * 30, 10, utf16_len)) == 10
    assert truncate_words(EMOJI * 30, 10) == EMOJI * 10
    words = " ".join([EMOJI * 3] * 10)  # each word is 6 units, 7 with its space
    cut = truncate_words(words, 19, utf16_len)
    assert cut == " ".join([EMOJI * 3] * 2) and utf16_len(cut) <= 19
    assert truncate_words(EMOJI * 5, 10, utf16_len) == EMOJI * 5
    # An odd limit never leaves half an emoji (Python strings cannot hold one anyway).
    assert utf16_len(truncate_words(EMOJI * 30, 11, utf16_len)) == 10


@pytest.mark.parametrize("caption", [EMOJI * 3000, ("Karl " + EMOJI * 4 + " ") * 400])
def test_derived_tiktok_captions_never_exceed_2200_utf16_units(caption):
    tiktok = derive_variants("T", caption, TAGS)["tiktok"]
    meta = build_post_metadata({"tiktok": tiktok}, "tiktok")
    assert 0 < utf16_len(meta.caption) <= 2200
    assert not any("caption is longer" in p for p in preflight(meta, "tiktok", 35.0))
    Variant.model_validate(tiktok.model_dump(), context={"platform": "tiktok"})


def test_coerce_repairs_a_stored_tiktok_caption_that_is_too_long_in_utf16_units():
    stored = {"title": "T", "caption": EMOJI * 1500, "hashtags": TAGS}
    out = coerce_variants({"tiktok": stored}, "x", "y", [])
    assert utf16_len(out["tiktok"].caption) <= caption_room("tiktok", TAGS)
    assert out["tiktok"].caption.startswith(EMOJI)
