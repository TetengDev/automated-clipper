"""Post metadata hardening: TikTok lengths in UTF-16 units (CLP-102) and link, mention and
disclosure-tag detection (CLP-107)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from clipper.metadata import (
    PLATFORM_LIMITS,
    PLATFORMS,
    Brief,
    PostMetadata,
    Variant,
    build_post_metadata,
    caption_room,
    clean_hashtags,
    coerce_variants,
    derive_variants,
    links_or_mentions,
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


# --- CLP-107: links, mentions and disclosure tags -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "claimprize[.]com",
        "claimprize(.)com",
        "claimprize (dot) com",
        "claimprize[dot]com",
        "claimprize d0t com",
        "claimprize．com",  # fullwidth full stop
        "claimprize.info",
        "claim at prize.website now",
        "CLAIMPRIZE.COM",
        "claimprize DOT COM",
        "＠someone",  # fullwidth @
        "visit ｗｗｗ．prize．io",
    ],
)
def test_obfuscated_links_and_mentions_are_flagged(text):
    assert links_or_mentions(text), text
    meta = build_post_metadata(variants(caption=text), "tiktok")
    assert any("links or mentions" in p for p in preflight(meta, "tiktok", 35.0))


@pytest.mark.parametrize(
    "text",
    [
        "the store. Me and my friend",
        "the CEO. Co-founder too",
        "it. TV was on",
        "out. App is great",
        "Wait. Net worth",
        "email me at home",
        "Karl wins $10,000. Then Chandler... wow",
        "it costs 3.5mm and 2.0x",
        "she said wait...what",
        "Mr.Beast is here",
        "connect the dots to win",
        "a． B",  # fullwidth stop then a space: a sentence break
    ],
)
def test_ordinary_sentence_breaks_are_still_not_links(text):
    assert links_or_mentions(text) == []


@pytest.mark.parametrize(
    "tag",
    [
        "#sponsoredpost",
        "#paidpartnership",
        "#adpartner",
        "#gifted",
        "#collab",
        "#SponsoredBy",
        "#ｓｐｏｎｓｏｒｅｄｐｏｓｔ",
        "#giftedby",
        "#paidpromotion",
        "#partnership",
    ],
)
def test_disclosure_like_tags_are_rejected_in_variants(tag):
    with pytest.raises(ValidationError, match="disclosure"):
        variant(hashtags=["#mrbeast", tag, "#island"])
    assert clean_hashtags(["#mrbeast", tag]) == ["#mrbeast"]


@pytest.mark.parametrize("tag", ["#adventure", "#address", "#paidoff", "#partners", "#giftwrap"])
def test_ordinary_tags_that_share_a_prefix_are_kept(tag):
    assert variant(hashtags=["#mrbeast", tag, "#island"]).hashtags[1] == tag


def test_campaign_disclosure_tags_are_unaffected():
    meta = build_post_metadata(
        variants(), "tiktok", campaign_tags=["#sponsoredpost", "#paidpartnership"]
    )
    assert meta.hashtags[-2:] == ["#sponsoredpost", "#paidpartnership"]
    assert preflight(meta, "tiktok", 35.0) == []


def test_a_brief_allowed_mention_passes_preflight():
    brief = Brief(name="vyro", allowed_mentions=["@MrBeast"])
    meta = build_post_metadata(variants(caption="Credit @mrbeast for this"), "tiktok")
    assert preflight(meta, "tiktok", 35.0, brief) == []
    assert any("links or mentions" in p for p in preflight(meta, "tiktok", 35.0))


def test_a_brief_blocks_mentions_and_links_not_on_its_list():
    brief = Brief(name="vyro", allowed_mentions=["mrbeast"], allowed_links=["vyro.app"])
    text = "Credit @mrbeast and @other, get it at https://vyro.app/x or claimprize.com"
    meta = build_post_metadata(variants(caption=text), "tiktok")
    problems = [p for p in preflight(meta, "tiktok", 35.0, brief) if "links or mentions" in p]
    assert len(problems) == 1
    assert "@other" in problems[0] and "claimprize.com" in problems[0]
    assert "@mrbeast" not in problems[0] and "vyro.app" not in problems[0]


@pytest.mark.parametrize(
    "text",
    [
        "https://vyro.app@evil.com/win",
        "https://evil.com/vyro.app",
        "vyro[.]app",
        "vyro.app.evil.com",
        "https://vyro.application.io",
    ],
)
def test_an_allowed_link_does_not_cover_lookalikes(text):
    brief = Brief(name="vyro", allowed_links=["vyro.app"])
    meta = build_post_metadata(variants(caption=text), "tiktok")
    assert any("links or mentions" in p for p in preflight(meta, "tiktok", 35.0, brief)), text


@pytest.mark.parametrize("text", ["https://vyro.app/x", "www.vyro.app", "VYRO.APP", "vyro.app"])
def test_an_allowed_link_passes_in_its_ordinary_forms(text):
    brief = Brief(name="vyro", allowed_links=["https://www.vyro.app/"])
    meta = build_post_metadata(variants(caption=text), "tiktok")
    assert preflight(meta, "tiktok", 35.0, brief) == [], text


def test_brief_allow_lists_reject_blank_entries():
    with pytest.raises(ValidationError):
        Brief(name="b", allowed_mentions=["  "])
    with pytest.raises(ValidationError):
        Brief(name="b", allowed_links=[""])
