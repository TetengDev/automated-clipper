"""Slot picking and scheduling approved clips onto accounts (CLP-92).

`pick_slot` is pure: given an account, the times of its existing posts and `now`, it
returns the earliest UTC time that is inside one of the account's local posting windows,
at least `min_gap_minutes` from every other post of the account, and on a local day that
still has room under `posts_per_day`, the warm-up ramp and the platform's daily API cap.
It fills today's windows first, then the following days, up to `MAX_DAYS_AHEAD`.

`schedule_for_clip` creates one `scheduled` post per eligible account when a clip is
approved (the review app's approve route calls it).
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clipper import clips
from clipper.clips import ClipStatus
from clipper.db import transaction
from clipper.posting import repo
from clipper.posting.models import Post

logger = logging.getLogger(__name__)

# Per account per day. CLP-91 (`clipper.accounts`) owns the canonical copy; this local
# duplicate goes away when that story merges.
PLATFORM_DAILY_CAPS: dict[str, int] = {
    "tiktok": 15,
    "facebook": 30,
    "instagram": 50,
    "youtube": 100,
}
MAX_DAYS_AHEAD = 7
"""Today plus this many days are searched for a free slot."""

SCHEDULABLE = (ClipStatus.APPROVED, ClipStatus.EXPORTED)
_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
_MIDNIGHT = time(0, 0)


class AccountLike(Protocol):
    """A posting account; CLP-91's accounts config implements it."""

    id: str
    platform: str  # "tiktok" | "instagram" | "youtube" | "facebook"
    creators: list[str]  # creator names from creators.yaml
    timezone: str  # IANA name
    posts_per_day: int
    min_gap_minutes: int
    windows: list[str]  # "HH:MM-HH:MM" local time, non-overlapping
    warmup_start: date | None
    warmup_days: int  # default 14
    enabled: bool


def parse_windows(windows: Sequence[str]) -> list[tuple[time, time]]:
    """`"HH:MM-HH:MM"` strings as (start, end) local times, sorted by start.

    The end is exclusive; `"22:00-00:00"` runs to midnight. A window may not wrap past
    midnight or be empty.
    """
    parsed = []
    for text in windows:
        match = _WINDOW.match(text.strip())
        if not match:
            raise ValueError(f"invalid posting window {text!r}; expected HH:MM-HH:MM")
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        start, end = time(h1, m1), time(h2, m2)
        if end != _MIDNIGHT and end <= start:
            raise ValueError(f"invalid posting window {text!r}: end must be after start")
        parsed.append((start, end))
    return sorted(parsed)


def warmup_allowance(account: AccountLike, day: date) -> int:
    """Posts allowed on `day` by the warm-up ramp: none before `warmup_start`, then
    `max(1, ceil(posts_per_day * (d + 1) / warmup_days))` on day d, capped at
    `posts_per_day`. Without `warmup_start` there is no ramp."""
    full = max(0, account.posts_per_day)
    if account.warmup_start is None or account.warmup_days <= 0:
        return full
    elapsed = (day - account.warmup_start).days
    if elapsed < 0:
        return 0
    if elapsed >= account.warmup_days:
        return full
    return min(full, max(1, math.ceil(full * (elapsed + 1) / account.warmup_days)))


def daily_limit(account: AccountLike, day: date) -> int:
    """The most posts the account may have on its local `day`."""
    cap = PLATFORM_DAILY_CAPS.get(account.platform, account.posts_per_day)
    return max(0, min(account.posts_per_day, warmup_allowance(account, day), cap))


def _ceil_minute(moment: datetime) -> datetime:
    floored = moment.replace(second=0, microsecond=0)
    return floored if floored == moment else floored + timedelta(minutes=1)


def _first_free(
    start: datetime, end: datetime, taken: Sequence[datetime], gap: timedelta
) -> datetime | None:
    candidate = start
    while candidate < end:
        clashes = [t for t in taken if abs(t - candidate) < gap]
        if not clashes:
            return candidate
        candidate = _ceil_minute(max(clashes) + gap)
    return None


def pick_slot(account: AccountLike, existing: Sequence[datetime], now: datetime) -> datetime | None:
    """The next free slot (aware UTC) for `account`, or None within `MAX_DAYS_AHEAD`.

    `existing` holds the times of the account's posts that count (everything but
    cancelled). Raises ValueError for a naive `now` or a bad window, and
    ZoneInfoNotFoundError for an unknown timezone.
    """
    if now.tzinfo is None:
        raise ValueError("naive datetime; pass an aware (UTC) datetime")
    tz = ZoneInfo(account.timezone)
    windows = parse_windows(account.windows)
    gap = timedelta(minutes=max(0, account.min_gap_minutes))
    taken = sorted(t.astimezone(UTC) for t in existing)
    per_day: dict[date, int] = {}
    for t in taken:
        local_day = t.astimezone(tz).date()
        per_day[local_day] = per_day.get(local_day, 0) + 1
    earliest = _ceil_minute(now.astimezone(UTC))
    today = now.astimezone(tz).date()
    for offset in range(MAX_DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if per_day.get(day, 0) >= daily_limit(account, day):
            continue
        for start, end in windows:
            opens = datetime.combine(day, start, tz).astimezone(UTC)
            end_day = day + timedelta(days=1) if end == _MIDNIGHT else day
            closes = datetime.combine(end_day, end, tz).astimezone(UTC)
            slot = _first_free(max(opens, earliest), closes, taken, gap)
            if slot is not None:
                return slot
    return None


def schedule_for_clip(
    conn: sqlite3.Connection,
    clip_id: int,
    accounts: Sequence[AccountLike],
    now: datetime,
    *,
    sensitive: bool = False,
) -> list[Post]:
    """Create one `scheduled` post per enabled account whose creators include the clip's.

    Returns the posts created; an approved clip already scheduled on an account is left as
    it is, so calling this again is safe. Nothing is scheduled for a sensitive clip (its
    stored flag, or `sensitive=True` from the creator's rule), a clip that is not approved
    or exported, or one without a rendered video. An account is skipped when the same file
    is already scheduled or posted on its platform, or when it has no free slot.
    """
    clip = clips.get_clip(conn, clip_id)
    if clip is None or clip.status not in SCHEDULABLE:
        return []
    if sensitive or clip.current.sensitive:
        logger.info("clip %d is sensitive; not scheduled for posting", clip_id)
        return []
    video_path = clip.current.video_path
    if not video_path:
        logger.warning("clip %d has no rendered video; not scheduled for posting", clip_id)
        return []
    eligible = sorted(
        (a for a in accounts if a.enabled and clip.creator in a.creators), key=lambda a: a.id
    )
    created: list[Post] = []
    with transaction(conn):
        for account in eligible:
            if repo.has_post(conn, clip_id, account.id):
                continue
            if repo.file_on_platform(conn, account.platform, video_path):
                logger.info(
                    "clip %d: file already scheduled on %s; skipped account %s",
                    clip_id,
                    account.platform,
                    account.id,
                )
                continue
            try:
                slot = pick_slot(account, repo.account_post_times(conn, account.id), now)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                logger.error("account %s: invalid schedule config: %s", account.id, exc)
                continue
            if slot is None:
                logger.warning(
                    "clip %d: no slot for account %s in the next %d days",
                    clip_id,
                    account.id,
                    MAX_DAYS_AHEAD,
                )
                continue
            created.append(
                repo.insert_post(
                    conn,
                    clip_id=clip_id,
                    clip_version=clip.current_version,
                    account_id=account.id,
                    platform=account.platform,
                    scheduled_at=slot,
                    video_path=video_path,
                    now=now,
                )
            )
    for post in created:
        logger.info(
            "clip %d scheduled on %s (%s) at %s",
            clip_id,
            post.account_id,
            post.platform,
            post.scheduled_at.isoformat(),
        )
    return created
