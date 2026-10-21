import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.config import AppConfig
from clipper.db import connect, migrate
from clipper.jobs import (
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
from clipper.jobs import worker as worker_mod
from clipper.jobs.queue import DEFAULT_LEASE
from clipper.jobs.stages import DEFAULT_STAGES
from clipper.pipeline import build_stages
from clipper.urls import SourceRef

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
YT = SourceRef("youtube", "dQw4w9WgXcQ")
ARTIFACTS = {
    JobStatus.FETCHING_AUDIO: "audio.m4a",
    JobStatus.TRANSCRIBING: "transcript.json",
    JobStatus.SELECTING: "selection.json",
    JobStatus.FETCHING_CLIPS: "clips/sources.done",
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
    (media / "audio.m4a").write_text("already here")
    worker = make_worker(pipeline)
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    assert pipeline.calls[JobStatus.FETCHING_AUDIO] == 0
    assert (media / "audio.m4a").read_text() == "already here"
    assert _status(db, job.id) is JobStatus.CLIPS_READY


def test_directory_does_not_count_as_artifact(make_worker: MakeWorker, tmp_path: Path) -> None:
    worker = make_worker(StubPipeline())
    worker.stages = {
        **worker.stages,
        JobStatus.FETCHING_AUDIO: StageDef(
            lambda job, ctx: (ctx.media_dir / "audio.m4a").mkdir(), "audio.m4a"
        ),
    }
    job, _ = enqueue(worker.conn, YT, T0, creator="manual")

    _drain(worker)

    stuck = get_job(worker.conn, job.id)
    assert stuck is not None and stuck.status is JobStatus.FETCHING_AUDIO
    assert "without producing audio.m4a" in (stuck.last_error or "")


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
    clock.now = T0 + timedelta(minutes=1)
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


def test_build_stages_registers_the_detected_pass_through() -> None:
    stages = build_stages()

    assert set(stages) == {JobStatus.DETECTED}
    assert stages[JobStatus.DETECTED].artifact is None


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
