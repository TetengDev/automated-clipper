"""Post states, the Post row model, and the publisher contract (CLP-92).

Platform publishers (CLP-93/94/95) implement `Publisher`. `PostMetadata` is built at
scheduling from the clip's per-platform variant (CLP-96) plus its campaign's tags, and
stored on the post (CLP-97); posts scheduled before that fall back to the clip version.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from clipper.jobs.models import from_iso

MAX_ATTEMPTS = 3
"""Failed publish attempts before a post is `post_failed` (and alerts)."""


_URL_UNSAFE = frozenset("<>`\\\"'")


def plain_https(url: str | None) -> str | None:
    """`url` if it is a plain https URL, else None. No whitespace, control characters or
    markup characters either: the URL is linked in templates and quoted in Discord
    messages and logs, where a newline or `>` could forge text. A backslash, userinfo or
    port is refused too: browsers may resolve those to another host (CLP-94)."""
    if not url or any(c.isspace() or not c.isprintable() or c in _URL_UNSAFE for c in url):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.netloc != parts.hostname:
        return None
    return url


class PostStatus(StrEnum):
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    POSTED = "posted"
    POST_FAILED = "post_failed"
    CANCELLED = "cancelled"
    HELD = "held"
    """Broke its campaign's brief at scheduling (CLP-97); the reason is in `error`. Never
    published: `clipper posts recheck` schedules it once it passes, or it is cancelled."""


POST_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    # scheduled -> scheduled is a reschedule.
    PostStatus.SCHEDULED: frozenset(
        {PostStatus.UPLOADING, PostStatus.CANCELLED, PostStatus.SCHEDULED}
    ),
    # uploading -> scheduled is a retry after a failed attempt (with backoff);
    # uploading -> held is the publish-time campaign pre-flight refusing it (CLP-97).
    PostStatus.UPLOADING: frozenset(
        {PostStatus.POSTED, PostStatus.SCHEDULED, PostStatus.POST_FAILED, PostStatus.HELD}
    ),
    PostStatus.POST_FAILED: frozenset({PostStatus.SCHEDULED, PostStatus.CANCELLED}),
    PostStatus.POSTED: frozenset(),
    PostStatus.CANCELLED: frozenset(),
    # held -> scheduled only through the pre-flight recheck (`scheduler.recheck_held`).
    PostStatus.HELD: frozenset({PostStatus.SCHEDULED, PostStatus.CANCELLED}),
}

OPEN_STATUSES = (
    PostStatus.SCHEDULED,
    PostStatus.UPLOADING,
    PostStatus.POST_FAILED,
    PostStatus.HELD,
)
"""What `clipper posts list` shows by default."""

RESCHEDULE_HORIZON = timedelta(days=366)
RESCHEDULE_PAST_SLACK = timedelta(minutes=1)


def parse_utc(text: str) -> datetime | None:
    """An ISO 8601 / `datetime-local` time as aware UTC (no offset means UTC), or None."""
    try:
        moment = datetime.fromisoformat(text.strip())
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def reschedule_problem(when: datetime, now: datetime) -> str | None:
    """Why `when` is not an acceptable new post time, or None. Shared by the review app
    and the CLI."""
    if when < now - RESCHEDULE_PAST_SLACK:
        return "Pick a time in the future."
    if when > now + RESCHEDULE_HORIZON:
        return "Pick a time within a year."
    return None


class InvalidPostTransition(Exception):
    """The post is not in a state that allows the requested change."""

    def __init__(self, post_id: int, current: str, target: PostStatus) -> None:
        super().__init__(f"post {post_id}: cannot move from {current} to {target}")
        self.post_id = post_id
        self.current = current


class PostConflict(Exception):
    """The post changed under us (another worker or a reviewer); the result is dropped."""


class NoFreeSlot(Exception):
    """The account has no free slot within the scheduler's horizon (CLP-106)."""


def can_transition(current: str, target: PostStatus) -> bool:
    try:
        return target in POST_TRANSITIONS[PostStatus(current)]
    except (KeyError, ValueError):
        return False


@dataclass(frozen=True)
class Post:
    id: int
    clip_id: int
    clip_version: int
    account_id: str
    platform: str
    status: PostStatus
    scheduled_at: datetime
    attempts: int
    idempotency_key: str
    video_path: str
    posted_at: datetime | None = None
    external_id: str | None = None
    url: str | None = None
    error: str | None = None
    worker_id: str | None = None
    claimed_at: datetime | None = None
    created_at: str = ""
    campaign_id: str | None = None
    publish_at: datetime | None = None
    """The slot the post goes live at (CLP-115); `scheduled_at` is when it is next tried,
    which a deferral moves and this does not. None for a held post."""

    @property
    def slot(self) -> datetime:
        """When the post is meant to go live: `publish_at`, else `scheduled_at`."""
        return self.publish_at or self.scheduled_at

    @property
    def next_try(self) -> datetime | None:
        """When a scheduled post is next tried, if not at its slot (a deferral); else None."""
        if self.status == PostStatus.SCHEDULED and self.scheduled_at != self.slot:
            return self.scheduled_at
        return None

    @property
    def safe_url(self) -> str | None:
        """The post's URL if it is plain https (it comes from a platform API), else None,
        so a template never links a `javascript:` or other odd scheme."""
        return plain_https(self.url)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Post:
        scheduled = from_iso(row["scheduled_at"])
        assert scheduled is not None  # NOT NULL column
        return cls(
            id=row["id"],
            clip_id=row["clip_id"],
            clip_version=row["clip_version"],
            account_id=row["account_id"],
            platform=row["platform"],
            status=PostStatus(row["status"]),
            scheduled_at=scheduled,
            attempts=row["attempts"],
            idempotency_key=row["idempotency_key"],
            video_path=row["video_path"],
            posted_at=from_iso(row["posted_at"]),
            external_id=row["external_id"],
            url=row["url"],
            error=row["error"],
            worker_id=row["worker_id"],
            claimed_at=from_iso(row["claimed_at"]),
            created_at=row["created_at"],
            campaign_id=row["campaign_id"],
            publish_at=from_iso(row["publish_at"]),
        )


@dataclass(frozen=True)
class PostMetadata:
    """What a publisher posts with the video: built from the clip's per-platform variant
    (CLP-96) with the campaign's tags at scheduling (CLP-97), and stored on the post."""

    title: str
    caption: str
    hashtags: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    external_id: str
    url: str | None = None


class PublishRejected(Exception):  # noqa: N818 - an outcome, not an error
    """Raised by `Publisher.publish` when the post can never go out as it is: a disclosure
    the platform API cannot set, a caption over the platform's limits (CLP-94). The
    posting stage marks it `post_failed` and alerts at once, without retrying."""


class PublishDeferred(Exception):  # noqa: N818 - an outcome, not an error
    """Raised by `Publisher.publish` when the post cannot go out now for a reason that is
    not the post's fault: a platform quota or upload limit, or a paused account (CLP-93).

    The posting stage moves the post back to `scheduled` at `until` without counting an
    attempt, and sends `alert` (when set) to a human. `reason` is stored as the post's
    error and must not contain secrets.
    """

    def __init__(self, reason: str, until: datetime, *, alert: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.until = until
        self.alert = alert


@runtime_checkable
class Publisher(Protocol):
    """A platform publisher (CLP-93/94/95), registered per platform in the posting stage.

    `publish` must give up (time out) well within `stage.STALE_UPLOAD` (30 minutes) and
    should dedupe on `post.idempotency_key` where the platform allows it.

    It may also implement `lookup(idempotency_key: str) -> PublishResult | None`: the
    posting stage calls it before re-publishing a post after a failed attempt and for a
    post a crashed worker left `uploading`, so an upload that did go through is recorded
    instead of posted twice. Without `lookup` a crashed upload becomes `post_failed`
    ("uploading state unresolved") for a human to check.

    `publish` may raise `PublishDeferred` to reschedule the post without counting an
    attempt (quota, upload limit, paused account).

    Also optional, read with getattr (CLP-94):

    * `publish` may raise `PublishRejected`: the post is `post_failed` at once (no retry).
    * `lead_time: timedelta`: a post's first attempt is claimed this long before its
      slot, for a platform that publishes natively at a given time (Facebook's
      `scheduled_publish_time`, YouTube's `publishAt`; CLP-115). Retries and deferrals
      still wait for their own time, and never come before the slot.
    * `use_platform_refs(refs: PlatformRefs)` (CLP-122): the stage hands the publisher a
      store for the platform's id of an upload in progress, kept on the post, so `lookup`
      still finds the upload after a worker restart.
    """

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult: ...


@dataclass(frozen=True)
class StoredRef:
    """A post's stored upload reference (CLP-122) with the post's own account: the
    account, never one named in the reference, decides whose token checks it."""

    account_id: str
    ref: str


class PlatformRefs(Protocol):
    """Where a publisher keeps the platform's id for an upload in progress (CLP-122), by
    the post's idempotency key; the posting stage backs it with `posts.platform_ref`.

    `save` raises when the post is no longer this worker's upload (or the write fails):
    the publisher then stops before the next platform call. `load` returns the last
    saved value with the post's account, or None."""

    def save(self, idempotency_key: str, ref: str) -> None: ...

    def load(self, idempotency_key: str) -> StoredRef | None: ...
