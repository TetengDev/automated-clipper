"""Single-worker loop: claim a due job, run the stage for its status, persist the outcome.

The stage contract (what CLP-13..16 plug into) is documented in `clipper.jobs.stages`.

Each worker process claims under a unique id, so a crashed worker's job becomes available
again when its lease expires (or immediately via `run-worker --recover-id <dead id>`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from clipper.config import AppConfig
from clipper.jobs.models import TERMINAL, Clock, Job, JobStatus, utcnow
from clipper.jobs.queue import (
    DEFAULT_LEASE,
    JobConflict,
    advance,
    claim_next,
    mark_terminal,
    record_failure,
    release_claim,
    release_claims,
)
from clipper.jobs.stages import DEFAULT_STAGES, PermanentError, SkipJob, StageContext, StageDef
from clipper.redact import redact

DEFAULT_MEDIA_ROOT = Path("data/media")
DEFAULT_POLL_SECONDS = 5.0

logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass
class Worker:
    conn: sqlite3.Connection
    config: AppConfig
    worker_id: str
    stages: Mapping[JobStatus, StageDef] = field(default_factory=lambda: dict(DEFAULT_STAGES))
    media_root: Path = DEFAULT_MEDIA_ROOT
    clock: Clock = utcnow
    lease: timedelta = DEFAULT_LEASE

    def recover(self, dead_worker_id: str) -> int:
        """Release claims of a worker known to be dead, without waiting for lease expiry."""
        released = release_claims(self.conn, dead_worker_id, self.clock())
        logger.info("released %d job(s) claimed by %s", released, dead_worker_id)
        return released

    def run_once(self) -> bool:
        """Process one stage of one due job. Returns False when nothing was due."""
        job: Job | None = None
        try:
            job = claim_next(
                self.conn,
                self.clock(),
                worker_id=self.worker_id,
                statuses=self.stages.keys(),
                lease=self.lease,
            )
            if job is None:
                return False
            self._run_stage(job)
        except BaseException:
            # Interrupted or the outcome write failed: hand the job back now rather than
            # leaving it claimed until the lease runs out.
            if job is not None:
                with contextlib.suppress(Exception):
                    release_claim(self.conn, job, self.worker_id, self.clock())
            raise
        return True

    def _run_stage(self, job: Job) -> None:
        stage = self.stages[job.status]
        media_dir = self.media_root / job.ref.key
        artifact = media_dir / stage.artifact if stage.artifact else None
        log = f"job {job.id} ({job.ref.key}) {job.status}"
        try:
            self._run_and_record(job, stage, media_dir, artifact, log)
        except JobConflict as exc:
            logger.warning("%s: result dropped: %s", log, exc)

    def _run_and_record(
        self, job: Job, stage: StageDef, media_dir: Path, artifact: Path | None, log: str
    ) -> None:
        try:
            if artifact is not None and artifact.is_file():
                logger.info("%s: artifact %s exists, skipping stage", log, stage.artifact)
            else:
                media_dir.mkdir(parents=True, exist_ok=True)
                stage.run(job, StageContext(media_dir, self.conn, self.config, self.clock))
                if artifact is not None and not artifact.is_file():
                    raise RuntimeError(f"stage finished without producing {stage.artifact}")
        except SkipJob as exc:
            reason = redact(str(exc)) or "skipped"
            logger.info("%s: skipped: %s", log, reason)
            self._terminal(job, JobStatus.SKIPPED, reason)
        except PermanentError as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
            logger.error("%s: failed permanently: %s", log, error)
            self._terminal(job, JobStatus.FAILED, error)
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
            logger.error("%s: stage error: %s", log, error)
            logger.debug("%s: stage error traceback", log, exc_info=True)  # unredacted
            retry_at = record_failure(self.conn, job, error, self.clock(), worker_id=self.worker_id)
            if retry_at is None:
                logger.error("%s: out of attempts, marked failed", log)
            else:
                logger.info("%s: retry at %s", log, retry_at.isoformat())
        else:
            nxt = advance(self.conn, job, self.clock(), worker_id=self.worker_id)
            logger.info("%s -> %s", log, nxt)

    def _terminal(self, job: Job, status: JobStatus, reason: str) -> None:
        mark_terminal(self.conn, job, status, reason, self.clock(), worker_id=self.worker_id)

    def run_forever(
        self, stop_event: threading.Event, poll_seconds: float = DEFAULT_POLL_SECONDS
    ) -> None:
        """Loop until `stop_event` is set; a running stage always finishes first.

        Errors outside a stage (e.g. DB locked while recording an outcome) are logged and
        retried after `poll_seconds`; only BaseException (interrupt, exit) stops the loop.
        """
        unhandled = [s.value for s in JobStatus if s not in self.stages and s not in TERMINAL]
        logger.info("worker %s started; not handling: %s", self.worker_id, ", ".join(unhandled))
        while not stop_event.is_set():
            try:
                busy = self.run_once()
            except Exception:
                logger.exception("worker loop error; retrying in %ss", poll_seconds)
                busy = False
            if not busy:
                stop_event.wait(poll_seconds)
