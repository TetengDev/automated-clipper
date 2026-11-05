"""Single-worker loop: claim a due job, run the stage for its status, persist the outcome.

The stage contract (what CLP-13..16 plug into) is documented in `clipper.jobs.stages`.

Each worker process claims under a unique id, so a crashed worker's job becomes available
again when its lease expires (or immediately via `run-worker --recover-id <dead id>`, which
also hands the dead worker's `uploading` posts to the posting stage's stale pass).

The posting pass (CLP-92) runs on a thread of its own (CLP-105), once per poll, so a long
job stage never delays a due post. It must therefore use a DB connection of its own.
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
from typing import Protocol, runtime_checkable

from clipper.config import AppConfig
from clipper.jobs.models import TERMINAL, Clock, Job, JobStatus, utcnow
from clipper.jobs.queue import (
    DEFAULT_LEASE,
    JobConflict,
    advance,
    claim_next,
    defer,
    mark_terminal,
    record_failure,
    release_claim,
    release_claims,
)
from clipper.jobs.stages import (
    DEFAULT_STAGES,
    Deferred,
    PermanentError,
    SkipJob,
    StageContext,
    StageDef,
)
from clipper.redact import redact

DEFAULT_MEDIA_ROOT = Path("data/media")
DEFAULT_POLL_SECONDS = 5.0
INTERRUPT_JOIN_SECONDS = 10.0
"""How long an interrupted loop waits for the posting thread before giving up on it."""

logger = logging.getLogger(__name__)


class PeriodicPass(Protocol):
    """Work the loop runs once per poll besides the job stages (CLP-92's posting stage).

    It runs on its own thread, so it must not share the worker's connection.
    """

    def run_once(self) -> int: ...


@runtime_checkable
class RecoverablePass(PeriodicPass, Protocol):
    """A periodic pass that can also release a dead worker's claims (`Worker.recover`)."""

    def recover(self, dead_worker_id: str) -> int: ...


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
    posting: PeriodicPass | None = None
    """Posting pass (`clipper.posting.stage.PostingStage`), run once per poll on its own
    thread by `run_forever`."""
    _posting_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    @property
    def posting_running(self) -> bool:
        """Whether the posting thread is still running (its connection is in use)."""
        return self._posting_thread is not None and self._posting_thread.is_alive()

    def recover(self, dead_worker_id: str) -> int:
        """Release claims of a worker known to be dead, without waiting for lease expiry.
        Returns the number of jobs released; the posting pass releases the dead worker's
        uploads too (its own count is logged)."""
        released = release_claims(self.conn, dead_worker_id, self.clock())
        logger.info("released %d job(s) claimed by %s", released, dead_worker_id)
        if isinstance(self.posting, RecoverablePass):
            self.posting.recover(dead_worker_id)
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
        except Deferred as exc:
            reason = redact(f"deferred: {exc.reason}")
            logger.info("%s: %s; next try at %s", log, reason, exc.until.isoformat())
            defer(self.conn, job, exc.until, reason, self.clock(), worker_id=self.worker_id)
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
        """Loop until `stop_event` is set; a running stage (and upload) always finishes first.

        Errors outside a stage (e.g. DB locked while recording an outcome) are logged and
        retried after `poll_seconds`; only BaseException (interrupt, exit) stops the loop.
        The posting pass runs on its own thread, once per `poll_seconds`, and stops with
        the loop.
        """
        unhandled = [s.value for s in JobStatus if s not in self.stages and s not in TERMINAL]
        logger.info("worker %s started; not handling: %s", self.worker_id, ", ".join(unhandled))
        self._start_posting(stop_event, poll_seconds)
        try:
            while not stop_event.is_set():
                self._check_posting(stop_event, poll_seconds)
                try:
                    busy = self.run_once()
                except Exception:
                    logger.exception("worker loop error; retrying in %ss", poll_seconds)
                    busy = False
                if not busy:
                    stop_event.wait(poll_seconds)
        except BaseException:
            # Interrupted: stop the posting thread, but wait only briefly for it.
            stop_event.set()
            if self._posting_thread is not None:
                self._posting_thread.join(INTERRUPT_JOIN_SECONDS)
            raise
        if self._posting_thread is not None:
            if self._posting_thread.is_alive():
                logger.info("waiting for the posting pass to finish its upload")
            self._posting_thread.join()

    def _start_posting(self, stop_event: threading.Event, poll_seconds: float) -> None:
        if self.posting is None:
            return
        self._posting_thread = threading.Thread(
            target=self._posting_loop,
            args=(self.posting, stop_event, poll_seconds),
            name="posting",
            daemon=True,  # an interrupted process must not hang on an upload
        )
        self._posting_thread.start()

    def _check_posting(self, stop_event: threading.Event, poll_seconds: float) -> None:
        """Restart the posting thread if something it did not catch killed it."""
        if self._posting_thread is None or self._posting_thread.is_alive():
            return
        if stop_event.is_set():
            return
        logger.error("posting thread died; restarting it")
        self._start_posting(stop_event, poll_seconds)

    @staticmethod
    def _posting_loop(
        posting: PeriodicPass, stop_event: threading.Event, poll_seconds: float
    ) -> None:
        while not stop_event.is_set():
            try:
                posting.run_once()
            except Exception:
                logger.exception("posting pass failed; retrying next poll")
            stop_event.wait(poll_seconds)
