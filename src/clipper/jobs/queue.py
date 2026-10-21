"""Job queue operations on the `jobs` table. Every write runs inside `transaction()`.

A claim is recorded as (worker_id, lease_until), where worker_id is unique per process.
Claimed jobs are invisible to other claimers until the holder finishes them or the lease
expires. Lease renewal (heartbeats) and concurrency limits come later (E3-S1b).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

from clipper.db import transaction
from clipper.jobs.models import TERMINAL, Job, JobStatus, to_iso
from clipper.urls import SourceRef, canonical_url

DEFAULT_LEASE = timedelta(hours=2)
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=1)
MAX_ATTEMPTS = 5
MAX_ERROR_CHARS = 2000
RETRYABLE = frozenset({JobStatus.FAILED, JobStatus.SKIPPED})


class JobConflict(Exception):
    """The job changed under us (status moved or claim lost); the update was not applied."""


def enqueue(
    conn: sqlite3.Connection,
    ref: SourceRef,
    now: datetime,
    *,
    creator: str,
    is_stream: bool = False,
) -> tuple[Job, bool]:
    """Insert a `detected` job unless (platform, item_id) exists. Returns (job, created)."""
    stamp = to_iso(now)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO jobs (platform, item_id, creator, source_url, is_stream, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (platform, item_id) DO NOTHING",
            (
                ref.platform,
                ref.item_id,
                creator,
                canonical_url(ref),
                int(is_stream),
                JobStatus.DETECTED.value,
                stamp,
                stamp,
            ),
        )
        created = cur.rowcount == 1
        row = conn.execute(
            "SELECT * FROM jobs WHERE platform = ? AND item_id = ?", (ref.platform, ref.item_id)
        ).fetchone()
    return Job.from_row(row), created


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def claim_next(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    worker_id: str,
    statuses: Iterable[JobStatus],
    lease: timedelta = DEFAULT_LEASE,
) -> Job | None:
    """Atomically claim the oldest due, unclaimed job whose status is in `statuses`.

    Rows that cannot be decoded into a Job (corrupt platform/id) are marked failed and
    passed over, so one bad row cannot wedge the queue.
    """
    wanted = [s.value for s in statuses if s not in TERMINAL]
    if not wanted:
        return None
    stamp = to_iso(now)
    marks = ", ".join("?" * len(wanted))
    with transaction(conn):
        while True:
            row = conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks})"  # noqa: S608 - placeholders only
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                " AND (worker_id IS NULL OR lease_until IS NULL OR lease_until <= ?)"
                " ORDER BY COALESCE(next_attempt_at, created_at), id LIMIT 1",
                (*wanted, stamp, stamp),
            ).fetchone()
            if row is None:
                return None
            try:
                job = Job.from_row(row)
            except (ValueError, TypeError) as exc:
                conn.execute(
                    "UPDATE jobs SET status = ?, last_error = ?, worker_id = NULL,"
                    " lease_until = NULL, updated_at = ? WHERE id = ?",
                    (JobStatus.FAILED.value, f"undecodable job row: {exc}", stamp, row["id"]),
                )
                continue
            conn.execute(
                "UPDATE jobs SET worker_id = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                (worker_id, to_iso(now + lease), stamp, job.id),
            )
            return job


def release_claim(conn: sqlite3.Connection, job: Job, worker_id: str, now: datetime) -> bool:
    """Hand one claimed job back (e.g. on interrupt) without touching its status."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET worker_id = NULL, lease_until = NULL, updated_at = ?"
            " WHERE id = ? AND worker_id = ?",
            (to_iso(now), job.id, worker_id),
        )
    return cur.rowcount == 1


def release_claims(conn: sqlite3.Connection, worker_id: str, now: datetime) -> int:
    """Drop every claim held by `worker_id`: operator recovery of a known-dead worker."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET worker_id = NULL, lease_until = NULL, updated_at = ?"
            " WHERE worker_id = ?",
            (to_iso(now), worker_id),
        )
    return cur.rowcount


def _finish(
    conn: sqlite3.Connection, job: Job, worker_id: str, assignments: str, params: tuple[object, ...]
) -> None:
    """Apply `assignments`, release the claim; only if we still hold it at the same status."""
    with transaction(conn):
        cur = conn.execute(
            f"UPDATE jobs SET {assignments}, worker_id = NULL, lease_until = NULL"  # noqa: S608
            " WHERE id = ? AND status = ? AND worker_id = ?",
            (*params, job.id, job.status.value, worker_id),
        )
        if cur.rowcount != 1:
            raise JobConflict(f"job {job.id} is no longer claimed by {worker_id} at {job.status}")


def advance(conn: sqlite3.Connection, job: Job, now: datetime, *, worker_id: str) -> JobStatus:
    """Persist the job's next pipeline status; resets the per-stage retry state."""
    nxt = job.next_status
    _finish(
        conn,
        job,
        worker_id,
        "status = ?, attempts = 0, next_attempt_at = NULL, last_error = NULL, updated_at = ?",
        (nxt.value, to_iso(now)),
    )
    return nxt


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_BASE * 2 ** min(max(attempts - 1, 0), 16), BACKOFF_CAP)


def record_failure(
    conn: sqlite3.Connection, job: Job, error: str, now: datetime, *, worker_id: str
) -> datetime | None:
    """Keep the status, bump attempts, schedule a retry with exponential backoff.

    After MAX_ATTEMPTS failures of the same stage the job is marked failed; returns None.
    """
    attempts = job.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        _finish(
            conn,
            job,
            worker_id,
            "status = ?, attempts = ?, last_error = ?, next_attempt_at = NULL, updated_at = ?",
            (
                JobStatus.FAILED.value,
                attempts,
                f"gave up after {attempts} attempts: {error}"[:MAX_ERROR_CHARS],
                to_iso(now),
            ),
        )
        return None
    retry_at = now + backoff(attempts)
    _finish(
        conn,
        job,
        worker_id,
        "attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?",
        (attempts, error[:MAX_ERROR_CHARS], to_iso(retry_at), to_iso(now)),
    )
    return retry_at


def mark_terminal(
    conn: sqlite3.Connection,
    job: Job,
    status: JobStatus,
    reason: str,
    now: datetime,
    *,
    worker_id: str,
) -> None:
    """Move to `failed` (reason -> last_error) or `skipped` (reason -> skip_reason)."""
    if status is JobStatus.FAILED:
        column = "last_error"
    elif status is JobStatus.SKIPPED:
        column = "skip_reason"
    else:
        raise ValueError(f"not a failure/skip status: {status}")
    _finish(
        conn,
        job,
        worker_id,
        f"status = ?, {column} = ?, next_attempt_at = NULL, updated_at = ?",
        (status.value, reason[:MAX_ERROR_CHARS], to_iso(now)),
    )


def requeue(conn: sqlite3.Connection, job_id: int, now: datetime) -> Job:
    """Reset a failed/skipped job to `detected` with a clean retry state.

    Existing stage artifacts are kept, so the rerun skips stages that already finished.
    """
    retryable = [s.value for s in RETRYABLE]
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET status = ?, attempts = 0, last_error = NULL, skip_reason = NULL,"
            " next_attempt_at = NULL, worker_id = NULL, lease_until = NULL, updated_at = ?"
            " WHERE id = ? AND status IN (?, ?)",
            (JobStatus.DETECTED.value, to_iso(now), job_id, *retryable),
        )
        if cur.rowcount != 1:
            raise JobConflict(f"job {job_id} is not failed/skipped")
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row)
