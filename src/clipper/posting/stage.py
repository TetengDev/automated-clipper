"""Worker stage `posting` (CLP-92): publish due posts through the platform publishers.

Each pass (the worker runs one per poll, see `clipper.jobs.worker.Worker.posting`):

1. Posts left `uploading` for longer than `STALE_UPLOAD` (a worker crashed mid-upload) on
   platforms this worker publishes to are taken over, never re-published. If the
   publisher's `lookup(idempotency_key)` finds the upload, the post is `posted`; in every
   other case (not found, lookup error, no `lookup`) the upload may still be in flight,
   so the post becomes `post_failed` ("uploading state unresolved") and alerts a human.
2. Due `scheduled` posts on platforms with a registered publisher are claimed one at a
   time (atomically moved to `uploading` under this worker's id) and published. A retry
   first asks `lookup` whether the earlier attempt went through. Success stores the
   external id and URL (`posted`). A failure counts an attempt and backs off
   (`now + BACKOFF_BASE * 2**attempts`); the `MAX_ATTEMPTS`th failure is `post_failed`
   and raises an alert. A successful upload whose result cannot be recorded because the
   post changed meanwhile alerts too (a possible duplicate).
3. Due posts on a platform with no publisher stay `scheduled`; that is logged once per
   platform per worker run.

If recording a successful upload fails (DB error), the post stays `uploading` and step 1
resolves it later through `lookup`; it is never re-published blindly.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from clipper.clips import resolve_media_path
from clipper.jobs.models import Clock, utcnow
from clipper.jobs.worker import DEFAULT_MEDIA_ROOT
from clipper.posting import repo
from clipper.posting.models import (
    MAX_ATTEMPTS,
    Post,
    PostConflict,
    PostStatus,
    Publisher,
    PublishResult,
)
from clipper.redact import redact

STALE_UPLOAD = timedelta(minutes=30)
BACKOFF_BASE = timedelta(minutes=5)
UNRESOLVED = "uploading state unresolved"
DEFAULT_BATCH = 10

logger = logging.getLogger(__name__)

Alert = Callable[[str], None]


def log_alert(message: str) -> None:
    """Default alert: an ERROR log line. Discord alerting is CLP-30."""
    logger.error("ALERT: %s", message)


@dataclass
class PostingStage:
    conn: sqlite3.Connection
    publishers: Mapping[str, Publisher]
    worker_id: str
    media_root: Path = DEFAULT_MEDIA_ROOT
    clock: Clock = utcnow
    alert: Alert = log_alert
    batch: int = DEFAULT_BATCH
    _warned: set[str] = field(default_factory=set, init=False, repr=False)

    def run_once(self) -> int:
        """One posting pass; returns how many posts it acted on."""
        handled = self._recover_stale()
        self._warn_unpublishable()
        # One post at a time (claim, publish, record), so a post never sits claimed while
        # others upload and cannot go stale before it is even tried.
        for _ in range(self.batch):
            due = repo.claim_due(
                self.conn,
                self.clock(),
                self.worker_id,
                platforms=self.publishers.keys(),
                limit=1,
            )
            if not due:
                break
            self._publish(due[0])
            handled += 1
        return handled

    # --- steps -------------------------------------------------------------------------

    def _warn_unpublishable(self) -> None:
        missing = repo.due_platforms(self.conn, self.clock()) - self.publishers.keys()
        for platform in sorted(missing - self._warned):
            logger.warning("no publisher registered for %s; its due posts stay scheduled", platform)
            self._warned.add(platform)

    def _publish(self, post: Post) -> None:
        publisher = self.publishers[post.platform]
        if post.attempts > 0 and self._already_live(post, publisher):
            return
        try:
            path = resolve_media_path(self.media_root, post.video_path)
            if path is None:
                raise FileNotFoundError("rendered video is missing")
            metadata = repo.metadata_for(self.conn, post)
            result = publisher.publish(post, path, metadata)
        except Exception as exc:
            self._failed_attempt(post, _describe(exc))
            return
        # Outside the try: if this write fails the post stays `uploading` and the stale
        # pass resolves it through `lookup` instead of uploading it again.
        self._posted(post, result)

    def _already_live(self, post: Post, publisher: Publisher) -> bool:
        """Before re-publishing after a failed attempt, ask `lookup` (when there is one)
        whether the earlier attempt went through after all, e.g. it timed out reading the
        response. True when the post was recorded as posted or handed to a human."""
        lookup = getattr(publisher, "lookup", None)
        if not callable(lookup):
            return False
        try:
            found = lookup(post.idempotency_key)
        except Exception as exc:
            self._give_up(post, f"{UNRESOLVED}: lookup failed: {_describe(exc)}")
            return True
        if found is None:
            return False
        logger.info("%s: an earlier attempt went through; recorded", _label(post))
        self._posted(post, found)
        return True

    def _recover_stale(self) -> int:
        """Resolve posts a crashed (or very slow) worker left `uploading`. Never re-publish:
        only a `lookup` that finds the upload records it as posted; anything else is
        `post_failed` for a human to check, since the upload may still be in flight."""
        stale = repo.claim_stale(
            self.conn,
            self.clock(),
            self.worker_id,
            older_than=STALE_UPLOAD,
            platforms=self.publishers.keys(),
        )
        for post in stale:
            lookup = getattr(self.publishers[post.platform], "lookup", None)
            if not callable(lookup):
                logger.error("%s: crashed mid-upload and cannot be checked", _label(post))
                self._give_up(post, UNRESOLVED)
                continue
            try:
                found = lookup(post.idempotency_key)
            except Exception as exc:
                self._give_up(post, f"{UNRESOLVED}: lookup failed: {_describe(exc)}")
                continue
            if found is not None:
                logger.info("%s: upload from a crashed worker found; recorded", _label(post))
                self._posted(post, found)
            else:
                self._give_up(post, f"{UNRESOLVED}: not found on the platform")
        return len(stale)

    # --- outcomes ----------------------------------------------------------------------

    def _posted(self, post: Post, result: PublishResult) -> None:
        try:
            repo.mark_posted(self.conn, post, self.worker_id, result, self.clock())
        except PostConflict as exc:
            # The video is live but the row changed under us: a possible duplicate.
            message = (
                f"{_label(post)} of clip {post.clip_id} was uploaded (external id"
                f" {result.external_id}, url {result.url or '-'}) but the result could not be"
                f" recorded ({exc}); check the platform for a duplicate."
            )
            logger.error("%s: result dropped: %s", _label(post), exc)
            self._send(message, post)
            return
        logger.info("%s: posted (%s)", _label(post), result.external_id)

    def _failed_attempt(self, post: Post, error: str) -> None:
        try:
            updated = repo.record_failure(
                self.conn,
                post,
                self.worker_id,
                error,
                self.clock(),
                max_attempts=MAX_ATTEMPTS,
                backoff=BACKOFF_BASE,
            )
        except PostConflict as exc:
            logger.warning("%s: failure not recorded: %s", _label(post), exc)
            return
        if updated.status == PostStatus.POST_FAILED:
            self._alert(updated, error)
        else:
            logger.warning(
                "%s: attempt %d failed: %s; retry at %s",
                _label(post),
                updated.attempts,
                error,
                updated.scheduled_at.isoformat(),
            )

    def _give_up(self, post: Post, error: str) -> None:
        try:
            failed = repo.mark_failed(self.conn, post, self.worker_id, error, self.clock())
        except PostConflict as exc:
            logger.warning("%s: failure not recorded: %s", _label(post), exc)
            return
        self._alert(failed, error)

    def _alert(self, post: Post, error: str) -> None:
        message = (
            f"{_label(post)} of clip {post.clip_id} is post_failed after"
            f" {post.attempts} attempt(s): {error}. Requeue with `clipper posts retry {post.id}`."
        )
        self._send(message, post)

    def _send(self, message: str, post: Post) -> None:
        try:
            self.alert(message)
        except Exception:
            logger.exception("%s: alert failed", _label(post))


def _describe(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}")


def _label(post: Post) -> str:
    return f"post {post.id} ({post.platform}/{post.account_id})"
