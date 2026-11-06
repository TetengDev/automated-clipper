"""Slot picking and scheduling approved clips onto accounts (CLP-92).

`pick_slot` is pure: given an account, the times of its existing posts and `now`, it
returns the earliest UTC time that is inside one of the account's local posting windows,
at least `min_gap_minutes` from the account's other posts that local day (the gap does
not carry over midnight), and on a local day that still has room under `posts_per_day`,
the warm-up ramp and the platform's daily API cap.
It fills today's windows first, then the following days, up to `MAX_DAYS_AHEAD`.

`schedule_for_clip` creates one `scheduled` post per eligible account when a clip is
approved (the review app's approve route calls it). Each post's metadata is built there
from the clip's platform variant plus, for a campaign account, the campaign's tags
(CLP-96), checked against the campaign's brief (CLP-97, `clipper.campaigns`) and stored on
the post. A post that breaks the brief is `held` with the reason instead of scheduled;
`recheck_held` schedules held posts that pass after the config was fixed.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clipper import clips
from clipper.campaigns import (
    Campaign,
    PostingConfig,
    campaign_for,
    campaign_preflight,
    render_edl,
)
from clipper.clips import ClipDetail, ClipStatus
from clipper.config import ConfigError
from clipper.db import transaction
from clipper.metadata import build_post_metadata
from clipper.pipeline.common import strip_control
from clipper.posting import repo
from clipper.posting.models import Post, PostConflict, PostMetadata, PostStatus

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
    campaign: str | None  # campaign id from campaigns.yaml (CLP-97), or None


def parse_windows(windows: Sequence[str]) -> list[tuple[time, time]]:
    """`"HH:MM-HH:MM"` strings as (start, end) local times, sorted by start.

    The end minute is inside the window (`"14:00-17:00"` accepts 17:00, as in CLP-91);
    `"22:00-00:00"` runs up to, but not including, midnight. A window may not wrap past
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
    start: datetime,
    end: datetime,
    taken: Sequence[datetime],
    gap: timedelta,
    *,
    end_inclusive: bool,
) -> datetime | None:
    candidate = start
    while candidate < end or (end_inclusive and candidate == end):
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
        # The minimum gap applies within one local day only (CLP-91's rule).
        day_taken = [t for t in taken if t.astimezone(tz).date() == day]
        for start, end in windows:
            opens = datetime.combine(day, start, tz).astimezone(UTC)
            end_day = day + timedelta(days=1) if end == _MIDNIGHT else day
            closes = datetime.combine(end_day, end, tz).astimezone(UTC)
            # The end minute is inside the window (CLP-91's rule), except a midnight end,
            # which already belongs to the next local day.
            slot = _first_free(
                max(opens, earliest),
                closes,
                day_taken,
                gap,
                end_inclusive=end != _MIDNIGHT,
            )
            if slot is not None:
                return slot
    return None


@dataclass(frozen=True)
class _Plan:
    """What one account would post for a clip, and why it may not (if it may not)."""

    campaign_id: str | None
    metadata: PostMetadata
    problems: list[str]

    @property
    def reason(self) -> str | None:
        return "; ".join(self.problems) if self.problems else None


def _plan(
    clip: ClipDetail,
    account: AccountLike,
    campaigns: Mapping[str, Campaign],
    media_root: Path | None,
    when: datetime,
) -> _Plan:
    campaign_id, campaign = campaign_for(account, clip.creator, campaigns)
    edl = render_edl(media_root, clip.current.video_path) if media_root else None
    built = build_post_metadata(
        clip.current.variants,
        account.platform,
        campaign.required_hashtags if campaign else (),
        edl=edl,
        paid_partnership=campaign.paid_partnership if campaign else False,
    )
    metadata = PostMetadata(**asdict(built))
    if campaign_id is None:
        return _Plan(None, metadata, [])
    if campaign is None:
        problem = f"account {account.id}: unknown campaign {campaign_id!r}; fix accounts.yaml"
        return _Plan(campaign_id, metadata, [problem])
    problems = campaign_preflight(
        campaign, built, account.platform, clip.current.duration_s, edl, when
    )
    return _Plan(campaign_id, metadata, problems)


def _log_text(text: str | None) -> str:
    """Hold reasons quote clip text (LLM output over an untrusted transcript)."""
    return strip_control(text or "")


def schedule_for_clip(
    conn: sqlite3.Connection,
    clip_id: int,
    accounts: Sequence[AccountLike],
    now: datetime,
    *,
    sensitive: bool = False,
    campaigns: Mapping[str, Campaign] | None = None,
    media_root: Path | None = None,
) -> list[Post]:
    """Create one post per enabled account whose creators include the clip's.

    Returns the posts created; an approved clip already scheduled on an account is left as
    it is, so calling this again is safe. Nothing is scheduled for a sensitive clip (its
    stored flag, or `sensitive=True` from the creator's rule), a clip that is not approved
    or exported, or one without a rendered video. An account is skipped when the same file
    is already scheduled, posted or held on its platform, or when it has no free slot.

    A post for a campaign account (`campaigns` by id) whose clip or metadata breaks the
    brief is created `held` with the reasons in `error`, with or without a free slot. A
    held post takes no slot (its `scheduled_at` is only the time it was held) but keeps
    its file reserved on the platform, so it can be released later. `media_root` locates
    the render sidecar, which says whether the clip has AI elements; without it the AI
    status is unknown, which holds the post under a brief that forbids AI content.
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
    briefs = campaigns or {}
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
            plan = _plan(clip, account, briefs, media_root, slot or now)
            if slot is None and plan.reason is None:
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
                    # A held post has no slot: this is only when it was held.
                    scheduled_at=slot if slot is not None and plan.reason is None else now,
                    video_path=video_path,
                    now=now,
                    campaign_id=plan.campaign_id,
                    metadata=plan.metadata,
                    held=plan.reason,
                )
            )
    for post in created:
        if post.status == PostStatus.HELD:
            logger.warning(
                "clip %d held on %s (%s): %s",
                clip_id,
                post.account_id,
                post.platform,
                _log_text(post.error),
            )
            continue
        logger.info(
            "clip %d scheduled on %s (%s) at %s",
            clip_id,
            post.account_id,
            post.platform,
            post.scheduled_at.isoformat(),
        )
    return created


@dataclass(frozen=True)
class RecheckResult:
    released: list[Post] = field(default_factory=list)
    still_held: list[Post] = field(default_factory=list)


SensitiveRule = Callable[[str], bool]
"""Whether a creator's clips are always sensitive (CLP-87), by creator name."""


def _ineligible(
    post: Post,
    clip: ClipDetail | None,
    account: AccountLike | None,
    is_sensitive: SensitiveRule,
) -> str | None:
    """Why a held post cannot be released whatever the brief says, or None. The same
    rules as `schedule_for_clip`."""
    if account is None:
        return f"account {post.account_id} is no longer configured"
    if not account.enabled:
        return f"account {account.id} is disabled"
    if clip is None or clip.status not in SCHEDULABLE:
        return f"clip {post.clip_id} is no longer approved"
    if clip.creator not in account.creators:
        return f"account {account.id} no longer posts {clip.creator}"
    if clip.current.sensitive or is_sensitive(clip.creator):
        return f"clip {clip.id} is sensitive and is never auto-posted"
    if not clip.current.video_path:
        return f"clip {clip.id} has no rendered video"
    return None


def recheck_held(
    conn: sqlite3.Connection,
    accounts: Sequence[AccountLike],
    campaigns: Mapping[str, Campaign],
    now: datetime,
    *,
    media_root: Path | None = None,
    is_sensitive: SensitiveRule,
) -> RecheckResult:
    """Run the pre-flight again on every held post with the current config and the clip's
    current version (a re-edited, re-approved clip is checked as it is now). A post that
    passes is scheduled in its account's next free slot; one that does not keeps its hold
    with the current reason. Each post is checked and released in one transaction, so the
    slot cannot be taken meanwhile; a post cancelled meanwhile is skipped.
    `is_sensitive` is required: a sensitive creator's clip is never released."""
    by_id = {a.id: a for a in accounts}
    released: list[Post] = []
    still: list[Post] = []
    for held in repo.held_posts(conn):
        try:
            with transaction(conn):
                post = repo.get_post(conn, held.id)
                if post is None or post.status != PostStatus.HELD:
                    continue  # cancelled (or released) meanwhile
                outcome = _recheck_one(conn, post, by_id, campaigns, now, media_root, is_sensitive)
        except (PostConflict, ValueError, ZoneInfoNotFoundError) as exc:
            logger.error("post %d: recheck failed: %s", held.id, _log_text(str(exc)))
            still.append(held)
            continue
        (released if outcome.status == PostStatus.SCHEDULED else still).append(outcome)
    return RecheckResult(released=released, still_held=still)


def _recheck_one(
    conn: sqlite3.Connection,
    post: Post,
    accounts: Mapping[str, AccountLike],
    campaigns: Mapping[str, Campaign],
    now: datetime,
    media_root: Path | None,
    is_sensitive: SensitiveRule,
) -> Post:
    account = accounts.get(post.account_id)
    clip = clips.get_clip(conn, post.clip_id)
    blocked = _ineligible(post, clip, account, is_sensitive)
    if blocked is not None:
        return repo.update_hold(conn, post, blocked, now)
    assert clip is not None and account is not None and clip.current.video_path
    video_path = clip.current.video_path
    if video_path != post.video_path and repo.file_on_platform(conn, post.platform, video_path):
        reason = f"clip {clip.id}'s current file is already scheduled on {post.platform}"
        return repo.update_hold(conn, post, reason, now)
    slot = pick_slot(account, repo.account_post_times(conn, account.id), now)
    plan = _plan(clip, account, campaigns, media_root, slot or now)
    if plan.reason is not None:
        return repo.update_hold(conn, post, plan.reason, now)
    if slot is None:
        reason = f"no free slot for account {account.id} in the next {MAX_DAYS_AHEAD} days"
        return repo.update_hold(conn, post, reason, now)
    return repo.release_held(
        conn,
        post,
        slot,
        now,
        metadata=plan.metadata,
        clip_version=clip.current_version,
        video_path=video_path,
        campaign_id=plan.campaign_id,
    )


def publish_problem(
    conn: sqlite3.Connection,
    post: Post,
    accounts: Mapping[str, AccountLike],
    campaigns: Mapping[str, Campaign],
    now: datetime,
    *,
    media_root: Path | None = None,
) -> str | None:
    """The publish-time pre-flight (CLP-97): why a claimed post must not go out now, or
    None. The config may have changed since scheduling (campaign disabled or ended, brief
    tightened, the account moved to another campaign) and a reschedule may have moved the
    post past the campaign's end, so the brief is checked again with the stored metadata.
    Fails closed: an account no longer configured, or a post whose campaign differs from
    the one its account has now (including one scheduled before CLP-97), is held."""
    account = accounts.get(post.account_id)
    if account is None:
        return f"account {post.account_id} is no longer configured"
    version = clips.get_version(conn, post.clip_id, post.clip_version)
    clip = clips.get_clip(conn, post.clip_id)
    if version is None or clip is None:
        return f"clip {post.clip_id} v{post.clip_version} is missing"
    campaign_id, campaign = campaign_for(account, clip.creator, campaigns)
    if campaign_id != post.campaign_id:
        return (
            f"campaign changed since scheduling ({post.campaign_id or 'none'} ->"
            f" {campaign_id or 'none'}); run `clipper posts recheck` or cancel the post"
        )
    if campaign_id is None:
        return None
    if campaign is None:
        return f"account {account.id}: unknown campaign {campaign_id!r}"
    metadata = repo.metadata_for(conn, post)
    edl = render_edl(media_root, post.video_path) if media_root else None
    problems = campaign_preflight(campaign, metadata, post.platform, version.duration_s, edl, now)
    return "; ".join(problems) if problems else None


def publish_guard(
    conn: sqlite3.Connection, config: PostingConfig, media_root: Path | None
) -> Callable[[Post], str | None]:
    """The posting stage's guard: `publish_problem` with the config as it is on disk now.
    An invalid config holds the post rather than publishing it."""

    def guard(post: Post) -> str | None:
        try:
            accounts, campaigns = config.current()
        except ConfigError as exc:
            first = str(exc).splitlines()[0] if str(exc) else "invalid"
            return f"posting config is invalid ({first}); fix it and run `clipper posts recheck`"
        return publish_problem(
            conn, post, accounts, campaigns, datetime.now(UTC), media_root=media_root
        )

    return guard
