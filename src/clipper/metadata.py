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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
DISCLOSURE_HASHTAGS = frozenset({"#ad", "#ads", "#sponsored", "#paid", "#partner", "#promo"})
"""Tags that read as a paid-partnership disclosure: only a campaign may add them (as campaign
tags at scheduling), never the model or a variant."""
LINK_OR_MENTION = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+"
    r"|\b[\w-]+\s*(?:\.|\bdot\b)\s*(?:com|net|org|io|ly|gg|me|co|xyz|link|ph|tv|app|shop)\b"
    r"|(?<![\w@])@\w+"
)
"""A link, a bare domain (also "name dot com") or an @mention: the transcript is untrusted,
so text like this in a post is flagged for the reviewer and in pre-flight."""
HASHTAG = re.compile(r"^#\w{1,50}$")
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


PLATFORM_LIMITS: Mapping[str, PlatformLimits] = {
    "tiktok": PlatformLimits(
        title_max=100, caption_max=2200, post_hashtags_max=10, min_seconds=3, max_seconds=600
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


def _fold(tag: str) -> str:
    """Compatibility-folded and case-folded, so fullwidth `#ｆｙｐ` matches `#fyp`."""
    return unicodedata.normalize("NFKC", tag).casefold()


def is_generic(tag: str) -> bool:
    return _fold(tag) in GENERIC_HASHTAGS


def is_disclosure(tag: str) -> bool:
    return _fold(tag) in DISCLOSURE_HASHTAGS


def links_or_mentions(text: str) -> list[str]:
    """Links, domains and @mentions in `text`, in order."""
    return [m.group(0) for m in LINK_OR_MENTION.finditer(text)]


def clean_hashtags(tags: Iterable[object], limit: int = _VARIANT_HASHTAGS_MAX) -> list[str]:
    """Lenient: malformed, generic and repeated tags are dropped, the rest cut to `limit`."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = normalize_hashtag(raw)
        key = _fold(tag)
        usable = HASHTAG.fullmatch(tag) and not is_generic(tag) and not is_disclosure(tag)
        if usable and key not in seen:
            seen.add(key)
            out.append(tag)
    return out[:limit]


def truncate_words(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters at a word boundary (a hard cut when the
    last space is in the first half)."""
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    space = cut.rfind(" ")
    head = cut[:space] if space > limit // 2 else text[:limit]
    return head.rstrip()


class Variant(BaseModel):
    """One platform's title, caption (without the hashtag line) and hashtags.

    Strict: generic, malformed, repeated or more than 5 hashtags are errors. Validate with
    `context={"platform": ...}` to apply that platform's title and caption limits; without
    it the loosest limits apply."""

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
        if len({_fold(t) for t in tags}) != len(tags):
            raise ValueError("duplicate hashtags")
        return tags

    @model_validator(mode="after")
    def _platform_limits(self, info: ValidationInfo) -> Variant:
        platform = (info.context or {}).get("platform")
        limits = _limits(platform) if platform else None
        title_max = limits.title_max if limits else _TITLE_MAX
        caption_max = limits.caption_max if limits else _CAPTION_MAX
        where = f"{platform} " if platform else ""
        if len(self.title) > title_max:
            raise ValueError(f"{where}title is longer than {title_max} characters")
        # The posted caption is the body plus a blank line and the hashtag line.
        room = caption_room(platform, self.hashtags) if platform else caption_max
        if len(self.caption) > room:
            raise ValueError(
                f"{where}caption is longer than {room} characters"
                f" ({caption_max} with the hashtag line)"
            )
        if platform == "youtube" and _ANGLE.search(self.title + self.caption):
            raise ValueError("youtube title and description cannot contain < or >")
        return self


def _hashtag_line(tags: Sequence[str]) -> str:
    return " ".join(tags)


def caption_room(platform: str, tags: Sequence[str]) -> int:
    """Characters left for the caption body once the hashtag line is appended."""
    line = _hashtag_line(tags)
    return _limits(platform).caption_max - (len(line) + 2 if line else 0)


def _fit(platform: str, title: str, caption: str, tags: Sequence[object]) -> Variant:
    limits = _limits(platform)
    hashtags = clean_hashtags(tags, limits.hashtags_max)
    title, caption = strip_control(title), clean_caption(caption)
    if platform == "youtube":
        title, caption = _ANGLE.sub("", title), _ANGLE.sub("", caption)
    return Variant.model_validate(
        {
            "title": truncate_words(title, limits.title_max),
            "caption": truncate_words(caption, caption_room(platform, hashtags)),
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
    caption body is cut at a word boundary so the caption with its hashtag line fits the
    limit. A malformed campaign tag is a ValueError (a config error, never dropped)."""
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
    body = truncate_words(variant.caption, max(0, caption_room(platform, hashtags)))
    line = _hashtag_line(hashtags)
    caption = "\n\n".join(part for part in (body, line) if part)
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
    found = links_or_mentions(f"{metadata.title}\n{metadata.caption}")
    if found:
        problems.append(f"{platform}: links or mentions to check: {', '.join(found)}")
    if len(metadata.title) > limits.title_max:
        problems.append(f"{platform}: title is longer than {limits.title_max} characters")
    if len(metadata.caption) > limits.caption_max:
        problems.append(f"{platform}: caption is longer than {limits.caption_max} characters")
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
