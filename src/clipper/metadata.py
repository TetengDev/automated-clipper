"""Per-platform post metadata (CLP-96): title, caption and hashtags per platform, campaign
tags, disclosure flags and the scheduling pre-flight. Pure functions, no I/O.

Selection writes one `Variant` per platform (`tiktok`, `instagram`, `youtube`, `facebook`).
Older replies and clips stored before CLP-96 have one title, caption and hashtag list;
`derive_variants` turns those into variants inside each platform's limits. Model output
and stored JSON go through `coerce_variants`, which repairs what it can (cut at a word
boundary, drop generic or malformed hashtags) and derives the rest, because a paid call
should not be thrown away over one long title.

At scheduling time `build_post_metadata` adds the campaign's tags (never the model's job)
and the disclosure flags, and `preflight` lists what would make a platform reject or a
campaign refuse the post.

`PostMetadata` mirrors the dataclass of the same name in the CLP-92 posting package field
for field; the two are merged into one definition when both land.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from clipper.edl import EDL, has_ai_elements
from clipper.pipeline.common import strip_control

PLATFORMS: tuple[str, ...] = ("tiktok", "instagram", "youtube", "facebook")
GENERIC_HASHTAGS = frozenset({"#fyp", "#foryou", "#foryoupage", "#viral", "#trending", "#fy"})
"""Reach tags that say nothing about the clip; rejected in variants, flagged in pre-flight."""
DISCLOSURE_HASHTAGS = frozenset(
    {
        "#ad",
        "#ads",
        "#paid",
        "#partner",
        "#partners",
        "#promo",
        "#promos",
        "#gifted",
        "#spon",
        "#collab",
        "#affiliate",
        "#pr",
    }
)
DISCLOSURE_PREFIXES = (
    "#spons",  # sponsored, and the misspelt sponser / sponserd
    "#paidpartner",
    "#paidpromo",
    "#paidad",
    "#paidpost",
    "#paidcollab",
    "#paidcontent",
    "#brandpartner",
    "#brandambassador",
    "#branddeal",
    "#ambassador",
    "#affiliatelink",
    "#partnered",
    "#partnership",
    "#partnerwith",
    "#adpartner",
    "#adpost",
    "#adspons",
    "#advert",
    "#promoted",
    "#promocode",
    "#collabwith",
    "#giftedby",
    "#prgift",
)
"""Tags that read as a paid-partnership disclosure, exactly or by prefix (`#sponsoredpost`),
compared without underscores, trailing digits or digit lookalikes (`#sp0nsored_2024`): only
a campaign may add them (as campaign tags at scheduling), never the model or a variant."""
_LOOKALIKE_DIGITS = str.maketrans("013457", "oieast")
_TLDS = "com|net|org|io|ly|gg|me|co|xyz|link|ph|tv|app|shop|info|biz|site|online|store|live|click"
_SPOKEN_TLDS = "com|net|org|io|ly|gg|co|xyz|link|tv|app|info|biz|click"
"""After a spoken "dot": only TLDs that are not everyday words ("polka dot shop")."""
# A name starts a token: never mid-word or right after a hyphen, or a dot that follows a
# word character or hyphen, so each run of text is scanned once (no quadratic backtracking on
# "a-a-a-..." or "a.a.a...").
_NAME = r"(?<![\w-])(?<![\w-]\.)[\w-]+"
LINK_OR_MENTION = re.compile(
    # A URL ends before quotes, brackets and trailing punctuation.
    r"(?i:(?<![\w./:-])(?:https?://|www\.)[\w\-.~:/?#@!$&*+,;=%]*[\w\-/#=~%&+])"
    # A domain: "name.tld" with no space around the dot. Any TLD of 2-24 letters in one
    # case, or a common TLD in any case, so a sentence break ("the store. Me") is not one.
    + rf"|{_NAME}(?:\.[\w-]+)*\.(?:[a-z]{{2,24}}|[A-Z]{{2,24}}|(?i:{_TLDS}))\b"
    # An obfuscated dot: "name[.]com", "name(dot)com", "name [d0t] info", "name . com".
    + rf"|(?i:{_NAME}[ \t]*[\[(][ \t]*(?:\.|dot|d0t)[ \t]*[\])][ \t]*[a-z]{{2,24}}\b)"
    + rf"|(?i:{_NAME}[ \t]+\.[ \t]+(?:{_SPOKEN_TLDS})\b)"
    # Spelled out: "name d0t anything", or "name dot com" with a common TLD.
    + rf"|(?i:{_NAME}[ \t]+d0t[ \t]+[a-z]{{2,24}}\b)"
    + rf"|(?i:{_NAME}[ \t]+dot[ \t]+(?:{_SPOKEN_TLDS})\b)"
    # An IPv4 address.
    + r"|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)"
    # A whole handle: dots and hyphens are part of it ("@vyro.official").
    + r"|(?<![\w@])@[\w.-]*\w"
)
"""A link, a bare or obfuscated domain, an IP or an @mention: the transcript is untrusted, so
text like this in a post is flagged for the reviewer and in pre-flight. Matched on a copy of
the text that is NFKC-normalized, without invisible or combining characters, and with every
dot IDNA treats as one turned into ".", so `scam\u200c．com` is caught too."""
_INVISIBLE = re.compile(
    "[\u034f\u115f\u1160\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
_IDNA_DOTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\ufe12": ".", "\u2024": "."})
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_INLINE_TAG = re.compile(r"#\w+")
_LINK_IN_PATH = re.compile(rf"(?i:www\.|https?:|[\w-]\.(?:{_TLDS})\b)")
"""Another link inside an allowed URL's path; a file name ("download.html") is fine."""
HASHTAG = re.compile(r"^#\w{1,50}$")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, as TikTok counts it: a character outside the Basic
    Multilingual Plane (most emoji) counts as two."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


_ANGLE = re.compile(r"[<>]")
"""The YouTube Data API rejects titles and descriptions containing < or >."""


@dataclass(frozen=True)
class PlatformLimits:
    title_max: int
    caption_max: int
    """The whole posted caption, including the hashtag line."""
    post_hashtags_max: int
    """Hashtags on the post, campaign tags included."""
    min_seconds: float
    max_seconds: float
    hashtags_min: int = 3
    hashtags_max: int = 5
    """What the model may write per variant (3-5, keyword-first)."""
    length: Callable[[str], int] = len
    """How the platform counts `title_max` and `caption_max`: code points unless its
    documentation says otherwise."""
    unit: str = "characters"


PLATFORM_LIMITS: Mapping[str, PlatformLimits] = {
    # TikTok counts its limits in UTF-16 units: an emoji is usually two.
    "tiktok": PlatformLimits(
        title_max=100,
        caption_max=2200,
        post_hashtags_max=10,
        min_seconds=3,
        max_seconds=600,
        length=utf16_len,
        unit="UTF-16 units",
    ),
    "instagram": PlatformLimits(
        title_max=100, caption_max=2200, post_hashtags_max=5, min_seconds=3, max_seconds=180
    ),
    # YouTube ignores every hashtag on a video with more than 15; Shorts are up to 3 minutes.
    "youtube": PlatformLimits(
        title_max=100, caption_max=5000, post_hashtags_max=15, min_seconds=1, max_seconds=180
    ),
    "facebook": PlatformLimits(
        title_max=100, caption_max=2200, post_hashtags_max=10, min_seconds=3, max_seconds=90
    ),
}
_TITLE_MAX = max(lim.title_max for lim in PLATFORM_LIMITS.values())
_CAPTION_MAX = max(lim.caption_max for lim in PLATFORM_LIMITS.values())
_VARIANT_HASHTAGS_MAX = max(lim.hashtags_max for lim in PLATFORM_LIMITS.values())


def _limits(platform: str) -> PlatformLimits:
    try:
        return PLATFORM_LIMITS[platform]
    except KeyError:
        raise ValueError(f"unknown platform: {platform!r}") from None


def clean_caption(text: str) -> str:
    """Control and invisible characters stripped per line; line breaks kept (at most one
    blank line in a row)."""
    lines = [strip_control(line) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def normalize_hashtag(raw: str) -> str:
    tag = strip_control(raw).replace(" ", "")
    return tag if tag.startswith("#") else f"#{tag}"


_NEUTRAL = frozenset("ʻʼʽʾʿ")
"""Apostrophe-like modifier letters written inside words of several scripts: the okina in
#Hawaiʻi, the Ukrainian apostrophe ʼ and the half rings of Arabic transliteration (#Qurʾan).
They belong to no script, and `_fold` drops them, so `#adʼ` is still `#ad`. Every other
modifier letter (prime, colon, stress mark) counts as a script of its own, so it cannot pad
a Latin tag."""
_DROP_NEUTRAL = dict.fromkeys(map(ord, _NEUTRAL))


def _fold(tag: str) -> str:
    """Compatibility-folded and case-folded without the neutral apostrophes, so fullwidth
    `#ｆｙｐ` and `#fypʻ` match `#fyp`."""
    return unicodedata.normalize("NFKC", tag).casefold().translate(_DROP_NEUTRAL)


def is_generic(tag: str) -> bool:
    return _fold(tag) in GENERIC_HASHTAGS


def is_disclosure(tag: str) -> bool:
    key = _fold(tag).replace("_", "").rstrip("0123456789").translate(_LOOKALIKE_DIGITS)
    return key in DISCLOSURE_HASHTAGS or key.startswith(DISCLOSURE_PREFIXES)


def _match_copy(text: str) -> tuple[str, list[int]]:
    """The copy of `text` that links, mentions and inline tags are matched on, and for each
    of its characters the index of the character of `text` it came from."""
    chars: list[str] = []
    origin: list[int] = []
    for i, char in enumerate(text):
        for c in unicodedata.normalize("NFKC", char):
            if _INVISIBLE.match(c) or unicodedata.category(c) in {"Cf", "Mn", "Me"}:
                continue
            chars.append(c.translate(_IDNA_DOTS))
            origin.append(i)
    return "".join(chars), origin


def _match_text(text: str) -> str:
    return _match_copy(text)[0]


def _link_matches(text: str) -> list[tuple[str, str]]:
    """Each link or mention as matched, with the slice of `text` it was matched from."""
    copy, origin = _match_copy(text)
    return [
        (m.group(0), text[origin[m.start()] : origin[m.end() - 1] + 1])
        for m in LINK_OR_MENTION.finditer(copy)
    ]


_SCRIPT_ALIASES = {"CJK": "HAN", "IDEOGRAPHIC": "HAN"}
"""First words of Unicode character names that do not spell the script."""
_KANA_MARKS = frozenset("ー〱〲〳〴〵")
"""The prolonged sound mark ー and the kana repeat marks: part of a tag only next to kana."""
_KANA = frozenset({"HIRAGANA", "KATAKANA"})
_SCRIPT_MIXES = (
    frozenset({"LATIN", "HAN", "HIRAGANA", "KATAKANA"}),
    frozenset({"LATIN", "HAN", "BOPOMOFO"}),
    frozenset({"LATIN", "HAN", "HANGUL"}),
)
"""Script sets that are one writing system (Japanese, Chinese, Korean), as in the Unicode
"highly restrictive" profile (UTS #39)."""


def _scripts(folded: str) -> set[str]:
    """The scripts of the letters in an NFKC-folded tag, read from the first word of each
    letter's Unicode name (LATIN, CYRILLIC, GREEK, ...). Digits, `_` and combining marks
    belong to no script."""
    scripts: set[str] = set()
    kana_marks = False
    for char in folded:
        if char in _NEUTRAL or not unicodedata.category(char).startswith("L"):
            continue
        if char in _KANA_MARKS:
            kana_marks = True
            continue
        word = unicodedata.name(char, "UNKNOWN").split(" ", 1)[0]
        scripts.add(_SCRIPT_ALIASES.get(word, word))
    if kana_marks and not scripts & _KANA:
        scripts.add("KANA MARK")
    return scripts


def _mixed_scripts(tag: str) -> list[str]:
    """The scripts `tag` mixes, sorted; empty when it is one writing system."""
    folded = unicodedata.normalize("NFKC", tag)
    scripts = _scripts(folded)
    if len(scripts) > 1 and not any(scripts <= mix for mix in _SCRIPT_MIXES):
        return sorted(scripts)
    # A Latin tag with a lookalike zero from another script (#pr၀m၀, #sp〇ns〇red). An
    # allowed CJK letter does not excuse it (#sp〇ns〇red一), so #iPhone二〇二四 is rejected too.
    if "LATIN" in scripts and any(
        not c.isascii() and unicodedata.category(c).startswith("N") for c in folded
    ):
        return ["LATIN", "non-ASCII digits"]
    return []


def is_mixed_script(tag: str) -> bool:
    """True when the letters come from more than one script, as in `#fyр` with a Cyrillic
    р, or a Latin tag holds a digit from another script: a mixed-script lookalike of a
    generic, disclosure or campaign tag. Japanese, Chinese and Korean mixed with Latin are
    one writing system each. A lookalike written wholly in one script (all-Cyrillic `#аԁ`)
    is not caught here."""
    return bool(_mixed_scripts(tag))


def links_or_mentions(text: str) -> list[str]:
    """Links, domains, IPs and @mentions in `text`, in order (as matched, normalized)."""
    return [found for found, _ in _link_matches(text)]


def _link_host(link: str) -> str | None:
    """The host a plain link or bare domain points at (folded, without `www.` or a trailing
    dot), or None when it is obfuscated or carries more than a host and a harmless path:
    userinfo, a port, a backslash, or another link or mention inside it."""
    rest = re.sub(r"^https?://", "", _fold(link.strip()))
    if "\\" in rest:
        return None
    try:
        parts = urlsplit(f"//{rest}")
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").removeprefix("www.")
    tail = unquote(f"{parts.path} {parts.query} {parts.fragment}")
    if parts.username is not None or port is not None or not _HOST.fullmatch(host):
        return None
    if "@" in tail or "://" in tail or _LINK_IN_PATH.search(tail):
        return None
    return host


def inline_disclosure_tags(text: str, allowed: Iterable[str] = ()) -> list[str]:
    """Disclosure-like hashtags written into `text` itself, other than `allowed` ones."""
    ok = {_fold(t) for t in allowed}
    return [
        t for t in _INLINE_TAG.findall(_match_text(text)) if is_disclosure(t) and _fold(t) not in ok
    ]


def clean_hashtags(tags: Iterable[object], limit: int = _VARIANT_HASHTAGS_MAX) -> list[str]:
    """Lenient: malformed, generic, mixed-script and repeated tags are dropped, the rest cut
    to `limit`."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = normalize_hashtag(raw)
        key = _fold(tag)
        usable = (
            HASHTAG.fullmatch(tag)
            and not is_generic(tag)
            and not is_disclosure(tag)
            and not is_mixed_script(tag)
        )
        if usable and key not in seen:
            seen.add(key)
            out.append(tag)
    return out[:limit]


def truncate_words(text: str, limit: int, length: Callable[[str], int] = len) -> str:
    """`text` cut to at most `limit` units of `length` (characters by default) at a word
    boundary (a hard cut when the last space is in the first half)."""
    if length(text) <= limit:
        return text
    end = used = 0
    for char in text:
        used += length(char)
        if used > limit:
            break
        end += 1
    # A space right after the longest prefix that fits is a word boundary too.
    space = end if text[end] == " " else text.rfind(" ", 0, end)
    head = text[:space] if space > 0 and length(text[:space]) > limit // 2 else text[:end]
    return head.rstrip()


class Variant(BaseModel):
    """One platform's title, caption (without the hashtag line) and hashtags.

    Strict: generic, malformed, mixed-script, repeated or more than 5 hashtags are errors.
    Validate with `context={"platform": ...}` to apply that platform's title and caption
    limits; without it the loosest limits apply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(max_length=_TITLE_MAX * 4)
    caption: str = Field(max_length=_CAPTION_MAX * 4)
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("title", mode="after")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        return strip_control(value)

    @field_validator("caption", mode="after")
    @classmethod
    def _clean_caption(cls, value: str) -> str:
        return clean_caption(value)

    @field_validator("hashtags", mode="after")
    @classmethod
    def _check_hashtags(cls, value: list[str]) -> list[str]:
        tags = [t if t.startswith("#") else f"#{t}" for t in map(strip_control, value)]
        if len(tags) > _VARIANT_HASHTAGS_MAX:
            raise ValueError(f"at most {_VARIANT_HASHTAGS_MAX} hashtags")
        for tag in tags:
            if not HASHTAG.fullmatch(tag):
                raise ValueError(f"invalid hashtag {tag!r}")
            if is_generic(tag):
                raise ValueError(f"generic hashtag {tag} says nothing about the clip")
            if is_disclosure(tag):
                raise ValueError(f"hashtag {tag} reads as a paid-partnership disclosure")
            if mixed := _mixed_scripts(tag):
                raise ValueError(f"hashtag {tag} mixes scripts ({', '.join(mixed)})")
        if len({_fold(t) for t in tags}) != len(tags):
            raise ValueError("duplicate hashtags")
        return tags

    @model_validator(mode="after")
    def _platform_limits(self, info: ValidationInfo) -> Variant:
        platform = (info.context or {}).get("platform")
        limits = _limits(platform) if platform else None
        title_max = limits.title_max if limits else _TITLE_MAX
        caption_max = limits.caption_max if limits else _CAPTION_MAX
        length, unit = (limits.length, limits.unit) if limits else (len, "characters")
        where = f"{platform} " if platform else ""
        if length(self.title) > title_max:
            raise ValueError(f"{where}title is longer than {title_max} {unit}")
        # The posted caption is the body plus a blank line and the hashtag line.
        room = caption_room(platform, self.hashtags) if platform else caption_max
        if length(self.caption) > room:
            raise ValueError(
                f"{where}caption is longer than {room} {unit} ({caption_max} with the hashtag line)"
            )
        if platform == "youtube" and _ANGLE.search(self.title + self.caption):
            raise ValueError("youtube title and description cannot contain < or >")
        return self


def _hashtag_line(tags: Sequence[str]) -> str:
    return " ".join(tags)


def caption_room(platform: str, tags: Sequence[str]) -> int:
    """Room left for the caption body once the hashtag line is appended, in the platform's
    own units (UTF-16 units on TikTok, which is also what an HTML `maxlength` counts)."""
    limits = _limits(platform)
    line = _hashtag_line(tags)
    return limits.caption_max - (limits.length(line) + 2 if line else 0)


def _fit(platform: str, title: str, caption: str, tags: Sequence[object]) -> Variant:
    limits = _limits(platform)
    hashtags = clean_hashtags(tags, limits.hashtags_max)
    title, caption = strip_control(title), clean_caption(caption)
    if platform == "youtube":
        title, caption = _ANGLE.sub("", title), _ANGLE.sub("", caption)
    return Variant.model_validate(
        {
            "title": truncate_words(title, limits.title_max, limits.length),
            "caption": truncate_words(caption, caption_room(platform, hashtags), limits.length),
            "hashtags": hashtags,
        },
        context={"platform": platform},
    )


def derive_variants(
    title: str | None, caption: str | None, hashtags: Iterable[object]
) -> dict[str, Variant]:
    """Variants for a clip with one title, caption and hashtag list (selected before
    CLP-96, or a reply without `variants`), each inside its platform's limits."""
    tags = list(hashtags)
    return {p: _fit(p, title or "", caption or "", tags) for p in PLATFORMS}


def coerce_variants(
    raw: object, title: str | None, caption: str | None, hashtags: Iterable[object]
) -> dict[str, Variant]:
    """Variants from untrusted JSON (model output, a stored column). An entry that is
    already valid for its platform is kept exactly (a reviewer's saved text reads back
    unchanged); another well-formed entry is cleaned into the limits; anything else is
    derived from the single title, caption and hashtags."""
    entries = raw if isinstance(raw, Mapping) else {}
    tags = list(hashtags)
    out: dict[str, Variant] = {}
    for platform in PLATFORMS:
        entry = entries.get(platform)
        if not (
            isinstance(entry, Mapping)
            and isinstance(entry.get("title"), str)
            and isinstance(entry.get("caption"), str)
            and isinstance(entry.get("hashtags"), list)
        ):
            out[platform] = _fit(platform, title or "", caption or "", tags)
            continue
        try:
            out[platform] = Variant.model_validate(
                {k: entry[k] for k in ("title", "caption", "hashtags")},
                context={"platform": platform},
            )
        except ValidationError:
            out[platform] = _fit(platform, entry["title"], entry["caption"], entry["hashtags"])
    return out


def variants_json(variants: Mapping[str, Variant]) -> dict[str, dict[str, Any]]:
    return {p: variants[p].model_dump() for p in PLATFORMS if p in variants}


def fingerprint(variants: Mapping[str, Variant]) -> str:
    """A short digest of the variants, so an edit made from a stale page is detected."""
    text = json.dumps(variants_json(variants), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- posting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostMetadata:
    """What a post sends: the caption already ends with the hashtag line. Field-for-field
    the same as `PostMetadata` in the CLP-92 posting package."""

    title: str
    caption: str
    hashtags: list[str]
    flags: dict[str, bool]


_DISCLOSURE_FLAG = {
    "tiktok": "is_aigc",
    "instagram": "is_ai_generated",
    "facebook": "is_ai_generated",
    "youtube": "contains_synthetic_media",
}
_PAID_FLAG = {
    "tiktok": "brand_content_toggle",
    "instagram": "paid_partnership",
    "facebook": "paid_partnership",
    "youtube": "paid_partnership",
}


def build_post_metadata(
    variants: Mapping[str, Variant],
    platform: str,
    campaign_tags: Sequence[str] = (),
    edl: EDL | None = None,
    paid_partnership: bool = False,
) -> PostMetadata:
    """The post for one platform. Campaign tags go last and are never dropped; where the
    platform caps hashtags (Instagram: 5) the model's last tags make room. A campaign with
    more tags than the cap still gets all of them, and `preflight` blocks the post. The
    caption is never cut here: when campaign tags push it past the limit, `preflight`
    reports it, so the reviewer's text is shortened by a person, not silently. A malformed
    campaign tag is a ValueError (a config error, never dropped)."""
    limits = _limits(platform)
    variant = variants[platform]
    campaign: list[str] = []
    for raw in campaign_tags:
        tag = normalize_hashtag(raw)
        if not HASHTAG.fullmatch(tag):
            raise ValueError(f"invalid campaign tag {raw!r}")
        if _fold(tag) not in {_fold(t) for t in campaign}:
            campaign.append(tag)
    taken = {_fold(t) for t in campaign}
    own = [t for t in variant.hashtags if _fold(t) not in taken]
    own = own[: max(0, limits.post_hashtags_max - len(campaign))]
    hashtags = [*own, *campaign]
    line = _hashtag_line(hashtags)
    caption = "\n\n".join(part for part in (variant.caption, line) if part)
    ai = edl is not None and has_ai_elements(edl)
    return PostMetadata(
        title=variant.title,
        caption=caption,
        hashtags=hashtags,
        flags={_DISCLOSURE_FLAG[platform]: ai, _PAID_FLAG[platform]: paid_partnership},
    )


class Brief(BaseModel):
    """A campaign's posting rules (CLP-97 loads real briefs from config)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required_tags: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    min_seconds: float | None = None
    paid_partnership: bool = False
    allowed_mentions: list[str] = Field(default_factory=list)
    """@handles the campaign asks for (a creator credit); pre-flight lets these through."""
    allowed_links: list[str] = Field(default_factory=list)
    """Hosts the campaign asks for ("vyro.app", subdomains not included)."""

    @field_validator("allowed_mentions", mode="after")
    @classmethod
    def _check_mentions(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[\w.-]*\w", _handle(v)) for v in value):
            raise ValueError("an allowed mention must be one @handle")
        return value

    @field_validator("allowed_links", mode="after")
    @classmethod
    def _check_links(cls, value: list[str]) -> list[str]:
        for link in value:
            if _link_host(link) is None:
                raise ValueError(f"allowed link {link!r} is not a plain host")
        return value

    def allows(self, found: str) -> bool:
        """Whether a link or mention from `links_or_mentions` is one the brief asks for."""
        if found.startswith("@"):
            return _handle(found) in {_handle(m) for m in self.allowed_mentions}
        host = _link_host(found)
        return host is not None and host in {_link_host(link) for link in self.allowed_links}


def _handle(mention: str) -> str:
    return _fold(mention).strip().removeprefix("@")


def _mentions(text: str, keyword: str) -> bool:
    words = keyword.strip()
    if not words:
        return False
    return re.search(rf"(?<!\w){re.escape(words)}(?!\w)", text, re.IGNORECASE) is not None


def preflight(
    metadata: PostMetadata, platform: str, clip_seconds: float, brief: Brief | None = None
) -> list[str]:
    """Problems that would get the post rejected by the platform or the campaign; empty
    when it is good to schedule."""
    limits = _limits(platform)
    problems: list[str] = []
    if not limits.min_seconds <= clip_seconds <= limits.max_seconds:
        problems.append(
            f"{platform}: clip length {clip_seconds:g} s is outside"
            f" {limits.min_seconds:g}-{limits.max_seconds:g} s"
        )
    if brief and brief.min_seconds is not None and clip_seconds < brief.min_seconds:
        problems.append(
            f"{brief.name}: clip length {clip_seconds:g} s is under the brief's"
            f" {brief.min_seconds:g} s"
        )
    count = len(metadata.hashtags)
    if count > limits.post_hashtags_max:
        problems.append(f"{platform}: {count} hashtags, at most {limits.post_hashtags_max}")
    if count < limits.hashtags_min:
        problems.append(f"{platform}: {count} hashtags, use at least {limits.hashtags_min}")
    generic = [t for t in metadata.hashtags if is_generic(t)]
    if generic:
        problems.append(f"{platform}: generic hashtags {' '.join(generic)}")
    if platform == "youtube" and not metadata.title.strip():
        problems.append("youtube: the title is empty")
    if platform == "youtube" and _ANGLE.search(metadata.title + metadata.caption):
        problems.append("youtube: title or description contains < or >")
    # An allowed match must also read the same as typed (up to NFKC): a combining mark in a
    # host is stripped for matching but would still be posted, to a different IDN host.
    found = [
        f
        for f, typed in _link_matches(f"{metadata.title}\n{metadata.caption}")
        if not (brief and brief.allows(f) and _fold(typed) == _fold(f))
    ]
    if found:
        problems.append(f"{platform}: links or mentions to check: {', '.join(found)}")
    inline = inline_disclosure_tags(f"{metadata.title}\n{metadata.caption}", metadata.hashtags)
    if inline:
        problems.append(f"{platform}: disclosure-like tags in the text: {' '.join(inline)}")
    if limits.length(metadata.title) > limits.title_max:
        problems.append(f"{platform}: title is longer than {limits.title_max} {limits.unit}")
    if limits.length(metadata.caption) > limits.caption_max:
        problems.append(f"{platform}: caption is longer than {limits.caption_max} {limits.unit}")
    if brief:
        present = {t.lower() for t in metadata.hashtags}
        for tag in brief.required_tags:
            wanted = normalize_hashtag(tag)
            if wanted.lower() not in present:
                problems.append(f"{brief.name}: required tag {wanted} is missing")
        text = "\n".join((metadata.title, metadata.caption, " ".join(metadata.hashtags)))
        for keyword in brief.forbidden_keywords:
            if _mentions(text, keyword):
                problems.append(f"{brief.name}: forbidden keyword {keyword!r}")
        if brief.paid_partnership and not metadata.flags.get(_PAID_FLAG[platform], False):
            problems.append(f"{brief.name}: the paid partnership toggle is off")
    return problems
