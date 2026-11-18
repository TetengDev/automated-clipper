"""Lease heartbeat (CLP-34): keeps a claimed job's lease alive while its stage runs.

A thread renews `lease_until` to now + lease every lease/4 and stops when the stage
returns. It writes through a connection of its own (a connection is never shared between
threads, see `clipper.db.connect`). Once the claim is gone (another worker took the job
over after this one stalled for longer than the lease) it stops renewing; the stage's
fenced writes (`clipper.jobs.queue.hold_lease`) are then refused and its result dropped.

A renewal that fails (a locked DB, any error) is logged and tried again on the next beat;
the heartbeat never stops for that. The stage itself is never interrupted: if renewals
keep failing until `lease_until` passes, the job is no longer protected, another worker
may take it over, and this stage's fenced writes and result are dropped as above.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from clipper.db import connect
from clipper.jobs.models import Clock, Job
from clipper.jobs.queue import renew_lease

logger = logging.getLogger(__name__)

JOIN_TIMEOUT_S = 5.0
"""How long the end of a stage waits for the heartbeat thread, which may be stuck on a
locked DB (up to the 30 s busy timeout). Left behind, it exits after that beat: once the
outcome is recorded, its renewal no longer matches the job."""


def database_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind `conn`'s main database; None for an in-memory one."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def _renew_until_stopped(
    db: Path, job: Job, worker_id: str, lease: timedelta, clock: Clock, stop: threading.Event
) -> None:
    conn: sqlite3.Connection | None = None
    try:
        with contextlib.suppress(sqlite3.Error, OSError):  # else retried (and logged) per beat
            conn = connect(db)  # now, so the first beat does not wait for it
        while not stop.wait(lease.total_seconds() / 4):
            try:
                conn = conn or connect(db)
                if not renew_lease(conn, job, worker_id, clock(), lease):
                    logger.warning("job %s: claim lost, lease heartbeat stopped", job.id)
                    return
            except Exception as exc:  # e.g. the DB stayed locked: next beat
                logger.warning(
                    "job %s: lease renewal failed: %s (retrying; the claim lapses at its"
                    " lease_until unless a later beat succeeds)",
                    job.id,
                    exc,
                    exc_info=not isinstance(exc, sqlite3.Error | OSError),
                )
    finally:
        if conn is not None:
            conn.close()


@contextlib.contextmanager
def lease_heartbeat(
    conn: sqlite3.Connection, job: Job, worker_id: str, lease: timedelta, clock: Clock
) -> Iterator[None]:
    """Renew the claim on `job` while the block runs (not for an in-memory DB)."""
    db = database_file(conn)
    if db is None:
        yield
        return
    stop = threading.Event()
    beat = threading.Thread(
        target=_renew_until_stopped,
        args=(db, job, worker_id, lease, clock, stop),
        name=f"lease-heartbeat-{job.id}",
        daemon=True,
    )
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(JOIN_TIMEOUT_S)
        if beat.is_alive():
            logger.warning(
                "job %s: lease heartbeat did not stop within %g s; leaving it behind",
                job.id,
                JOIN_TIMEOUT_S,
            )
