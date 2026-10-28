"""Post states, the Post row model, and the publisher contract (CLP-92).

Platform publishers (CLP-93/94/95) implement `Publisher`; per-platform metadata variants
(CLP-96) will build `PostMetadata`. Until then it comes from the clip version.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from clipper.jobs.models import from_iso

MAX_ATTEMPTS = 3
"""Failed publish attempts before a post is `post_failed` (and alerts)."""


class PostStatus(StrEnum):
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    POSTED = "posted"
    POST_FAILED = "post_failed"
    CANCELLED = "cancelled"


POST_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    # scheduled -> scheduled is a reschedule.
    PostStatus.SCHEDULED: frozenset(
        {PostStatus.UPLOADING, PostStatus.CANCELLED, PostStatus.SCHEDULED}
    ),
    # uploading -> scheduled is a retry after a failed attempt (with backoff).
    PostStatus.UPLOADING: frozenset(
        {PostStatus.POSTED, PostStatus.SCHEDULED, PostStatus.POST_FAILED}
    ),
    PostStatus.POST_FAILED: frozenset({PostStatus.SCHEDULED, PostStatus.CANCELLED}),
    PostStatus.POSTED: frozenset(),
    PostStatus.CANCELLED: frozenset(),
}

OPEN_STATUSES = (PostStatus.SCHEDULED, PostStatus.UPLOADING, PostStatus.POST_FAILED)
"""What `clipper posts list` shows by default."""


class InvalidPostTransition(Exception):
    """The post is not in a state that allows the requested change."""

    def __init__(self, post_id: int, current: str, target: PostStatus) -> None:
        super().__init__(f"post {post_id}: cannot move from {current} to {target}")
        self.post_id = post_id
        self.current = current


class PostConflict(Exception):
    """The post changed under us (another worker or a reviewer); the result is dropped."""


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

    @property
    def safe_url(self) -> str | None:
        """The post's URL if it is plain https (it comes from a platform API), else None,
        so a template never links a `javascript:` or other odd scheme."""
        if not self.url:
            return None
        parts = urlsplit(self.url)
        return self.url if parts.scheme == "https" and parts.netloc else None

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
        )


@dataclass(frozen=True)
class PostMetadata:
    """What a publisher posts with the video. CLP-96 builds it from per-platform variants."""

    title: str
    caption: str
    hashtags: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    external_id: str
    url: str | None = None


@runtime_checkable
class Publisher(Protocol):
    """A platform publisher (CLP-93/94/95), registered per platform in the posting stage.

    It may also implement `lookup(idempotency_key: str) -> PublishResult | None`: the
    posting stage calls it before retrying a post a crashed worker left `uploading`, so
    an upload that did go through is recorded instead of posted twice. Without `lookup`
    such a post becomes `post_failed` ("uploading state unresolved") for a human to check.
    """

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult: ...
