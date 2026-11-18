import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.db import connect, migrate, transaction
from clipper.jobs.models import (
    COMMENTARY_PIPELINE,
    PIPELINE,
    TERMINAL,
    Job,
    JobKind,
    JobStatus,
)
from clipper.jobs.queue import (
    BACKOFF_CAP,
    DEFAULT_LEASE,
    MAX_ATTEMPTS,
    JobConflict,
    advance,
    backoff,
    claim_next,
    enqueue,
    get_job,
    hold_lease,
    jittered,
    mark_terminal,
    record_failure,
    release_claim,
    release_claims,
    requeue,
)
from clipper.urls import SourceRef

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ALL = [s for s in JobStatus if s not in TERMINAL]
YT = SourceRef("youtube", "dQw4w9WgXcQ")
DAY = SourceRef("commentary", "random_gaming_yt:2026-11-19")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


def test_enqueue_inserts_detected_job_with_canonical_url(conn: sqlite3.Connection) -> None:
    job, created = enqueue(conn, YT, T0, creator="manual")

    assert created
    assert job.status is JobStatus.DETECTED
    assert job.ref == YT
    assert job.source_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job.attempts == 0


def test_enqueue_is_deduplicated(conn: sqlite3.Connection) -> None:
    first, created_first = enqueue(conn, YT, T0, creator="manual")
    second, created_second = enqueue(conn, YT, T0 + timedelta(hours=1), creator="other")

    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert second.creator == "manual"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_claim_respects_statuses_due_time_and_claims(conn: sqlite3.Connection) -> None:
    job, _ = enqueue(conn, YT, T0, creator="manual")

    assert claim_next(conn, T0, worker_id="w1", statuses=[JobStatus.RENDERING]) is None
    assert claim_next(conn, T0, worker_id="w1", statuses=[JobStatus.CLIPS_READY]) is None
    claimed = claim_next(conn, T0, worker_id="w1", statuses=ALL)
    assert claimed is not None and claimed.id == job.id
    # Held by w1: invisible to others until the lease expires.
    assert claim_next(conn, T0, worker_id="w2", statuses=ALL) is None
    late = T0 + timedelta(hours=2)
    assert claim_next(conn, late, worker_id="w2", statuses=ALL) is not None


def test_claim_order_is_oldest_first(conn: sqlite3.Connection) -> None:
    newer, _ = enqueue(conn, SourceRef("twitch", "2"), T0 + timedelta(seconds=5), creator="m")
    older, _ = enqueue(conn, SourceRef("twitch", "1"), T0, creator="m")

    first = claim_next(conn, T0 + timedelta(minutes=1), worker_id="w", statuses=ALL)
    second = claim_next(conn, T0 + timedelta(minutes=1), worker_id="w", statuses=ALL)

    assert [first.id, second.id] == [older.id, newer.id]  # type: ignore[union-attr]


def test_advance_walks_pipeline_skipping_waiting_vod(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    seen = []
    while (job := claim_next(conn, T0, worker_id="w", statuses=ALL)) is not None:
        seen.append(job.status)
        advance(conn, job, T0, worker_id="w")

    assert seen == [s for s in PIPELINE[:-1] if s is not JobStatus.WAITING_VOD]
    final = get_job(conn, job_id=1)
    assert final is not None and final.status is JobStatus.CLIPS_READY


def test_stream_jobs_wait_for_vod(conn: sqlite3.Connection) -> None:
    enqueue(conn, SourceRef("twitch", "9"), T0, creator="m", is_stream=True)
    job = claim_next(conn, T0, worker_id="w", statuses=ALL)

    assert job is not None
    assert advance(conn, job, T0, worker_id="w") is JobStatus.WAITING_VOD


def test_advance_requires_holding_the_claim(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w1", statuses=ALL)
    assert job is not None

    with pytest.raises(JobConflict):
        advance(conn, job, T0, worker_id="w2")
    advance(conn, job, T0, worker_id="w1")
    with pytest.raises(JobConflict):
        advance(conn, job, T0, worker_id="w1")  # stale status


def test_record_failure_backs_off_and_keeps_status(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w", statuses=ALL)
    assert job is not None

    retry_at = record_failure(conn, job, "boom " * 1000, T0, worker_id="w")

    failed = get_job(conn, job.id)
    assert failed is not None
    assert failed.status is JobStatus.DETECTED
    assert failed.attempts == 1
    assert failed.last_error is not None and len(failed.last_error) == 2000
    assert retry_at is not None
    assert T0 + timedelta(seconds=48) <= retry_at <= T0 + timedelta(seconds=72)  # 1 min ±20%
    assert claim_next(conn, T0 + timedelta(seconds=47), worker_id="w", statuses=ALL) is None
    again = claim_next(conn, retry_at, worker_id="w", statuses=ALL)
    assert again is not None and again.attempts == 2  # counted at claim

    advance(conn, again, retry_at, worker_id="w")
    advanced = get_job(conn, job.id)
    assert advanced is not None and advanced.attempts == 0 and advanced.last_error is None


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3)] == [60, 120, 240]
    assert backoff(50) == BACKOFF_CAP


@pytest.mark.parametrize(
    ("status", "column"), [(JobStatus.FAILED, "last_error"), (JobStatus.SKIPPED, "skip_reason")]
)
def test_mark_terminal(conn: sqlite3.Connection, status: JobStatus, column: str) -> None:
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w", statuses=ALL)
    assert job is not None

    mark_terminal(conn, job, status, "nope", T0, worker_id="w")

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert (row["status"], row[column], row["worker_id"]) == (status.value, "nope", None)
    with pytest.raises(ValueError, match="not a failure"):
        mark_terminal(conn, job, JobStatus.RENDERING, "x", T0, worker_id="w")


def test_release_claims_frees_own_jobs_only(conn: sqlite3.Connection) -> None:
    enqueue(conn, SourceRef("twitch", "1"), T0, creator="m")
    enqueue(conn, SourceRef("twitch", "2"), T0, creator="m")
    claim_next(conn, T0, worker_id="w1", statuses=ALL)
    claim_next(conn, T0, worker_id="w2", statuses=ALL)

    assert release_claims(conn, "w1", T0) == 1
    reclaimed = claim_next(conn, T0, worker_id="w3", statuses=ALL)
    assert reclaimed is not None and reclaimed.ref.item_id == "1"


def test_naive_datetime_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="naive"):
        enqueue(conn, YT, datetime(2026, 1, 1), creator="m")


def test_concurrent_claims_never_hand_out_a_job_twice(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    setup = connect(db)
    migrate(setup)
    for n in range(1, 61):
        enqueue(setup, SourceRef("twitch", str(n)), T0, creator="m")
    setup.close()

    claimed: dict[str, list[int]] = {"a": [], "b": []}
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def run(name: str) -> None:
        try:
            conn = connect(db)
            start.wait()
            while (job := claim_next(conn, T0, worker_id=name, statuses=ALL)) is not None:
                claimed[name].append(job.id)
            conn.close()
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    ids = claimed["a"] + claimed["b"]
    assert sorted(ids) == list(range(1, 61))
    assert len(set(ids)) == len(ids)


def test_record_failure_gives_up_after_max_attempts(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    now = T0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        job = claim_next(conn, now, worker_id="w", statuses=ALL)
        assert job is not None and job.attempts == attempt
        retry_at = record_failure(conn, job, "boom", now, worker_id="w")
        now += timedelta(hours=1)

    assert retry_at is None
    final = get_job(conn, 1)
    assert final is not None and final.status is JobStatus.FAILED
    assert final.last_error == f"gave up after {MAX_ATTEMPTS} attempts: boom"


def test_claim_counts_attempts_so_crash_loops_end_in_failed(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    now = T0
    seen = []
    while (job := claim_next(conn, now, worker_id="w", statuses=ALL)) is not None:
        seen.append(job.attempts)  # the worker dies mid-stage: nothing is recorded
        now += DEFAULT_LEASE

    assert seen == list(range(1, MAX_ATTEMPTS + 1))
    row = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == JobStatus.FAILED.value
    assert row["worker_id"] is None
    assert row["last_error"] == (f"gave up after {MAX_ATTEMPTS} attempts (worker died mid-stage)")


def test_claim_fails_undecodable_rows_and_moves_on(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES ('vimeo', '../x', 'm', 'u', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
    )
    good, _ = enqueue(conn, YT, T0, creator="manual")

    claimed = claim_next(conn, T0, worker_id="w", statuses=ALL)

    assert claimed is not None and claimed.id == good.id
    bad = conn.execute("SELECT * FROM jobs WHERE platform = 'vimeo'").fetchone()
    assert bad["status"] == "failed"
    assert bad["last_error"].startswith("undecodable job row")
    assert bad["worker_id"] is None


def test_requeue_resets_failed_or_skipped_job(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w", statuses=ALL)
    assert job is not None
    with pytest.raises(JobConflict):
        requeue(conn, job.id, T0)  # not terminal yet
    mark_terminal(conn, job, JobStatus.SKIPPED, "too short", T0, worker_id="w")

    again = requeue(conn, job.id, T0)

    assert (again.status, again.attempts, again.last_error) == (JobStatus.DETECTED, 0, None)
    row = conn.execute("SELECT skip_reason FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["skip_reason"] is None
    assert claim_next(conn, T0, worker_id="w", statuses=ALL) is not None


def test_release_claim_only_touches_that_job_and_holder(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w1", statuses=ALL)
    assert job is not None

    assert not release_claim(conn, job, "w2", T0)
    assert release_claim(conn, job, "w1", T0)
    assert claim_next(conn, T0, worker_id="w2", statuses=ALL) is not None


def test_hold_lease_passes_only_for_the_live_holder(conn: sqlite3.Connection) -> None:
    """CLP-66: a stage fences its own writes on the claim, inside its transaction."""
    enqueue(conn, YT, T0, creator="manual")
    job = claim_next(conn, T0, worker_id="w1", statuses=ALL, lease=timedelta(minutes=10))
    assert job is not None

    with transaction(conn):
        hold_lease(conn, job.id, "w1", T0 + timedelta(minutes=9))
    for worker_id, now in (("w2", T0), ("w1", T0 + timedelta(minutes=10))):
        with pytest.raises(JobConflict), transaction(conn):
            hold_lease(conn, job.id, worker_id, now)
    release_claim(conn, job, "w1", T0)
    with pytest.raises(JobConflict), transaction(conn):
        hold_lease(conn, job.id, "w1", T0)


@pytest.mark.parametrize(("factor", "seconds"), [(0.8, 96), (1.0, 120), (1.2, 144)])
def test_retry_delay_is_jittered_by_up_to_20_percent(
    conn: sqlite3.Connection, factor: float, seconds: int
) -> None:
    """CLP-34: jobs that failed together (an outage) do not all retry in the same poll."""
    enqueue(conn, YT, T0, creator="manual")
    conn.execute("UPDATE jobs SET attempts = 1")
    job = claim_next(conn, T0, worker_id="w", statuses=ALL)
    assert job is not None and job.attempts == 2
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return factor

    retry_at = record_failure(conn, job, "boom", T0, worker_id="w", uniform=uniform)

    assert bounds == [(0.8, 1.2)]
    assert retry_at == T0 + timedelta(seconds=seconds)  # backoff(2) = 120 s


def test_retry_delays_spread_out() -> None:
    delays = {jittered(backoff(3)) for _ in range(50)}
    assert len(delays) > 1
    assert all(timedelta(seconds=192) <= d <= timedelta(seconds=288) for d in delays)


# --- CLP-143: commentary jobs have their own chain and claims ---------------------------------


def test_clip_jobs_keep_their_chain() -> None:
    assert PIPELINE == (
        JobStatus.DETECTED,
        JobStatus.WAITING_VOD,
        JobStatus.FETCHING_AUDIO,
        JobStatus.TRANSCRIBING,
        JobStatus.SELECTING,
        JobStatus.FETCHING_CLIPS,
        JobStatus.RENDERING,
        JobStatus.CLIPS_READY,
    )
    assert COMMENTARY_PIPELINE == (
        JobStatus.DETECTED,
        JobStatus.DISCOVERING,
        JobStatus.FETCHING_SOURCES,
        JobStatus.TRANSCRIBING,
        JobStatus.SCRIPTING,
        JobStatus.VOICING,
        JobStatus.RENDERING,
        JobStatus.CLIPS_READY,
    )
    assert {"discovering", "fetching_sources", "scripting", "voicing"} <= {
        s.value for s in JobStatus
    }


def test_a_commentary_job_is_its_own_kind_with_no_source_url(conn: sqlite3.Connection) -> None:
    job, created = enqueue(conn, DAY, T0, creator="random_gaming_yt")

    assert created
    assert job.kind is JobKind.COMMENTARY
    assert job.source_url == ""
    assert job.creator == "random_gaming_yt"
    assert job.ref.key == "commentary_random_gaming_yt_2026-11-19"
    assert enqueue(conn, YT, T0, creator="manual")[0].kind is JobKind.CLIP


def test_clip_statuses_never_claim_a_commentary_job(conn: sqlite3.Connection) -> None:
    enqueue(conn, DAY, T0, creator="random_gaming_yt")

    assert claim_next(conn, T0, worker_id="w", statuses=ALL) is None
    claimed = claim_next(
        conn, T0, worker_id="w", statuses=ALL, commentary_statuses=[JobStatus.DETECTED]
    )
    assert claimed is not None and claimed.ref == DAY


def test_commentary_statuses_never_claim_a_clip_job(conn: sqlite3.Connection) -> None:
    enqueue(conn, YT, T0, creator="manual")

    assert claim_next(conn, T0, worker_id="w", statuses=[], commentary_statuses=ALL) is None


def test_a_commentary_job_walks_its_own_chain(conn: sqlite3.Connection) -> None:
    enqueue(conn, DAY, T0, creator="random_gaming_yt")
    seen = []
    while (
        job := claim_next(conn, T0, worker_id="w", statuses=[], commentary_statuses=ALL)
    ) is not None:
        seen.append(job.status)
        advance(conn, job, T0, worker_id="w")

    assert seen == list(COMMENTARY_PIPELINE[:-1])
    final = get_job(conn, job_id=1)
    assert final is not None and final.status is JobStatus.CLIPS_READY


def test_a_clip_job_has_no_successor_at_a_commentary_status() -> None:
    job = Job(
        id=1,
        ref=YT,
        creator="m",
        source_url="",
        status=JobStatus.SCRIPTING,
        is_stream=False,
        attempts=0,
    )

    with pytest.raises(ValueError, match="no stage after scripting"):
        _ = job.next_status
