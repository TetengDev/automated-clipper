import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.config import AppConfig
from clipper.db import connect, migrate
from clipper.jobs import (
    Deferred,
    Job,
    JobStatus,
    PermanentError,
    SkipJob,
    StageContext,
    StageDef,
    Worker,
    default_worker_id,
    enqueue,
    get_job,
)
from clipper.jobs import heartbeat as heartbeat_mod
from clipper.jobs import worker as worker_mod
from clipper.jobs.models import COMMENTARY_PIPELINE, TERMINAL
from clipper.jobs.queue import DEFAULT_LEASE, JobConflict
from clipper.jobs.stages import DEFAULT_STAGES
from clipper.notify import NotifyError
from clipper.pipeline import build_commentary_stages, build_stages
from clipper.urls import SourceRef

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
YT = SourceRef("youtube", "dQw4w9WgXcQ")
DAY = SourceRef("commentary", "random_gaming_yt:2026-11-19")
# Real artifact names (clipper.pipeline.build_stages); rendering is CLP-17's, still a stub.
ARTIFACTS = {
    JobStatus.FETCHING_AUDIO: "audio_16k.flac",
    JobStatus.TRANSCRIBING: "transcript.json",
    JobStatus.SELECTING: "highlights.json",
    JobStatus.FETCHING_CLIPS: "sections.json",
    JobStatus.RENDERING: "render.done",
}


class Crash(BaseException):
    """Stands in for the process being interrupted mid-stage."""


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class StubPipeline:
    """Stub stages that write their artifact and count calls; can be told to crash/fail."""

    def __init__(self) -> None:
        self.calls: Counter[JobStatus] = Counter()
        self.crash_at: JobStatus | None = None
        self.crash_after_artifact = False
        self.fail_at: dict[JobStatus, BaseException] = {}
        self.hooks: dict[JobStatus, Callable[[], None]] = {}

    def stage(self, status: JobStatus) -> StageDef:
        artifact = ARTIFACTS[status]

        def run(job: Job, ctx: StageContext) -> None:
            self.calls[status] += 1
            assert ctx.media_dir.name == job.ref.key
            if status in self.hooks:
                self.hooks.pop(status)()
            if status in self.fail_at:
                raise self.fail_at[status]
            if self.crash_at is status and not self.crash_after_artifact:
                raise Crash
            path = ctx.media_dir / artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(status.value)
            if self.crash_at is status:
                raise Crash

        return StageDef(run, artifact)

    def stages(self) -> dict[JobStatus, StageDef]:
        return {**DEFAULT_STAGES, **{s: self.stage(s) for s in ARTIFACTS}}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


MakeWorker = Callable[..., Worker]


@pytest.fixture
def make_worker(db: Path, tmp_path: Path) -> Iterator[MakeWorker]:
    opened: list[sqlite3.Connection] = []

    def make(
        pipeline: StubPipeline | None = None,
        clock: FakeClock | None = None,
        worker_id: str = "w1",
    ) -> Worker:
        conn = connect(db)
        opened.append(conn)
        return Worker(
            conn=conn,
            config=AppConfig(),
            worker_id=worker_id,
            stages=pipeline.stages() if pipeline else dict(DEFAULT_STAGES),
            media_root=tmp_path / "media",
            clock=clock or FakeClock(),
        )

    yield make
    for conn in opened:
        conn.close()


def _drain(worker: Worker) -> int:
    steps = 0
    while worker.run_once():
        steps += 1
    return steps


def _row(db: Path, job_id: int) -> sqlite3.Row:
    with closing(connect(db)) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _status(db: Path, job_id: int) -> JobStatus:
    return JobStatus(_row(db, job_id)["status"])


def _hard_kill(db: Path, job_id: int, worker_id: str) -> None:
    """SIGKILL: the job stays claimed with a live lease and nobody releases it."""
    lease_until = (T0 + DEFAULT_LEASE).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(connect(db)) as conn:
        conn.execute(
            "UPDATE jobs SET worker_id = ?, lease_until = ? WHERE id = ?",
            (worker_id, lease_until, job_id),
        )


def test_worker_runs_job_to_clips_ready(make_worker: MakeWorker, db: Path, tmp_path: Path) -> None:
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    assert _drain(worker) == 6

    assert _status(db, job.id) is JobStatus.CLIPS_READY
    assert pipeline.calls == Counter(dict.fromkeys(ARTIFACTS, 1))
    media = tmp_path / "media" / "youtube_dQw4w9WgXcQ"
    assert all((media / name).is_file() for name in ARTIFACTS.values())


@pytest.mark.parametrize("after_artifact", [False, True])
def test_interrupted_worker_resumes_from_last_completed_stage(
    make_worker: MakeWorker, db: Path, after_artifact: bool
) -> None:
    pipeline = StubPipeline()
    pipeline.crash_at = JobStatus.TRANSCRIBING
    pipeline.crash_after_artifact = after_artifact
    first = make_worker(pipeline)
    job, _ = enqueue(first.conn, YT, T0, creator="manual")

    with pytest.raises(Crash):
        _drain(first)
    assert _status(db, job.id) is JobStatus.TRANSCRIBING
    assert _row(db, job.id)["worker_id"] is None  # interrupt handed the job back

    pipeline.crash_at = None
    _drain(make_worker(pipeline, worker_id="w2"))

    assert _status(db, job.id) is JobStatus.CLIPS_READY
    assert pipeline.calls[JobStatus.FETCHING_AUDIO] == 1  # never re-downloaded
    # Artifact written before the interrupt means the stage counts as done on restart.
    assert pipeline.calls[JobStatus.TRANSCRIBING] == (1 if after_artifact else 2)
    assert pipeline.calls[JobStatus.RENDERING] == 1


def test_hard_killed_job_resumes_after_lease_expiry(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    pipeline.crash_at = JobStatus.TRANSCRIBING
    pipeline.crash_after_artifact = True
    clock = FakeClock()
    first = make_worker(pipeline, clock, worker_id="dead")
    job, _ = enqueue(first.conn, YT, T0, creator="manual")
    with pytest.raises(Crash):
        _drain(first)
    _hard_kill(db, job.id, "dead")

    pipeline.crash_at = None
    restarted = make_worker(pipeline, clock, worker_id="new")
    assert not restarted.run_once()
    clock.now = T0 + DEFAULT_LEASE
    _drain(restarted)

    assert _status(db, job.id) is JobStatus.CLIPS_READY
    assert pipeline.calls[JobStatus.FETCHING_AUDIO] == 1
    assert pipeline.calls[JobStatus.TRANSCRIBING] == 1


def test_recover_id_releases_dead_workers_claims_immediately(
    make_worker: MakeWorker, db: Path
) -> None:
    pipeline = StubPipeline()
    first = make_worker(pipeline, worker_id="dead")
    job, _ = enqueue(first.conn, YT, T0, creator="manual")
    first.run_once()
    _hard_kill(db, job.id, "dead")

    restarted = make_worker(pipeline, worker_id="new")
    assert restarted.recover("dead") == 1
    assert restarted.run_once()
    assert _status(db, job.id) is JobStatus.TRANSCRIBING


def test_lease_expiry_mid_stage_drops_late_result(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    clock = FakeClock()
    slow = make_worker(pipeline, clock, worker_id="slow")
    fast = make_worker(StubPipeline(), clock, worker_id="fast")
    job, _ = enqueue(slow.conn, YT, T0, creator="manual")
    slow.run_once()  # detected -> fetching_audio

    def lease_expires_and_other_takes_over() -> None:
        clock.now = T0 + DEFAULT_LEASE
        assert fast.run_once()  # fetching_audio -> transcribing under the other worker

    pipeline.hooks[JobStatus.FETCHING_AUDIO] = lease_expires_and_other_takes_over

    assert slow.run_once()  # conflict is logged, not raised

    row = _row(db, job.id)
    assert row["status"] == JobStatus.TRANSCRIBING.value
    assert row["worker_id"] is None
    assert row["last_error"] is None
    assert slow.run_once()  # the slow worker keeps going
    assert _status(db, job.id) is JobStatus.SELECTING


def test_stage_that_lost_its_lease_records_nothing(make_worker: MakeWorker, db: Path) -> None:
    """CLP-66: a stage whose fenced write was refused (hold_lease) charges no attempt."""
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = JobConflict("job 1 is no longer leased to w1")
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()  # detected -> fetching_audio

    assert worker.run_once()

    row = _row(db, job.id)
    assert (row["status"], row["last_error"], row["next_attempt_at"]) == (
        "fetching_audio", None, None,
    )  # fmt: skip


def _lease_until(db: Path, job_id: int) -> str:
    return _row(db, job_id)["lease_until"]


def test_heartbeat_renews_the_lease_while_the_stage_runs(make_worker: MakeWorker, db: Path) -> None:
    """CLP-34: a stage that outlives the lease keeps its claim."""
    pipeline = StubPipeline()
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    worker.lease = timedelta(seconds=0.4)  # renewed every 0.1 s
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()  # detected -> fetching_audio
    seen: list[str] = []

    def long_stage() -> None:
        seen.append(_lease_until(db, job.id))
        clock.now = T0 + timedelta(hours=1)
        time.sleep(0.5)
        seen.append(_lease_until(db, job.id))

    pipeline.hooks[JobStatus.FETCHING_AUDIO] = long_stage
    threads = threading.active_count()

    assert worker.run_once()

    assert seen == ["2026-01-01T12:00:00Z", "2026-01-01T13:00:00Z"]
    assert threading.active_count() == threads  # stopped with the stage
    assert _status(db, job.id) is JobStatus.TRANSCRIBING


def test_heartbeat_stops_once_another_worker_holds_the_job(
    make_worker: MakeWorker, db: Path
) -> None:
    pipeline = StubPipeline()
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    worker.lease = timedelta(seconds=0.4)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()

    def taken_over() -> None:
        with closing(connect(db)) as other:
            other.execute(
                "UPDATE jobs SET worker_id = 'other', lease_until = 'theirs' WHERE id = ?",
                (job.id,),
            )
        clock.now = T0 + timedelta(hours=1)
        time.sleep(0.3)

    pipeline.hooks[JobStatus.FETCHING_AUDIO] = taken_over

    assert worker.run_once()  # the late result is dropped

    row = _row(db, job.id)
    assert (row["worker_id"], row["lease_until"], row["status"]) == (
        "other", "theirs", "fetching_audio",
    )  # fmt: skip


@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("database is locked"), RuntimeError("unexpected")]
)
def test_heartbeat_retries_a_failed_renewal(
    make_worker: MakeWorker,
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """Any renewal error is logged and the next beat tries again: the heartbeat only stops
    with its stage or once the claim is gone."""
    pipeline = StubPipeline()
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    worker.lease = timedelta(seconds=0.4)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()
    real = heartbeat_mod.renew_lease
    calls: list[int] = []

    def flaky(*args: object) -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise error
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(heartbeat_mod, "renew_lease", flaky)

    def long_stage() -> None:
        clock.now = T0 + timedelta(hours=1)
        time.sleep(0.5)
        assert _lease_until(db, job.id) == "2026-01-01T13:00:00Z"

    pipeline.hooks[JobStatus.FETCHING_AUDIO] = long_stage

    assert worker.run_once()
    assert _status(db, job.id) is JobStatus.TRANSCRIBING
    assert f"lease renewal failed: {error}" in caplog.text
    assert "the claim lapses at its lease_until" in caplog.text


def test_heartbeat_stop_waits_a_bounded_time(
    make_worker: MakeWorker,
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A beat stuck on a locked DB (up to the 30 s busy timeout) does not hold up the end
    of the stage, or Ctrl-C."""
    monkeypatch.setattr(heartbeat_mod, "JOIN_TIMEOUT_S", 0.2)
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    worker.lease = timedelta(seconds=0.4)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()
    stuck = threading.Event()
    unblock = threading.Event()

    def blocked(*args: object) -> bool:
        stuck.set()
        unblock.wait(5)
        return True

    monkeypatch.setattr(heartbeat_mod, "renew_lease", blocked)
    pipeline.hooks[JobStatus.FETCHING_AUDIO] = lambda: stuck.wait(2)
    started = time.monotonic()
    try:
        assert worker.run_once()
        elapsed = time.monotonic() - started
    finally:
        unblock.set()

    assert elapsed < 1.5
    assert _status(db, job.id) is JobStatus.TRANSCRIBING
    assert f"job {job.id}: lease heartbeat did not stop within 0.2 s" in caplog.text


def test_no_heartbeat_for_an_in_memory_db() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        assert heartbeat_mod.database_file(conn) is None
        threads = threading.active_count()
        job = Job(1, YT, "manual", "https://x", JobStatus.DETECTED, False, 1)
        with heartbeat_mod.lease_heartbeat(conn, job, "w", timedelta(seconds=1), FakeClock()):
            assert threading.active_count() == threads


def test_run_forever_survives_db_errors(
    make_worker: MakeWorker, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = RuntimeError("download failed")
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    stop = threading.Event()
    calls = []

    def locked(*args: object, **kwargs: object) -> None:
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker_mod, "record_failure", locked)

    worker.run_forever(stop, poll_seconds=0.01)

    assert len(calls) == 2  # the loop kept running after the first error
    row = _row(db, job.id)
    assert (row["status"], row["worker_id"]) == (JobStatus.FETCHING_AUDIO.value, None)


def test_existing_artifact_skips_stage(make_worker: MakeWorker, db: Path, tmp_path: Path) -> None:
    pipeline = StubPipeline()
    media = tmp_path / "media" / YT.key
    media.mkdir(parents=True)
    (media / "audio_16k.flac").write_text("already here")
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    assert pipeline.calls[JobStatus.FETCHING_AUDIO] == 0
    assert (media / "audio_16k.flac").read_text() == "already here"
    assert _status(db, job.id) is JobStatus.CLIPS_READY


def test_directory_does_not_count_as_artifact(make_worker: MakeWorker, tmp_path: Path) -> None:
    worker = make_worker(StubPipeline())
    worker.stages = {
        **worker.stages,
        JobStatus.FETCHING_AUDIO: StageDef(
            lambda job, ctx: (ctx.media_dir / "audio_16k.flac").mkdir(), "audio_16k.flac"
        ),
    }
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    stuck = get_job(worker.conn, job.id)
    assert stuck is not None and stuck.status is JobStatus.FETCHING_AUDIO
    assert "without producing audio_16k.flac" in (stuck.last_error or "")


def test_stage_error_is_recorded_and_retried_after_backoff(
    make_worker: MakeWorker, db: Path
) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.SELECTING] = RuntimeError("llm timeout")
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    failed = get_job(worker.conn, job.id)
    assert failed is not None
    assert failed.status is JobStatus.SELECTING
    assert failed.attempts == 1
    assert failed.last_error == "RuntimeError: llm timeout"

    del pipeline.fail_at[JobStatus.SELECTING]
    clock.now = T0 + timedelta(minutes=1.2)  # backoff(1) plus the most jitter
    _drain(worker)
    assert _status(db, job.id) is JobStatus.CLIPS_READY


def test_repeated_stage_errors_end_in_failed(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = RuntimeError("geo-blocked")
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    for _ in range(10):
        _drain(worker)
        clock.now += timedelta(hours=1)

    row = _row(db, job.id)
    assert row["status"] == JobStatus.FAILED.value
    assert pipeline.calls[JobStatus.FETCHING_AUDIO] == 5
    assert row["last_error"].startswith("gave up after 5 attempts: RuntimeError: geo-blocked")


@pytest.mark.parametrize(
    ("exc", "status", "column", "text"),
    [
        (SkipJob("too short"), JobStatus.SKIPPED, "skip_reason", "too short"),
        (PermanentError("private video"), JobStatus.FAILED, "last_error", "private video"),
    ],
)
def test_skip_and_permanent_errors_are_terminal(
    make_worker: MakeWorker,
    db: Path,
    exc: Exception,
    status: JobStatus,
    column: str,
    text: str,
) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = exc
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    row = _row(db, job.id)
    assert row["status"] == status.value
    assert text in row[column]


def test_unhandled_status_stays_put(make_worker: MakeWorker, db: Path) -> None:
    worker = make_worker()
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    assert worker.run_once()  # detected -> fetching_audio (built-in pass-through)
    assert not worker.run_once()  # no fetching_audio stage registered yet
    assert _status(db, job.id) is JobStatus.FETCHING_AUDIO


def test_interrupt_releases_only_the_claimed_job(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    pipeline.crash_at = JobStatus.FETCHING_AUDIO
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()  # detected -> fetching_audio
    other, _ = enqueue(worker.conn, SourceRef("twitch", "7"), T0, creator="m")
    _hard_kill(db, other.id, "w1")  # another claim under the same id must survive

    with pytest.raises(Crash):
        worker.run_once()

    assert _row(db, job.id)["worker_id"] is None
    assert _row(db, other.id)["worker_id"] == "w1"


def test_run_forever_stops_on_event(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    stop = threading.Event()
    pipeline.hooks[JobStatus.FETCHING_AUDIO] = stop.set

    worker.run_forever(stop, poll_seconds=0.01)

    assert _status(db, job.id) is JobStatus.TRANSCRIBING


def test_default_worker_ids_are_unique() -> None:
    assert default_worker_id() != default_worker_id()


def test_stage_def_rejects_escaping_artifact() -> None:
    for bad in ("/etc/passwd", "../x", "a/../../b", ""):
        with pytest.raises(ValueError, match="relative"):
            StageDef(lambda job, ctx: None, bad)


def test_build_stages_registers_the_pipeline_through_rendering() -> None:
    stages = build_stages()

    assert stages[JobStatus.DETECTED].artifact is None
    assert {status: stage.artifact for status, stage in stages.items() if stage.artifact} == {
        status: name for status, name in ARTIFACTS.items() if status is not JobStatus.RENDERING
    }
    # Rendering's progress lives in the `clips` table, not in an artifact file.
    assert stages[JobStatus.RENDERING].artifact is None
    assert JobStatus.WAITING_VOD not in stages


def test_stage_modules_do_not_depend_on_the_worker() -> None:
    import clipper.jobs.stages as stages_mod

    assert "clipper.jobs.worker" not in Path(stages_mod.__file__).read_text()


@pytest.mark.parametrize(
    ("exc", "column"),
    [
        (RuntimeError("GET https://api.example/v1?key=SECRET1 failed"), "last_error"),
        (PermanentError("bad token in https://x.test/?token=SECRET1"), "last_error"),
        (SkipJob("Authorization: Bearer SECRET1"), "skip_reason"),
    ],
)
def test_stage_error_text_is_redacted_in_db_and_logs(
    make_worker: MakeWorker, db: Path, caplog: pytest.LogCaptureFixture, exc: Exception, column: str
) -> None:
    caplog.set_level(logging.INFO)
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = exc
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    stored = _row(db, job.id)[column]
    assert stored and "SECRET1" not in stored and "REDACTED" in stored
    assert "SECRET1" not in caplog.text


def test_deferred_keeps_status_reschedules_and_does_not_consume_attempts(
    make_worker: MakeWorker, db: Path
) -> None:
    clock = FakeClock()
    pipeline = StubPipeline()
    worker = make_worker(pipeline, clock)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.run_once()  # detected -> fetching_audio

    # Far more deferrals than MAX_ATTEMPTS: the job must never be failed by them.
    for _ in range(8):
        until = clock.now + timedelta(hours=3)
        pipeline.fail_at[JobStatus.FETCHING_AUDIO] = Deferred(until, "daily budget reached")
        assert worker.run_once()
        row = _row(db, job.id)
        assert row["status"] == JobStatus.FETCHING_AUDIO.value
        assert row["attempts"] == 0
        assert row["worker_id"] is None and row["lease_until"] is None
        assert row["next_attempt_at"] == until.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert "deferred: daily budget reached" in row["last_error"]
        assert not worker.run_once()  # not due before `until`
        clock.now = until

    del pipeline.fail_at[JobStatus.FETCHING_AUDIO]
    _drain(worker)
    assert _status(db, job.id) is JobStatus.CLIPS_READY


def test_deferred_requires_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Deferred(datetime(2026, 1, 1), "x")


# --- failed-job alert (CLP-34) ----------------------------------------------------------


def test_job_out_of_attempts_sends_one_alert(make_worker: MakeWorker, db: Path) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = RuntimeError("geo-blocked")
    clock = FakeClock()
    worker = make_worker(pipeline, clock)
    alerts: list[str] = []
    worker.alert = alerts.append
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    for _ in range(10):
        _drain(worker)
        clock.now += timedelta(hours=1)

    assert _status(db, job.id) is JobStatus.FAILED
    assert alerts == [
        f"Job {job.id} failed (youtube_dQw4w9WgXcQ, manual) at fetching_audio: gave up after"
        f" 5 attempts: RuntimeError: geo-blocked. Run `clipper retry {job.id}` to try again."
    ]


@pytest.mark.parametrize(
    ("exc", "alerted"), [(PermanentError("private video"), True), (SkipJob("too short"), False)]
)
def test_permanent_failure_alerts_and_skip_does_not(
    make_worker: MakeWorker, exc: Exception, alerted: bool
) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = exc
    worker = make_worker(pipeline)
    alerts: list[str] = []
    worker.alert = alerts.append
    enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    assert [("PermanentError: private video" in a) for a in alerts] == ([True] if alerted else [])


def test_job_given_up_at_claim_alerts(make_worker: MakeWorker, db: Path) -> None:
    """Its workers kept dying mid-stage: the next claim marks it failed."""
    worker = make_worker()
    alerts: list[str] = []
    worker.alert = alerts.append
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")
    worker.conn.execute("UPDATE jobs SET attempts = 5 WHERE id = ?", (job.id,))

    assert not worker.run_once()

    assert _status(db, job.id) is JobStatus.FAILED
    assert alerts == [
        f"Job {job.id} failed (youtube_dQw4w9WgXcQ, manual) at detected: gave up after"
        " 5 attempts (worker died mid-stage)."
        f" Run `clipper retry {job.id}` to try again."
    ]


def test_alert_that_cannot_be_sent_is_logged(
    make_worker: MakeWorker, db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = StubPipeline()
    pipeline.fail_at[JobStatus.FETCHING_AUDIO] = PermanentError("private video")
    worker = make_worker(pipeline)

    def down(text: str) -> None:
        raise NotifyError("discord webhook returned 503")

    worker.alert = down
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    assert _status(db, job.id) is JobStatus.FAILED
    assert f"job {job.id}: failure alert not sent: discord webhook returned 503" in caplog.text


# --- CLP-143: stages by job kind ----------------------------------------------------------------


def test_a_worker_without_commentary_stages_never_touches_a_commentary_job(
    make_worker: MakeWorker, db: Path
) -> None:
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    with closing(connect(db)) as conn:
        job, _ = enqueue(conn, DAY, T0, creator="random_gaming_yt")

    assert worker.run_once() is False
    assert _status(db, job.id) is JobStatus.DETECTED
    assert sum(pipeline.calls.values()) == 0


def test_a_commentary_day_is_skipped_at_discovery_until_clp_144(
    make_worker: MakeWorker, db: Path
) -> None:
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    worker.commentary_stages = build_commentary_stages()
    alerts: list[str] = []
    worker.alert = alerts.append
    with closing(connect(db)) as conn:
        job, _ = enqueue(conn, DAY, T0, creator="random_gaming_yt")

    assert _drain(worker) == 2  # detected -> discovering, then the placeholder skips

    row = _row(db, job.id)
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "commentary discovery is not built yet (CLP-144)"
    assert sum(pipeline.calls.values()) == 0  # no clip stage ran
    assert alerts == []  # a skip is not a failure


@pytest.mark.parametrize(
    "status", [s for s in COMMENTARY_PIPELINE if s not in TERMINAL and s is not JobStatus.DETECTED]
)
def test_every_commentary_status_has_a_placeholder_that_skips(
    make_worker: MakeWorker, db: Path, status: JobStatus
) -> None:
    pipeline = StubPipeline()
    worker = make_worker(pipeline)
    worker.commentary_stages = build_commentary_stages()
    with closing(connect(db)) as conn:
        job, _ = enqueue(conn, DAY, T0, creator="random_gaming_yt")
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job.id))

    assert worker.run_once()

    row = _row(db, job.id)
    assert row["status"] == "skipped"
    assert row["skip_reason"].startswith("commentary ")
    assert "is not built yet (CLP-14" in row["skip_reason"]
    assert sum(pipeline.calls.values()) == 0  # transcribing/rendering: not the clip stage


def test_build_commentary_stages_covers_the_commentary_chain_only() -> None:
    stages = build_commentary_stages()

    assert set(stages) == set(COMMENTARY_PIPELINE) - TERMINAL
    assert stages[JobStatus.DETECTED].artifact is None
    assert stages[JobStatus.TRANSCRIBING] is not build_stages()[JobStatus.TRANSCRIBING]


def test_start_log_lists_unhandled_statuses_per_kind(
    make_worker: MakeWorker, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    worker = make_worker(StubPipeline())
    stop = threading.Event()
    stop.set()

    worker.run_forever(stop, poll_seconds=0.01)

    assert (
        "not handling clip: waiting_vod; commentary: detected, discovering, fetching_sources,"
        " transcribing, scripting, voicing, rendering"
    ) in caplog.text
