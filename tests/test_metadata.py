"""Per-platform post metadata (CLP-96): variants and their limits, campaign tags, disclosure
flags from the EDL, and the scheduling pre-flight. Everything here is pure."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import ValidationError

from clipper.edl import has_ai_elements, parse_edl
from clipper.metadata import (
    GENERIC_HASHTAGS,
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
    fingerprint,
    links_or_mentions,
    preflight,
    truncate_words,
)

EDL = {"source": {"path": "a.mp4", "in_s": 10, "out_s": 45}}
LONG_TITLE = "MrBeast gives away a private island " + "and then some more " * 10
LONG_CAPTION = "Karl finds the key in the last box. " * 200  # 7200 chars


def variant(**kw: Any) -> Variant:
    base = {"title": "Title", "caption": "Caption", "hashtags": ["#mrbeast", "#island", "#money"]}
    return Variant(**{**base, **kw})


def variants(**kw: Any) -> dict[str, Variant]:
    return {p: variant(**kw) for p in PLATFORMS}


def ai_edl() -> Any:
    return parse_edl({**EDL, "hook": {"text": "Narrated", "ai_generated": True}})


# --- limits and the Variant model -----------------------------------------------------------


def test_platforms_and_limits_cover_the_four_targets():
    assert PLATFORMS == ("tiktok", "instagram", "youtube", "facebook")
    assert set(PLATFORM_LIMITS) == set(PLATFORMS)
    limits = PLATFORM_LIMITS
    assert limits["youtube"].title_max == 100
    assert limits["youtube"].caption_max == 5000
    for p in ("tiktok", "instagram", "facebook"):
        assert limits[p].caption_max == 2200
    assert all(lim.hashtags_min == 3 and lim.hashtags_max == 5 for lim in limits.values())
    assert limits["instagram"].post_hashtags_max == 5
    seconds = {p: (lim.min_seconds, lim.max_seconds) for p, lim in limits.items()}
    assert seconds["tiktok"] == (3, 600)
    assert seconds["instagram"] == (3, 180)
    assert seconds["youtube"][1] == 180
    assert seconds["facebook"] == (3, 90)


@pytest.mark.parametrize("tag", sorted(GENERIC_HASHTAGS))
def test_variant_rejects_generic_hashtags(tag):
    assert {"#fyp", "#foryou", "#viral", "#trending"} <= GENERIC_HASHTAGS
    with pytest.raises(ValidationError, match="generic"):
        variant(hashtags=["#mrbeast", tag.upper(), "#island"])


def test_variant_normalizes_and_bounds_hashtags():
    v = variant(hashtags=["mrbeast", "#Island", "#money"])
    assert v.hashtags == ["#mrbeast", "#Island", "#money"]
    with pytest.raises(ValidationError, match="at most 5"):
        variant(hashtags=[f"#tag{i}" for i in range(6)])
    with pytest.raises(ValidationError):
        variant(hashtags=["#bad tag", "#a", "#b"])
    with pytest.raises(ValidationError, match="duplicate"):
        variant(hashtags=["#island", "#Island", "#a"])


def test_variant_strips_control_characters_but_keeps_caption_lines():
    v = variant(title="Big\x07 moment\n", caption="Line one\x00\nLine‮ two")
    assert v.title == "Big moment"
    assert v.caption == "Line one\nLine two"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_variant_validates_per_platform_limits(platform):
    limits = PLATFORM_LIMITS[platform]
    ok = {"title": "t" * limits.title_max, "caption": "c" * limits.caption_max}
    Variant.model_validate({**ok, "hashtags": []}, context={"platform": platform})
    with pytest.raises(ValidationError, match="title"):
        Variant.model_validate(
            {**ok, "title": "t" * (limits.title_max + 1), "hashtags": []},
            context={"platform": platform},
        )
    with pytest.raises(ValidationError, match="caption"):
        Variant.model_validate(
            {**ok, "caption": "c" * (limits.caption_max + 1), "hashtags": []},
            context={"platform": platform},
        )


def test_truncate_words_cuts_at_a_word_boundary():
    assert truncate_words("short", 10) == "short"
    assert truncate_words("one two three four", 12) == "one two"
    assert truncate_words("x" * 30, 10) == "x" * 10  # no space: hard cut
    assert len(truncate_words(LONG_TITLE, 100)) <= 100
    assert not truncate_words(LONG_TITLE, 100).endswith(" ")


# --- derive_variants (backward compatibility) ------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_derived_variants_stay_within_each_platform_limit(platform):
    tags = ["#mrbeast", "#fyp", "#island", "viral", "#money", "#karl", "#chandler", "#extra"]
    derived = derive_variants(LONG_TITLE, LONG_CAPTION, tags)
    assert set(derived) == set(PLATFORMS)
    v = derived[platform]
    limits = PLATFORM_LIMITS[platform]
    assert 0 < len(v.title) <= limits.title_max
    assert v.title.startswith("MrBeast gives away")
    assert 0 < len(v.caption) <= limits.caption_max
    assert v.caption.startswith("Karl finds the key")
    assert v.hashtags == ["#mrbeast", "#island", "#money", "#karl", "#chandler"]
    Variant.model_validate(v.model_dump(), context={"platform": platform})


def test_derived_instagram_never_has_more_than_5_hashtags():
    derived = derive_variants("T", "C", [f"#tag{i}" for i in range(12)])
    assert len(derived["instagram"].hashtags) == 5


def test_derive_handles_missing_text():
    derived = derive_variants(None, None, ())
    assert all(v == Variant(title="", caption="", hashtags=[]) for v in derived.values())


def test_coerce_keeps_valid_model_variants_and_repairs_the_rest():
    raw = {
        "tiktok": {"title": "TT", "caption": "tt cap", "hashtags": ["#a1", "#b1", "#c1"]},
        "instagram": {
            "title": "IG",
            "caption": "ig cap",
            "hashtags": ["#fyp", *[f"#ig{i}" for i in range(7)]],
        },
        "youtube": {"title": "Y" * 300, "caption": "yt", "hashtags": ["#y1", "#y2", "#y3"]},
        "facebook": "not an object",
    }
    out = coerce_variants(raw, "Title", "Caption", ["#mrbeast", "#island", "#money"])
    assert out["tiktok"] == Variant(title="TT", caption="tt cap", hashtags=["#a1", "#b1", "#c1"])
    assert out["instagram"].hashtags == [f"#ig{i}" for i in range(5)]
    assert len(out["youtube"].title) == 100
    assert (
        out["facebook"]
        == derive_variants("Title", "Caption", ["#mrbeast", "#island", "#money"])["facebook"]
    )
    assert coerce_variants(None, "T", "C", ["#a"]) == derive_variants("T", "C", ["#a"])
    assert coerce_variants("[]", "T", "C", ["#a"]) == derive_variants("T", "C", ["#a"])


# --- build_post_metadata ---------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_campaign_tags_are_appended_and_land_in_the_caption(platform):
    meta = build_post_metadata(variants(), platform, campaign_tags=["#mrbeastpartner", "vyro"])
    assert isinstance(meta, PostMetadata)
    assert meta.hashtags[-2:] == ["#mrbeastpartner", "#vyro"]
    assert meta.caption.endswith("\n\n" + " ".join(meta.hashtags))
    assert meta.caption.startswith("Caption")
    assert meta.title == "Title"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_campaign_means_no_campaign_tags(platform):
    meta = build_post_metadata(variants(), platform)
    assert meta.hashtags == ["#mrbeast", "#island", "#money"]
    assert "#mrbeastpartner" not in meta.caption


def test_campaign_tags_are_deduplicated_against_the_model_tags():
    meta = build_post_metadata(variants(), "tiktok", campaign_tags=["#MrBeast", "#mrbeastpartner"])
    assert meta.hashtags == ["#island", "#money", "#MrBeast", "#mrbeastpartner"]


def test_instagram_drops_the_models_last_tags_never_the_campaign_tags():
    five = ["#a1", "#b1", "#c1", "#d1", "#e1"]
    meta = build_post_metadata(
        variants(hashtags=five), "instagram", campaign_tags=["#mrbeast", "#mrbeastpartner"]
    )
    assert meta.hashtags == ["#a1", "#b1", "#c1", "#mrbeast", "#mrbeastpartner"]
    tiktok = build_post_metadata(
        variants(hashtags=five), "tiktok", campaign_tags=["#mrbeast", "#mrbeastpartner"]
    )
    assert tiktok.hashtags == [*five, "#mrbeast", "#mrbeastpartner"]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_final_caption_stays_within_the_limit_with_the_hashtag_line(platform):
    limit = PLATFORM_LIMITS[platform].caption_max
    long = variants(caption=truncate_words(LONG_CAPTION, limit))
    meta = build_post_metadata(long, platform, campaign_tags=["#mrbeastpartner"])
    assert len(meta.caption) <= limit
    assert meta.caption.endswith("#mrbeastpartner")


def test_post_metadata_is_frozen_with_the_shared_fields():
    names = [f.name for f in dataclasses.fields(PostMetadata)]
    assert names == ["title", "caption", "hashtags", "flags"]
    meta = build_post_metadata(variants(), "tiktok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.title = "x"  # type: ignore[misc]


DISCLOSURE = {
    "tiktok": "is_aigc",
    "instagram": "is_ai_generated",
    "facebook": "is_ai_generated",
    "youtube": "contains_synthetic_media",
}
PAID = {
    "tiktok": "brand_content_toggle",
    "instagram": "paid_partnership",
    "facebook": "paid_partnership",
    "youtube": "paid_partnership",
}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_disclosure_flag_is_set_only_when_the_edl_has_an_ai_element(platform):
    flag = DISCLOSURE[platform]
    assert build_post_metadata(variants(), platform, edl=ai_edl()).flags[flag] is True
    assert build_post_metadata(variants(), platform, edl=parse_edl(EDL)).flags[flag] is False
    assert build_post_metadata(variants(), platform).flags[flag] is False


@pytest.mark.parametrize("platform", PLATFORMS)
def test_paid_partnership_flag_follows_the_campaign(platform):
    flag = PAID[platform]
    assert build_post_metadata(variants(), platform, paid_partnership=True).flags[flag] is True
    assert build_post_metadata(variants(), platform).flags[flag] is False


def test_unknown_platform_is_rejected():
    with pytest.raises(ValueError, match="unknown platform"):
        build_post_metadata(variants(), "myspace")


# --- EDL AI elements ---------------------------------------------------------------------------


def test_edl_ai_elements_default_off_and_are_detected():
    assert has_ai_elements(parse_edl(EDL)) is False
    assert has_ai_elements(parse_edl({**EDL, "hook": {"text": "Hi"}})) is False
    assert has_ai_elements(ai_edl()) is True
    generated_broll = parse_edl(
        {
            **EDL,
            "segments": [
                {"in_s": 10, "out_s": 20},
                {"in_s": 30, "out_s": 45, "ai_generated": True},
            ],
        }
    )
    assert has_ai_elements(generated_broll) is True


# --- preflight -----------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_preflight_passes_a_good_35_second_post(platform):
    meta = build_post_metadata(variants(), platform)
    assert preflight(meta, platform, 35.0) == []


@pytest.mark.parametrize(
    ("platform", "seconds", "ok"),
    [
        ("tiktok", 2.0, False),
        ("tiktok", 600.0, True),
        ("tiktok", 601.0, False),
        ("instagram", 180.0, True),
        ("instagram", 181.0, False),
        ("youtube", 180.0, True),
        ("youtube", 181.0, False),
        ("facebook", 90.0, True),
        ("facebook", 91.0, False),
        ("facebook", 2.5, False),
    ],
)
def test_preflight_checks_clip_length_per_platform(platform, seconds, ok):
    problems = preflight(build_post_metadata(variants(), platform), platform, seconds)
    assert (problems == []) is ok
    if not ok:
        assert any("length" in p for p in problems)


def test_preflight_flags_hashtag_count_and_text_lengths():
    too_many = PostMetadata(
        title="t" * 101,
        caption="c" * 2201,
        hashtags=[f"#t{i}" for i in range(6)],
        flags={},
    )
    problems = preflight(too_many, "instagram", 35.0)
    assert any("hashtags" in p and "at most 5" in p for p in problems)
    assert any("title" in p for p in problems)
    assert any("caption" in p for p in problems)
    few = PostMetadata(title="T", caption="C", hashtags=["#a"], flags={})
    assert any("at least 3" in p for p in preflight(few, "tiktok", 35.0))
    untitled = PostMetadata(title="", caption="C", hashtags=["#a", "#b", "#c"], flags={})
    assert any("title" in p for p in preflight(untitled, "youtube", 35.0))


def test_preflight_flags_generic_hashtags():
    meta = PostMetadata(title="T", caption="C", hashtags=["#a", "#b", "#FYP"], flags={})
    assert any("#FYP" in p for p in preflight(meta, "tiktok", 35.0))


def test_preflight_applies_the_campaign_brief():
    brief = Brief(
        name="vyro",
        required_tags=["#mrbeastpartner"],
        forbidden_keywords=["scam", "Feastables"],
        min_seconds=40,
        paid_partnership=True,
    )
    meta = build_post_metadata(variants(caption="Is this a SCAM?"), "tiktok")
    problems = preflight(meta, "tiktok", 35.0, brief)
    assert any("#mrbeastpartner" in p for p in problems)
    assert any("scam" in p.lower() for p in problems)
    assert any("40" in p for p in problems)
    assert any("paid partnership" in p for p in problems)

    good = build_post_metadata(
        variants(), "tiktok", campaign_tags=brief.required_tags, paid_partnership=True
    )
    assert preflight(good, "tiktok", 45.0, brief) == []


def test_preflight_forbidden_keywords_match_whole_words_only():
    brief = Brief(name="b", forbidden_keywords=["ass"])
    meta = build_post_metadata(variants(caption="A massive class"), "tiktok")
    assert preflight(meta, "tiktok", 35.0, brief) == []


def test_preflight_rejects_unknown_platform():
    with pytest.raises(ValueError, match="unknown platform"):
        preflight(build_post_metadata(variants(), "tiktok"), "vine", 35.0)


# --- review fixes (CLP-96 review) ------------------------------------------------------------


def test_malformed_campaign_tag_is_an_error_not_dropped():
    with pytest.raises(ValueError, match="invalid campaign tag"):
        build_post_metadata(variants(), "tiktok", campaign_tags=["#brand-name"])


def test_campaign_over_the_instagram_cap_keeps_every_tag_and_preflight_blocks():
    campaign = [f"#c{i}" for i in range(6)]
    meta = build_post_metadata(variants(), "instagram", campaign_tags=campaign)
    assert meta.hashtags == campaign
    assert any("at most 5" in p for p in preflight(meta, "instagram", 35.0))


@pytest.mark.parametrize("tag", ["#\uff46\uff59\uff50", "#FyP", "#ad", "#Sponsored", "#partner"])
def test_lookalike_generic_and_disclosure_tags_are_rejected(tag):
    with pytest.raises(ValidationError):
        variant(hashtags=["#mrbeast", tag, "#island"])
    assert clean_hashtags(["#mrbeast", tag]) == ["#mrbeast"]


def test_campaign_may_add_a_partner_tag():
    meta = build_post_metadata(variants(), "tiktok", campaign_tags=["#ad"])
    assert meta.hashtags[-1] == "#ad"


def test_youtube_text_never_holds_angle_brackets():
    derived = derive_variants("Kai <3 chair", "a > b", ["#kai"])
    assert derived["youtube"].title == "Kai 3 chair" and "<" not in derived["youtube"].caption
    assert derived["tiktok"].title == "Kai <3 chair"
    with pytest.raises(ValidationError, match="< or >"):
        Variant.model_validate(
            {"title": "<b>", "caption": "", "hashtags": []}, context={"platform": "youtube"}
        )
    bad = PostMetadata(title="T <x>", caption="C", hashtags=["#a", "#b", "#c"], flags={})
    assert any("< or >" in p for p in preflight(bad, "youtube", 35.0))


@pytest.mark.parametrize(
    "text",
    ["visit https://x.example/win", "go to www.prize.io", "claim-prize dot com", "tag @someone"],
)
def test_preflight_flags_links_and_mentions(text):
    meta = build_post_metadata(variants(caption=text), "tiktok")
    assert any("links or mentions" in p for p in preflight(meta, "tiktok", 35.0))
    assert links_or_mentions(text)


def test_preflight_ignores_plain_text_and_emails_are_not_mentions():
    assert links_or_mentions("Karl wins $10,000. Then Chandler... wow") == []


def test_caption_room_includes_the_hashtag_line():
    tags = ["#a1", "#b1", "#c1"]
    room = caption_room("tiktok", tags)
    assert room == 2200 - len("\n\n#a1 #b1 #c1")
    Variant.model_validate(
        {"title": "", "caption": "c" * room, "hashtags": tags}, context={"platform": "tiktok"}
    )
    with pytest.raises(ValidationError, match="with the hashtag line"):
        Variant.model_validate(
            {"title": "", "caption": "c" * (room + 1), "hashtags": tags},
            context={"platform": "tiktok"},
        )


def test_coerce_keeps_a_valid_stored_entry_exactly():
    stored = {"title": "T", "caption": "word " * 5 + "end", "hashtags": ["#a1", "#b1", "#c1"]}
    out = coerce_variants({"tiktok": stored}, "x", "y", [])
    assert out["tiktok"].model_dump() == stored


def test_fingerprint_changes_with_the_text():
    a = derive_variants("T", "C", ["#a1"])
    b = {**a, "tiktok": variant(title="Other")}
    assert fingerprint(a) == fingerprint(derive_variants("T", "C", ["#a1"]))
    assert fingerprint(a) != fingerprint(b)
