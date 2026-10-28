"""Worker stage `posting` (CLP-92): publish due posts through the platform publishers.

Each pass (the worker runs one per poll, see `clipper.jobs.worker.Worker.posting`):

1. Posts left `uploading` for longer than `STALE_UPLOAD` (a worker crashed mid-upload) are
   taken over. If the platform's publisher has `lookup(idempotency_key)`, it says whether
   the upload went through: found means `posted`, not found means a failed attempt. A
   publisher without `lookup` cannot tell, so the post becomes `post_failed` ("uploading
   state unresolved") for a human to check, rather than risk posting twice.
2. Due `scheduled` posts on platforms with a registered publisher are claimed (atomically
   moved to `uploading` under this worker's id) and published. Success stores the external
   id and URL (`posted`). A failure counts an attempt and backs off
   (`now + BACKOFF_BASE * 2**attempts`); the `MAX_ATTEMPTS`th failure is `post_failed`
   and raises an alert.
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
        due = repo.claim_due(
            self.conn,
            self.clock(),
            self.worker_id,
            platforms=self.publishers.keys(),
            limit=self.batch,
        )
        for post in due:
            self._publish(post)
        return handled + len(due)

    # --- steps -------------------------------------------------------------------------

    def _warn_unpublishable(self) -> None:
        missing = repo.due_platforms(self.conn, self.clock()) - self.publishers.keys()
        for platform in sorted(missing - self._warned):
            logger.warning("no publisher registered for %s; its due posts stay scheduled", platform)
            self._warned.add(platform)

    def _publish(self, post: Post) -> None:
        publisher = self.publishers[post.platform]
        try:
            path = resolve_media_path(self.media_root, post.video_path)
            if path is None:
                raise FileNotFoundError("rendered video is missing")
            metadata = repo.metadata_for(self.conn, post)
            result = publisher.publish(post, path, metadata)
        except Exception as exc:
            self._failed_attempt(post, redact(f"{type(exc).__name__}: {exc}"))
            return
        # Outside the try: if this write fails the post stays `uploading` and the stale
        # pass resolves it through `lookup` instead of uploading it again.
        self._posted(post, result)

    def _recover_stale(self) -> int:
        stale = repo.claim_stale(self.conn, self.clock(), self.worker_id, older_than=STALE_UPLOAD)
        for post in stale:
            publisher = self.publishers.get(post.platform)
            lookup = getattr(publisher, "lookup", None)
            if not callable(lookup):
                logger.error("%s: crashed mid-upload and cannot be checked", _label(post))
                self._give_up(post, UNRESOLVED)
                continue
            try:
                found = lookup(post.idempotency_key)
            except Exception as exc:
                self._failed_attempt(post, redact(f"lookup failed: {type(exc).__name__}: {exc}"))
                continue
            if found is not None:
                logger.info("%s: upload from a crashed worker found; recorded", _label(post))
                self._posted(post, found)
            else:
                self._failed_attempt(post, "upload interrupted; not found on the platform")
        return len(stale)

    # --- outcomes ----------------------------------------------------------------------

    def _posted(self, post: Post, result: PublishResult) -> None:
        try:
            repo.mark_posted(self.conn, post, self.worker_id, result, self.clock())
        except PostConflict as exc:
            logger.warning("%s: result dropped: %s", _label(post), exc)
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
        try:
            self.alert(message)
        except Exception:
            logger.exception("%s: alert failed", _label(post))


def _label(post: Post) -> str:
    return f"post {post.id} ({post.platform}/{post.account_id})"
