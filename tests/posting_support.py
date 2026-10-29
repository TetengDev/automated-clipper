"""Shared helpers for the posting tests (CLP-92): a fake account and a fake publisher.

`FakeAccount` implements `clipper.posting.scheduler.AccountLike`, the contract with the
accounts config of CLP-91, so these tests do not depend on that story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from clipper.posting.models import Post, PostMetadata, PublishResult


@dataclass
class FakeAccount:
    id: str = "tt_main"
    platform: str = "tiktok"
    creators: list[str] = field(default_factory=lambda: ["MrBeast"])
    timezone: str = "Asia/Manila"
    posts_per_day: int = 3
    min_gap_minutes: int = 120
    windows: list[str] = field(default_factory=lambda: ["09:00-12:00", "18:00-22:00"])
    warmup_start: date | None = None
    warmup_days: int = 14
    enabled: bool = True


@dataclass
class FakePublisher:
    """Records publish calls; fails the first `fail` of them."""

    fail: int = 0
    calls: list[tuple[Post, Path, PostMetadata]] = field(default_factory=list)

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        self.calls.append((post, video_path, metadata))
        if len(self.calls) <= self.fail:
            raise RuntimeError("upload rejected: rate limited")
        return PublishResult(external_id=f"ext-{post.id}", url=f"https://example.test/p/{post.id}")


@dataclass
class LookupPublisher(FakePublisher):
    """A publisher that can say whether an upload with an idempotency key already exists."""

    found: dict[str, PublishResult] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    def lookup(self, idempotency_key: str) -> PublishResult | None:
        self.lookups.append(idempotency_key)
        return self.found.get(idempotency_key)
