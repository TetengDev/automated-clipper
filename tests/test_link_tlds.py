"""Bare domains are flagged only on a real IANA TLD, in any case and script, and warnings
show what was typed (CLP-116)."""

from __future__ import annotations

import re
import unicodedata

import pytest

from clipper import metadata, tld_data
from clipper.metadata import Brief, PostMetadata, links_or_mentions, preflight

TAGS = ["#mrbeast", "#island", "#money"]


@pytest.mark.parametrize(
    "text",
    [
        "Scam.Rocks",  # a mixed-case TLD not on the common list
        "SCAM.ROCKS",
        "buy at scam.xn--p1ai",  # the punycode form of .рф
        "free stuff at shop.Example.Io today",
        "scam\u3002com",  # an ideographic full stop is a dot to IDNA
        "ok\u3002com",
        "name[.]rocks",
        "name d0t Rocks",
        # A deliberately obfuscated dot is no sentence break: any real TLD, any script.
        "name (dot) онлайн",
        "name[.]みんな",
        "name d0t рф",
        # A TLD in another script counts in a URL only (see the CJK sentences below).
        "go to https://сайт.рф now",
        "https://例え.みんな",
        "www.例え.みんな",
        "www.ПРИМЕР.РФ",
    ],
)
def test_a_real_tld_is_flagged_in_any_case(text):
    assert links_or_mentions(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Ordinary sentences: the full stop reads as a dot, and the next word is an IDN TLD.
        "ありがとう。みんな！",
        "我爱你。中国！",
        "新店开业。购物！",
        "感谢。在线",
        "감사합니다.한국!",
        # So a bare domain needs an ASCII TLD (punycode included).
        "go to сайт.рф now",
    ],
)
def test_a_bare_domain_needs_an_ascii_tld(text):
    assert links_or_mentions(text) == [], text


def test_a_glued_capitalised_tld_word_is_flagged_but_not_abbreviations():
    """The cost of catching `Scam.Rocks`: `this.So` reads as a domain (.so is a TLD)."""
    assert links_or_mentions("this.So") == ["this.So"]
    for text in ("e.g. this", "Mr.Smith", "built with Node.js"):
        assert links_or_mentions(text) == [], text


@pytest.mark.parametrize(
    "text",
    [
        "the store. Me and him",  # an ordinary sentence break
        "Karl wins $10,000. Then he leaves",
        "built with Node.js",  # .js is no TLD
        "open index.html",
        "it.Notatld",
        "name[.]notatld",
        "polka dot shop",  # a spoken "dot" stays limited to TLDs that are no everyday word
        "scam dot rocks",
    ],
)
def test_text_without_a_real_tld_is_not_flagged(text):
    assert links_or_mentions(text) == [], text


def test_a_domain_stops_before_a_sentence_glued_on():
    assert links_or_mentions("example.com.Then we left") == ["example.com"]


@pytest.mark.parametrize(
    ("text", "typed"),
    [
        ("visit Example.COM now", "Example.COM"),
        ("visit name．com now", "name．com"),  # a fullwidth dot
        ("visit scam‌.com now", "scam‌.com"),  # an invisible joiner
        ("by @Vyro.Official", "@Vyro.Official"),
    ],
)
def test_warnings_show_the_text_as_typed(text, typed):
    assert links_or_mentions(text) == [typed]
    meta = PostMetadata(title="T", caption=text, hashtags=TAGS, flags={})
    (problem,) = [p for p in preflight(meta, "tiktok", 35.0) if "links or mentions" in p]
    assert problem.endswith(f"links or mentions to check: {typed}")


def test_an_allowed_link_is_still_matched_on_its_normalized_host():
    brief = Brief(name="vyro", allowed_links=["vyro.app"])
    meta = PostMetadata(title="T", caption="get Vyro.App now", hashtags=TAGS, flags={})
    assert not any("links or mentions" in p for p in preflight(meta, "tiktok", 35.0, brief))


def test_the_vendored_tlds_hold_their_invariants():
    tlds = tld_data.TLDS
    assert {"com", "rocks", "io", "рф", "онлайн", "xn--p1ai"} <= tlds
    assert not {"js", "html", "notatld"} & tlds
    for tld in tlds:
        copy = metadata._match_copy(tld)[0]
        assert copy.lower() == tld, tld  # as the link pattern sees it
        assert re.fullmatch(r"[a-z]{2,}|xn--[a-z0-9-]+|[^\W\d_]+", tld), tld
        assert tld.isascii() or all(not unicodedata.category(c).startswith("M") for c in tld)
    assert re.fullmatch(r"20\d{6}\d{2}", tld_data.IANA_VERSION)
