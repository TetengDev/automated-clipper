"""Format tags of a Short (CLP-154): what a Short is made of, so the insights page and the
weekly summary can compare each Short with the channel's own median. Tags are stored by
YouTube video id; a pipeline post is tagged at approval by post id, before it has one.

Eight keys, each always present (`unknown` when nothing says):
- hook_type, structure, topic, palette: from `commentary_stories.format`, written by the
  scripting (CLP-146) and rendering (CLP-147) stages; lower-cased.
- length_bucket: the rendered length (`duration_s`, else end_s - start_s).
- clip_share: the share of the runtime that is streamer clips, in 10-point buckets, from
  the story's excerpts. A creator's clip is all clip (90-100%).
- streamers: the Twitch logins of the story's excerpts, comma-separated. A creator's clip
  names the creator (lower case, as the owner's tags are).
- publish_hour: the hour, in the account's timezone, at which the post goes public
  (`scheduled_at`, which the YouTube publisher sends as `publishAt`).

A hand-uploaded Short gets length_bucket and publish_hour from the Data API (duration and
publish time) and `unknown` for the rest, which the owner sets with
`clipper insights tag <video_id> key=value ...`. The owner's values always win.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from clipper.insights import repo
from clipper.pipeline.common import strip_control
from clipper.posting.models import Post
from clipper.urls import COMMENTARY

TAG_KEYS = (
    "hook_type",
    "structure",
    "palette",
    "length_bucket",
    "clip_share",
    "streamers",
    "topic",
    "publish_hour",
)
FORMAT_KEYS = ("hook_type", "structure", "palette", "topic")
DATA_KEYS = ("length_bucket", "publish_hour")
"""The keys `video_defaults` derives from the Data API."""
UNKNOWN = repo.UNKNOWN
MAX_VALUE = 80
_LENGTHS = ((20.0, "<20s"), (40.0, "20-40s"), (60.0, "40-60s"), (90.0, "60-90s"))


def length_bucket(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return UNKNOWN
    for limit, label in _LENGTHS:
        if seconds < limit:
            return label
    return "90s+"


def share_bucket(share: float | None) -> str:
    if share is None or share < 0:
        return UNKNOWN
    low = min(int(share * 10), 9) * 10
    return f"{low}-{low + 10}%"


def publish_hour(when: datetime, timezone: str) -> str:
    return f"{when.astimezone(ZoneInfo(timezone)).hour:02d}"


def clean_value(value: str) -> str:
    """A tag value as stored: no control characters, at most 80 characters, lower case.
    Derived and owner-set values both go through it, so one creator has one spelling."""
    return strip_control(value)[:MAX_VALUE].strip().lower() or UNKNOWN


def derive_tags(conn: sqlite3.Connection, post: Post, timezone: str) -> dict[str, str]:
    tags = dict.fromkeys(TAG_KEYS, UNKNOWN)
    tags["publish_hour"] = publish_hour(post.scheduled_at, timezone)
    clip = repo.post_clip(conn, post)
    if clip is None:
        return tags
    duration = clip.duration_s
    tags["length_bucket"] = length_bucket(duration)
    if clip.platform != COMMENTARY:  # a creator's clip
        tags["streamers"] = clean_value(clip.creator)
        tags["clip_share"] = share_bucket(1.0)
        return tags
    story = conn.execute(
        "SELECT excerpts, format FROM commentary_stories WHERE clip_id = ? AND version = ?",
        (post.clip_id, post.clip_version),
    ).fetchone()
    if story is None:
        return tags
    seconds = 0.0
    ids: list[str] = []
    excerpts = [e for e in repo.loads_json(story["excerpts"], list) or () if isinstance(e, dict)]
    for excerpt in excerpts:
        start, end = excerpt.get("start_s"), excerpt.get("end_s")
        if isinstance(start, int | float) and isinstance(end, int | float) and end > start:
            seconds += end - start
        if isinstance(excerpt.get("clip_id"), str):
            ids.append(excerpt["clip_id"])
    if excerpts and duration and duration > 0:
        tags["clip_share"] = share_bucket(seconds / duration)
    if ids:
        marks = ", ".join("?" * len(ids))
        rows = conn.execute(
            "SELECT DISTINCT broadcaster_login FROM story_candidates"  # noqa: S608 - placeholders
            f" WHERE twitch_clip_id IN ({marks})",
            ids,
        )
        logins = sorted({clean_value(r[0]) for r in rows if r[0]})
        if logins:
            tags["streamers"] = ",".join(logins)[:MAX_VALUE]
    fmt = repo.loads_json(story["format"], dict) or {}
    for key in FORMAT_KEYS:
        value = fmt.get(key)
        if isinstance(value, str):
            tags[key] = clean_value(value)
    return tags


def tag_post(conn: sqlite3.Connection, post: Post, timezone: str) -> dict[str, str]:
    """Derive the post's tags and store them by post id, replacing earlier values. Once
    the post is uploaded they are stored by video id too and only fill keys without a
    real value, so the owner's tags (set by video id) always win."""
    tags = derive_tags(conn, post, timezone)
    vid = post.external_id if repo.VIDEO_ID.fullmatch(post.external_id or "") else None
    repo.set_tags(conn, tags, post_id=post.id, video_id=vid, replace=vid is None)
    return tags


def video_defaults(video: repo.Video, timezone: str) -> dict[str, str]:
    """What the Data API says about any video: its length and publish hour."""
    tags = dict.fromkeys(TAG_KEYS, UNKNOWN)
    tags["length_bucket"] = length_bucket(video.duration_s)
    tags["publish_hour"] = publish_hour(video.published_at, timezone)
    return tags


def needs_defaults(tags: Mapping[str, str]) -> bool:
    """Whether `ensure_video_tags` would change a video with these tags: a key is missing,
    or a key the Data API can tell (length, publish hour) is still `unknown`."""
    missing = set(TAG_KEYS) - set(tags)
    return bool(missing) or any(tags[key] == UNKNOWN for key in DATA_KEYS)


def ensure_video_tags(conn: sqlite3.Connection, video: repo.Video, timezone: str) -> None:
    """Give a video its defaults (a hand upload's) where a key is missing or `unknown`;
    never changes a real value, so the owner's tags stay."""
    repo.set_tags(conn, video_defaults(video, timezone), video_id=video.video_id, replace=False)


def parse_pairs(args: Sequence[str]) -> dict[str, str]:
    """`key=value` words of `clipper insights tag` as cleaned tags."""
    pairs: dict[str, str] = {}
    for word in args:
        key, sep, value = word.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{word!r} is not key=value")
        if key not in TAG_KEYS:
            raise ValueError(f"unknown tag key {key!r}; one of: {', '.join(TAG_KEYS)}")
        if not value.strip():
            raise ValueError(f"empty value for {key!r}")
        pairs[key] = clean_value(value)
    return pairs


def owner_tags(
    conn: sqlite3.Connection, video_id: str, pairs: Mapping[str, str], timezone: str = "UTC"
) -> dict[str, str]:
    """Store the owner's tags for a video (any video id, known to the job or not). Keys
    not supplied keep their value, or become the video's defaults (`unknown` when the job
    has not seen the video)."""
    if not repo.VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"{video_id!r} is not a YouTube video id (11 characters)")
    unknown = sorted(set(pairs) - set(TAG_KEYS))
    if unknown:
        raise ValueError(f"unknown tag key(s): {', '.join(unknown)}")
    if pairs:
        repo.set_tags(
            conn, {k: clean_value(v) for k, v in pairs.items()}, video_id=video_id, replace=True
        )
    video = repo.get_video(conn, video_id)
    defaults = video_defaults(video, timezone) if video else dict.fromkeys(TAG_KEYS, UNKNOWN)
    repo.set_tags(conn, defaults, video_id=video_id, replace=False)
    return repo.tags_for_videos(conn, [video_id]).get(video_id, {})
