"""Posting stage robustness (CLP-105): an upload heartbeat, dead-worker post recovery and
a posting loop of its own, next to the job stages."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.config import AppConfig
from clipper.db import connect
from clipper.jobs import Job, JobStatus, StageContext, StageDef, Worker, enqueue
from clipper.posting import repo
from clipper.posting.models import Post, PostStatus, PublishResult
from clipper.posting.stage import MAX_UPLOAD, RECOVER_QUIET, STALE_UPLOAD, PostingStage
from clipper.urls import SourceRef
from posting_support import LookupPublisher
from review_support import Env, make_env

T0 = datetime(2027, 1, 15, 1, 0, tzinfo=UTC)
WAIT = 5.0  # seconds a test waits for a background thread before failing


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_post(env: Env, *, account: str = "tt_main", at: datetime = T0) -> Post:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    target = env.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake mp4")
    clip_id = env.clip(job, status="approved", video_path=rel, title="t", caption="c")
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform="tiktok",
        scheduled_at=at,
        video_path=rel,
        now=T0 - timedelta(hours=1),
    )


def stage(
    env: Env,
    clock: Clock,
    publisher: object,
    *,
    worker_id: str = "w1",
    conn: sqlite3.Connection | None = None,
    **kwargs: object,
) -> PostingStage:
    return PostingStage(
        conn=conn or env.conn,
        publishers={"tiktok": publisher},  # type: ignore[dict-item]
        worker_id=worker_id,
        media_root=env.media_root,
        clock=clock,
        connect=lambda: connect(env.db_path),
        heartbeat_seconds=0.01,
        **kwargs,  # type: ignore[arg-type]
    )


def fresh(env: Env, post_id: int) -> Post:
    with closing(connect(env.db_path)) as conn:
        post = repo.get_post(conn, post_id)
    assert post is not None
    return post


def wait_until(check: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.005)
    return False


# --- AC1: an upload that is still alive is never taken over -------------------------------


def test_slow_live_upload_is_not_taken_over_and_is_recorded(env: Env, clock: Clock) -> None:
    post = make_post(env)
    other: list[int] = []

    @dataclass
    class Slow(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            # 45 minutes of upload: the heartbeat keeps the claim fresh meanwhile.
            for _ in range(3):
                clock.now += timedelta(minutes=15)
                assert wait_until(lambda: fresh(env, sent.id).claimed_at == clock.now)
            with closing(connect(env.db_path)) as conn2:
                rival = stage(env, clock, LookupPublisher(), worker_id="w2", conn=conn2)
                other.append(rival.run_once())
            return PublishResult(external_id="slow-ok")

    stage(env, clock, Slow()).run_once()

    assert clock.now - T0 > STALE_UPLOAD
    assert other == [0]  # the rival worker found nothing stale
    done = fresh(env, post.id)
    assert done.status == PostStatus.POSTED and done.external_id == "slow-ok"
    actions = [r["action"] for r in env.reviews(post.clip_id)]
    assert "post_recovered" not in actions


def test_heartbeat_stops_when_the_upload_ends(env: Env, clock: Clock) -> None:
    post = make_post(env)

    stage(env, clock, LookupPublisher()).run_once()
    clock.now += timedelta(hours=1)
    time.sleep(0.05)

    assert fresh(env, post.id).claimed_at == T0  # nothing refreshed it after the upload


def test_heartbeat_never_revives_a_post_another_worker_took(env: Env, clock: Clock) -> None:
    post = make_post(env)

    @dataclass
    class TakenOver(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            clock.now += timedelta(minutes=1)
            assert wait_until(lambda: fresh(env, sent.id).claimed_at == clock.now)  # beating
            with closing(connect(env.db_path)) as conn2:
                conn2.execute(
                    "UPDATE posts SET worker_id = 'w2', claimed_at = '2027-01-15T00:00:00Z'"
                    " WHERE id = ?",
                    (sent.id,),
                )
            clock.now += timedelta(minutes=5)
            time.sleep(0.05)
            return PublishResult(external_id="x")

    stage(env, clock, TakenOver(), alert=lambda _: None).run_once()

    taken = fresh(env, post.id)
    assert taken.worker_id == "w2"
    assert taken.claimed_at == datetime(2027, 1, 15, 0, 0, tzinfo=UTC)


def test_heartbeat_gives_up_after_max_upload(env: Env, clock: Clock) -> None:
    # A publish that hangs for ever must not hold its post for ever: past MAX_UPLOAD the
    # heartbeat stops and the stale pass resolves the post 30 minutes later.
    post = make_post(env)

    @dataclass
    class Hung(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            clock.now += timedelta(hours=1)
            assert wait_until(lambda: fresh(env, sent.id).claimed_at == clock.now)  # beating
            clock.now += MAX_UPLOAD
            time.sleep(0.05)
            frozen = fresh(env, sent.id).claimed_at
            assert frozen is not None and frozen < clock.now
            clock.now += timedelta(minutes=10)
            time.sleep(0.05)
            assert fresh(env, sent.id).claimed_at == frozen
            raise RuntimeError("timed out")

    stage(env, clock, Hung(), alert=lambda _: None).run_once()

    assert fresh(env, post.id).status == PostStatus.SCHEDULED  # a normal failed attempt


def test_heartbeat_db_errors_never_break_the_upload(env: Env, clock: Clock) -> None:
    post = make_post(env)

    def broken() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    @dataclass
    class Slow(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            time.sleep(0.05)
            return PublishResult(external_id="ok")

    posting = stage(env, clock, Slow())
    posting.connect = broken
    posting.run_once()

    assert fresh(env, post.id).status == PostStatus.POSTED


def test_heartbeat_survives_a_failing_claim_write_and_keeps_beating(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = make_post(env)
    real = repo.touch_claim
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky(*args: object, **kwargs: object) -> bool:
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "touch_claim", flaky)

    @dataclass
    class Slow(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            clock.now += timedelta(minutes=40)
            assert wait_until(lambda: fresh(env, sent.id).claimed_at == clock.now)
            return PublishResult(external_id="ok")

    stage(env, clock, Slow()).run_once()

    assert failures == []
    assert fresh(env, post.id).status == PostStatus.POSTED


def test_stale_lookups_keep_their_claims_fresh(env: Env, clock: Clock) -> None:
    post = make_post(env)
    uploading(env, post, "dead", T0 - timedelta(hours=1))

    @dataclass
    class SlowLookup(LookupPublisher):
        def lookup(self, idempotency_key: str) -> PublishResult | None:
            clock.now += timedelta(minutes=40)
            assert wait_until(lambda: fresh(env, post.id).claimed_at == clock.now)
            return PublishResult(external_id="found")

    stage(env, clock, SlowLookup()).run_once()

    assert fresh(env, post.id).status == PostStatus.POSTED


def test_a_stopping_pass_claims_no_new_post(env: Env, clock: Clock) -> None:
    first = make_post(env)
    second = make_post(env, account="tt_alt")
    stop = threading.Event()

    @dataclass
    class StopAfterOne(LookupPublisher):
        def publish(self, sent: Post, *_: object) -> PublishResult:
            stop.set()  # shutdown arrives mid-upload: this one finishes, no other starts
            return PublishResult(external_id=f"x{sent.id}")

    stage(env, clock, StopAfterOne(), stop=stop).run_once()

    assert fresh(env, first.id).status == PostStatus.POSTED
    assert fresh(env, second.id).status == PostStatus.SCHEDULED


def test_without_a_connection_factory_publish_still_works(env: Env, clock: Clock) -> None:
    post = make_post(env)
    posting = PostingStage(
        conn=env.conn,
        publishers={"tiktok": LookupPublisher()},
        worker_id="w1",
        media_root=env.media_root,
        clock=clock,
    )

    posting.run_once()

    assert fresh(env, post.id).status == PostStatus.POSTED


def test_touch_claim_only_refreshes_the_owners_upload(env: Env) -> None:
    post = make_post(env)
    [claimed] = repo.claim_due(env.conn, T0, "w1", platforms={"tiktok"})
    later = T0 + timedelta(minutes=20)

    assert not repo.touch_claim(env.conn, claimed.id, "w2", later)
    assert repo.touch_claim(env.conn, claimed.id, "w1", later)
    assert fresh(env, post.id).claimed_at == later
    repo.mark_posted(env.conn, claimed, "w1", PublishResult(external_id="e"), later)
    assert not repo.touch_claim(env.conn, claimed.id, "w1", later + timedelta(minutes=1))


# --- AC2: --recover-id resolves the dead worker's uploads in the next pass ----------------


def uploading(env: Env, post: Post, worker: str, since: datetime) -> None:
    env.conn.execute(
        "UPDATE posts SET status = 'uploading', worker_id = ?, claimed_at = ? WHERE id = ?",
        (worker, since.strftime("%Y-%m-%dT%H:%M:%SZ"), post.id),
    )


def test_recover_hands_dead_workers_uploads_to_the_next_pass(env: Env, clock: Clock) -> None:
    dead = make_post(env)
    alive = make_post(env, account="tt_alt")
    # Quiet for 5 minutes: well past three missed heartbeats.
    uploading(env, dead, "dead", T0 - timedelta(minutes=5))
    uploading(env, alive, "alive", T0 - timedelta(minutes=5))
    publisher = LookupPublisher(
        found={dead.idempotency_key: PublishResult(external_id="was-live", url=None)}
    )
    posting = stage(env, clock, publisher)
    worker = Worker(
        conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, clock=clock, posting=posting
    )

    assert worker.recover("dead") == 0  # no jobs; the post count is logged separately
    posting.run_once()

    assert publisher.lookups == [dead.idempotency_key]
    assert publisher.calls == []
    done = fresh(env, dead.id)
    assert done.status == PostStatus.POSTED and done.external_id == "was-live"
    untouched = fresh(env, alive.id)
    assert untouched.status == PostStatus.UPLOADING and untouched.worker_id == "alive"
    actions = [r["action"] for r in env.reviews(dead.clip_id)]
    assert actions[-3:] == ["post_released", "post_recovered", "post_posted"]


def test_release_uploads_counts_only_the_dead_workers_uploads(env: Env) -> None:
    dead = make_post(env)
    other = make_post(env, account="tt_alt")
    scheduled = make_post(env, account="tt_third")
    uploading(env, dead, "dead", T0)
    uploading(env, other, "alive", T0)
    env.conn.execute("UPDATE posts SET worker_id = 'dead' WHERE id = ?", (scheduled.id,))

    later = T0 + timedelta(minutes=10)
    released = repo.release_uploads(
        env.conn, "dead", later, quiet_for=timedelta(minutes=3), stale_after=STALE_UPLOAD
    )
    assert released == (1, 0)
    assert fresh(env, dead.id).claimed_at is None
    assert fresh(env, other.id).claimed_at == T0
    assert fresh(env, scheduled.id).status == PostStatus.SCHEDULED


def test_released_upload_is_not_retaken_once_its_worker_heartbeats(env: Env, clock: Clock) -> None:
    # A worker wrongly reported dead keeps its post: its next heartbeat re-stamps the claim
    # before any stale pass ran, so nobody takes the upload over.
    post = make_post(env)
    [claimed] = repo.claim_due(env.conn, T0, "w1", platforms={"tiktok"})
    repo.release_uploads(
        env.conn, "w1", T0 + timedelta(minutes=10), quiet_for=timedelta(0), stale_after=STALE_UPLOAD
    )

    assert repo.touch_claim(env.conn, claimed.id, "w1", T0 + timedelta(minutes=11))
    rival = stage(env, clock, LookupPublisher(), worker_id="w2")
    assert rival.run_once() == 0
    assert fresh(env, post.id).worker_id == "w1"


def test_recover_never_releases_an_upload_that_is_still_heartbeating(
    env: Env, clock: Clock
) -> None:
    # An operator who names a live worker must not get its upload taken over: a claim
    # re-stamped within RECOVER_QUIET is not released, only set to go stale RECOVER_QUIET
    # from now, which a live worker's next heartbeat undoes.
    post = make_post(env)
    uploading(env, post, "busy", T0 - timedelta(minutes=1))
    posting = stage(env, clock, LookupPublisher())

    assert posting.recover("busy") == 0
    assert posting.run_once() == 0
    left = fresh(env, post.id)
    assert left.worker_id == "busy" and left.status == PostStatus.UPLOADING
    assert left.claimed_at == T0 - STALE_UPLOAD + RECOVER_QUIET
    assert repo.touch_claim(env.conn, post.id, "busy", T0 + timedelta(minutes=1))
    clock.now = T0 + RECOVER_QUIET
    assert posting.run_once() == 0  # alive: re-stamped, so still not stale
    assert fresh(env, post.id).worker_id == "busy"


def test_recover_of_a_fresh_crash_resolves_it_after_recover_quiet(env: Env, clock: Clock) -> None:
    # Restarted right after the crash: the last beat is recent, so the post is not released
    # at once, but it goes stale RECOVER_QUIET later, not STALE_UPLOAD later.
    post = make_post(env)
    uploading(env, post, "dead", T0 - timedelta(seconds=30))
    publisher = LookupPublisher(found={post.idempotency_key: PublishResult(external_id="e")})
    posting = stage(env, clock, publisher)

    posting.recover("dead")
    clock.now = T0 + RECOVER_QUIET - timedelta(seconds=1)
    assert posting.run_once() == 0
    clock.now = T0 + RECOVER_QUIET

    assert posting.run_once() == 1
    assert fresh(env, post.id).status == PostStatus.POSTED


def test_unresolved_upload_alert_says_to_check_the_platform_first(env: Env, clock: Clock) -> None:
    post = make_post(env)
    uploading(env, post, "dead", T0 - timedelta(hours=1))
    alerts: list[str] = []

    stage(env, clock, LookupPublisher(), alert=alerts.append).run_once()

    [alert] = alerts
    assert f"post {post.id}" in alert
    assert "check the platform" in alert and "before" in alert


def test_worker_recover_without_a_posting_stage_still_releases_jobs(env: Env) -> None:
    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={})

    assert worker.recover("dead") == 0


# --- AC3: posting runs on its own loop, not between job stages -----------------------------


def test_due_post_is_attempted_while_a_long_job_stage_runs(env: Env) -> None:
    stop = threading.Event()
    stage_started = threading.Event()
    stage_release = threading.Event()
    posted_during_stage: list[bool] = []

    def long_stage(job: Job, ctx: StageContext) -> None:
        stage_started.set()
        stage_release.wait(WAIT)

    class Pass:
        def run_once(self) -> int:
            if stage_started.is_set() and not stage_release.is_set():
                posted_during_stage.append(True)
                stage_release.set()
                stop.set()
            return 0

    enqueue(env.conn, SourceRef("youtube", "dQw4w9WgXcQ"), datetime.now(UTC), creator="manual")
    # The worker loop runs on a test thread here, so it gets a connection it may use there.
    worker_conn = connect(env.db_path, check_same_thread=False)
    worker = Worker(
        conn=worker_conn,
        config=AppConfig(),
        worker_id="w1",
        stages={JobStatus.DETECTED: StageDef(long_stage)},
        media_root=env.media_root,
        posting=Pass(),
    )

    runner = threading.Thread(target=worker.run_forever, args=(stop, 0.01))
    runner.start()
    runner.join(WAIT * 2)
    stop.set()
    stage_release.set()
    runner.join(WAIT)
    worker_conn.close()

    assert posted_during_stage == [True]


def test_stopping_the_worker_stops_the_posting_loop(env: Env) -> None:
    stop = threading.Event()
    passes: list[int] = []

    class Pass:
        def run_once(self) -> int:
            passes.append(1)
            return 0

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())
    timer = threading.Timer(0.1, stop.set)
    timer.start()

    worker.run_forever(stop, poll_seconds=0.01)
    seen = len(passes)
    time.sleep(0.05)

    assert seen >= 1
    assert len(passes) == seen  # no pass after run_forever returned
    assert not any(t.name == "posting" for t in threading.enumerate())


def test_posting_loop_survives_errors(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class Pass:
        def run_once(self) -> int:
            calls.append(1)
            if len(calls) >= 3:
                stop.set()
                return 0
            raise RuntimeError("publisher exploded")

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())

    worker.run_forever(stop, poll_seconds=0.01)

    assert len(calls) == 3
    assert "posting pass failed" in caplog.text


def test_run_worker_recover_id_releases_the_dead_workers_uploads(
    env: Env, config_file: Path
) -> None:
    from typer.testing import CliRunner

    from clipper.cli import app

    post = make_post(env)
    uploading(env, post, "dead", datetime(2020, 1, 1, tzinfo=UTC))  # long quiet (real clock)
    opts = ["--db", str(env.db_path), "--config", str(config_file)]

    result = CliRunner().invoke(app, [*opts, "run-worker", "--once", "--recover-id", "dead"])

    assert result.exit_code == 0, result.output
    released = fresh(env, post.id)
    assert released.status == PostStatus.UPLOADING and released.claimed_at is None


def test_run_worker_refuses_a_recover_id_with_control_characters(
    env: Env, config_file: Path
) -> None:
    from typer.testing import CliRunner

    from clipper.cli import app

    opts = ["--db", str(env.db_path), "--config", str(config_file)]

    result = CliRunner().invoke(
        app, [*opts, "run-worker", "--once", "--recover-id", "dead\nALERT: forged"]
    )

    assert result.exit_code != 0
    assert "recover-id" in result.output


class Crash(BaseException):
    """Stands in for Ctrl-C reaching the job loop."""


def test_interrupt_waits_briefly_for_the_posting_pass(env: Env) -> None:
    stop = threading.Event()
    in_pass = threading.Event()

    class Pass:
        def run_once(self) -> int:
            in_pass.set()
            time.sleep(0.1)
            return 0

    def crash(job: Job, ctx: StageContext) -> None:
        assert in_pass.wait(WAIT)
        raise Crash

    enqueue(env.conn, SourceRef("youtube", "dQw4w9WgXcQ"), datetime.now(UTC), creator="manual")
    worker = Worker(
        conn=env.conn,
        config=AppConfig(),
        worker_id="w1",
        stages={JobStatus.DETECTED: StageDef(crash)},
        media_root=env.media_root,
        posting=Pass(),
    )

    with pytest.raises(Crash):
        worker.run_forever(stop, poll_seconds=0.01)

    assert stop.is_set()
    assert not worker.posting_running  # its connection is safe to close now


class Fatal(BaseException):
    """A BaseException that is neither an interrupt nor an exit."""


def test_posting_loop_survives_a_base_exception(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class Pass:
        def run_once(self) -> int:
            calls.append(1)
            if len(calls) == 1:
                raise Fatal
            stop.set()
            return 0

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())

    worker.run_forever(stop, poll_seconds=0.01)

    assert len(calls) == 2
    assert "posting pass failed" in caplog.text


# pytest reports the SystemExit that ends the thread; plain threading ignores it.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_dead_posting_thread_is_restarted_after_a_backoff(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()
    calls: list[float] = []

    class Pass:
        def run_once(self) -> int:
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise SystemExit  # ends the thread (threading ignores SystemExit)
            stop.set()
            return 0

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())

    worker.run_forever(stop, poll_seconds=0.05)

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.05  # not restarted in a tight loop
    assert "posting thread died" in caplog.text


def test_recover_quiet_allows_for_clock_skew_between_hosts() -> None:
    from clipper.posting.stage import HEARTBEAT, RECOVER_QUIET

    assert RECOVER_QUIET >= HEARTBEAT * 5


def test_worker_wires_its_stop_event_into_the_posting_stage(env: Env, clock: Clock) -> None:
    stop = threading.Event()
    posting = stage(env, clock, LookupPublisher())
    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=posting)
    stop.set()

    worker.run_forever(stop, poll_seconds=0.01)

    assert posting.stop is stop


def test_an_interrupt_leaves_a_still_busy_posting_thread_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipper.jobs import worker as worker_mod

    monkeypatch.setattr(worker_mod, "INTERRUPT_JOIN_SECONDS", 0.01)
    stop = threading.Event()
    in_pass = threading.Event()
    release = threading.Event()

    class Pass:
        def run_once(self) -> int:
            in_pass.set()
            release.wait(WAIT)
            return 0

    def crash(job: Job, ctx: StageContext) -> None:
        assert in_pass.wait(WAIT)
        raise Crash

    enqueue(env.conn, SourceRef("youtube", "dQw4w9WgXcQ"), datetime.now(UTC), creator="manual")
    worker = Worker(
        conn=env.conn,
        config=AppConfig(),
        worker_id="w1",
        stages={JobStatus.DETECTED: StageDef(crash)},
        media_root=env.media_root,
        posting=Pass(),
    )

    with pytest.raises(Crash):
        worker.run_forever(stop, poll_seconds=0.01)

    assert worker.posting_running  # so the CLI must not close its connection
    release.set()
    assert wait_until(lambda: not worker.posting_running)


def test_stale_posts_are_claimed_one_at_a_time(env: Env, clock: Clock) -> None:
    # A post waiting behind a slow lookup is not claimed (and ageing) meanwhile.
    first = make_post(env)
    second = make_post(env, account="tt_alt")
    uploading(env, first, "dead", T0 - timedelta(hours=2))
    uploading(env, second, "dead", T0 - timedelta(hours=1))
    seen: list[str | None] = []

    @dataclass
    class Watching(LookupPublisher):
        def lookup(self, idempotency_key: str) -> PublishResult | None:
            if idempotency_key == first.idempotency_key:
                seen.append(fresh(env, second.id).worker_id)
            return PublishResult(external_id=idempotency_key[:6])

    stage(env, clock, Watching()).run_once()

    assert seen == ["dead"]
    assert fresh(env, second.id).status == PostStatus.POSTED


def test_connect_accepts_a_shorter_busy_timeout(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "x.db", busy_timeout_ms=1500)) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1500
