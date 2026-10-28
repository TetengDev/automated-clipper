"""Worker stage `posting` (CLP-92): publish due posts, back off, fail after 3 attempts with
an alert, and never double-post after a crash mid-upload."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.config import AppConfig
from clipper.jobs import Worker
from clipper.posting import repo
from clipper.posting.models import MAX_ATTEMPTS, Post, PostStatus, PublishResult
from clipper.posting.stage import BACKOFF_BASE, STALE_UPLOAD, PostingStage
from posting_support import FakePublisher, LookupPublisher
from review_support import Env, make_env

T0 = datetime(2027, 1, 15, 1, 0, tzinfo=UTC)


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


def make_post(
    env: Env,
    *,
    platform: str = "tiktok",
    account: str = "tt_main",
    at: datetime = T0,
    write_file: bool = True,
) -> Post:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    if write_file:
        target = env.media_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake mp4")
    clip_id = env.clip(
        job,
        status="approved",
        video_path=rel,
        title="Chair falls",
        caption="He did not see it coming",
        hashtags=["funny", "#fail"],
    )
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=at,
        video_path=rel,
        now=T0 - timedelta(hours=1),
    )


def stage(env: Env, clock: Clock, publishers: dict, **kwargs: object) -> PostingStage:
    return PostingStage(
        conn=env.conn,
        publishers=publishers,
        worker_id="w1",
        media_root=env.media_root,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


def status(env: Env, post_id: int) -> PostStatus:
    post = repo.get_post(env.conn, post_id)
    assert post is not None
    return post.status


# --- happy path --------------------------------------------------------------------------


def test_due_post_is_published_and_recorded(env: Env, clock: Clock) -> None:
    post = make_post(env)
    publisher = FakePublisher()

    handled = stage(env, clock, {"tiktok": publisher}).run_once()

    assert handled == 1
    [(sent, path, meta)] = publisher.calls
    assert sent.id == post.id and sent.status == PostStatus.UPLOADING
    assert path == (env.media_root / post.video_path).resolve()
    assert meta.title == "Chair falls"
    assert meta.caption == "He did not see it coming"
    assert meta.hashtags == ["#funny", "#fail"]
    assert meta.flags == {}
    done = repo.get_post(env.conn, post.id)
    assert done is not None
    assert done.status == PostStatus.POSTED
    assert done.external_id == f"ext-{post.id}"
    assert done.url == f"https://example.test/p/{post.id}"
    assert done.posted_at == T0
    actions = [r["action"] for r in env.reviews(post.clip_id)]
    assert actions == ["post_uploading", "post_posted"]


def test_post_not_yet_due_is_left_alone(env: Env, clock: Clock) -> None:
    post = make_post(env, at=T0 + timedelta(minutes=1))
    publisher = FakePublisher()

    assert stage(env, clock, {"tiktok": publisher}).run_once() == 0
    assert publisher.calls == []
    assert status(env, post.id) == PostStatus.SCHEDULED


def test_cancelled_post_is_never_published(env: Env, clock: Clock) -> None:
    post = make_post(env)
    repo.cancel(env.conn, post.id, T0)
    publisher = FakePublisher()

    stage(env, clock, {"tiktok": publisher}).run_once()

    assert publisher.calls == []


def test_a_claimed_post_cannot_be_claimed_again(env: Env) -> None:
    make_post(env)

    first = repo.claim_due(env.conn, T0, "w1", platforms={"tiktok"})
    second = repo.claim_due(env.conn, T0, "w2", platforms={"tiktok"})

    assert len(first) == 1 and second == []
    assert first[0].worker_id == "w1"


# --- no publisher ------------------------------------------------------------------------


def test_without_a_publisher_due_posts_stay_scheduled_and_warn_once(
    env: Env, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    first = make_post(env)
    second = make_post(env, account="tt_alt")
    yt = make_post(env, platform="youtube", account="yt_main")
    posting = stage(env, clock, {})

    with caplog.at_level(logging.WARNING):
        posting.run_once()
        clock.now += timedelta(minutes=1)
        posting.run_once()

    assert {status(env, p.id) for p in (first, second, yt)} == {PostStatus.SCHEDULED}
    warnings = [r.getMessage() for r in caplog.records if "no publisher" in r.getMessage()]
    assert len(warnings) == 2
    assert any("tiktok" in w for w in warnings) and any("youtube" in w for w in warnings)


# --- failures, backoff, alert ------------------------------------------------------------


def test_failure_backs_off_and_counts_an_attempt(env: Env, clock: Clock) -> None:
    post = make_post(env)

    stage(env, clock, {"tiktok": FakePublisher(fail=1)}).run_once()

    failed = repo.get_post(env.conn, post.id)
    assert failed is not None
    assert failed.status == PostStatus.SCHEDULED
    assert failed.attempts == 1
    assert failed.scheduled_at == T0 + BACKOFF_BASE * 2
    assert failed.error is not None and "rate limited" in failed.error
    assert failed.worker_id is None


def test_three_failures_mark_post_failed_and_alert(env: Env, clock: Clock) -> None:
    post = make_post(env)
    alerts: list[str] = []
    publisher = FakePublisher(fail=MAX_ATTEMPTS)
    posting = stage(env, clock, {"tiktok": publisher}, alert=alerts.append)

    for _ in range(MAX_ATTEMPTS):
        posting.run_once()
        clock.now += timedelta(hours=1)

    assert MAX_ATTEMPTS == 3
    assert len(publisher.calls) == 3
    failed = repo.get_post(env.conn, post.id)
    assert failed is not None
    assert failed.status == PostStatus.POST_FAILED
    assert failed.attempts == 3
    assert len(alerts) == 1
    assert f"post {post.id}" in alerts[0] and "tiktok" in alerts[0]
    assert "post_failed" in [r["action"] for r in env.reviews(post.clip_id)]


def test_backoff_doubles_each_attempt(env: Env, clock: Clock) -> None:
    post = make_post(env)
    posting = stage(env, clock, {"tiktok": FakePublisher(fail=2)})

    posting.run_once()
    clock.now = T0 + timedelta(hours=1)
    posting.run_once()

    again = repo.get_post(env.conn, post.id)
    assert again is not None
    assert again.scheduled_at == clock.now + BACKOFF_BASE * 4


def test_default_alert_logs_an_error(
    env: Env, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    post = make_post(env)
    env.conn.execute("UPDATE posts SET attempts = 2 WHERE id = ?", (post.id,))

    stage(env, clock, {"tiktok": FakePublisher(fail=1)}).run_once()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(f"post {post.id}" in r.getMessage() for r in errors)


def test_missing_video_file_counts_as_a_failed_attempt(env: Env, clock: Clock) -> None:
    post = make_post(env, write_file=False)
    publisher = FakePublisher()

    stage(env, clock, {"tiktok": publisher}).run_once()

    assert publisher.calls == []
    failed = repo.get_post(env.conn, post.id)
    assert failed is not None and failed.attempts == 1
    assert failed.error is not None and "missing" in failed.error


def test_failure_errors_are_redacted(env: Env, clock: Clock) -> None:
    @dataclass
    class Leaky:
        def publish(self, *_: object) -> PublishResult:
            raise RuntimeError("POST https://api.test/upload?access_token=abc123secret failed")

    post = make_post(env)

    stage(env, clock, {"tiktok": Leaky()}).run_once()

    failed = repo.get_post(env.conn, post.id)
    assert failed is not None and failed.error is not None
    assert "abc123secret" not in failed.error


def test_retry_requeues_a_failed_post(env: Env, clock: Clock) -> None:
    post = make_post(env)
    posting = stage(env, clock, {"tiktok": FakePublisher(fail=MAX_ATTEMPTS)}, alert=lambda _: None)
    for _ in range(MAX_ATTEMPTS):
        posting.run_once()
        clock.now += timedelta(hours=1)
    assert status(env, post.id) == PostStatus.POST_FAILED

    repo.retry(env.conn, post.id, clock.now)
    posting.run_once()

    assert status(env, post.id) == PostStatus.POSTED


# --- crash mid-upload --------------------------------------------------------------------


def stuck(env: Env, post: Post, since: datetime, worker: str = "dead") -> None:
    env.conn.execute(
        "UPDATE posts SET status = 'uploading', worker_id = ?, claimed_at = ? WHERE id = ?",
        (worker, since.strftime("%Y-%m-%dT%H:%M:%SZ"), post.id),
    )


def test_stale_upload_found_by_lookup_is_marked_posted_without_reposting(
    env: Env, clock: Clock
) -> None:
    post = make_post(env)
    stuck(env, post, T0 - STALE_UPLOAD - timedelta(minutes=1))
    publisher = LookupPublisher(
        found={post.idempotency_key: PublishResult(external_id="ext-9", url=None)}
    )

    stage(env, clock, {"tiktok": publisher}).run_once()

    assert publisher.lookups == [post.idempotency_key]
    assert publisher.calls == []
    done = repo.get_post(env.conn, post.id)
    assert done is not None
    assert done.status == PostStatus.POSTED and done.external_id == "ext-9"


def test_stale_upload_not_found_is_retried_later(env: Env, clock: Clock) -> None:
    post = make_post(env)
    stuck(env, post, T0 - STALE_UPLOAD - timedelta(minutes=1))
    publisher = LookupPublisher()

    stage(env, clock, {"tiktok": publisher}).run_once()

    assert publisher.calls == []  # not in this pass: it backs off first
    again = repo.get_post(env.conn, post.id)
    assert again is not None
    assert again.status == PostStatus.SCHEDULED and again.attempts == 1


def test_stale_upload_without_lookup_fails_rather_than_double_posting(
    env: Env, clock: Clock
) -> None:
    post = make_post(env)
    stuck(env, post, T0 - STALE_UPLOAD - timedelta(minutes=1))
    publisher = FakePublisher()
    alerts: list[str] = []

    stage(env, clock, {"tiktok": publisher}, alert=alerts.append).run_once()

    assert publisher.calls == []
    failed = repo.get_post(env.conn, post.id)
    assert failed is not None
    assert failed.status == PostStatus.POST_FAILED
    assert failed.error == "uploading state unresolved"
    assert len(alerts) == 1


def test_recent_upload_is_left_to_its_worker(env: Env, clock: Clock) -> None:
    post = make_post(env)
    stuck(env, post, T0 - timedelta(minutes=5))
    publisher = LookupPublisher()

    stage(env, clock, {"tiktok": publisher}).run_once()

    assert publisher.lookups == [] and publisher.calls == []
    assert status(env, post.id) == PostStatus.UPLOADING


def test_result_write_failure_leaves_post_uploading_for_recovery(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = make_post(env)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "mark_posted", boom)
    publisher = FakePublisher()

    with pytest.raises(RuntimeError):
        stage(env, clock, {"tiktok": publisher}).run_once()

    assert len(publisher.calls) == 1
    assert status(env, post.id) == PostStatus.UPLOADING  # never re-published blindly


# --- worker loop -------------------------------------------------------------------------


def test_worker_loop_runs_the_posting_pass(env: Env) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class Pass:
        def run_once(self) -> int:
            calls.append(1)
            stop.set()
            return 0

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())

    worker.run_forever(stop, poll_seconds=0.01)

    assert calls == [1]


def test_worker_loop_survives_a_posting_error(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class Pass:
        def run_once(self) -> int:
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
                return 0
            raise RuntimeError("publisher exploded")

    worker = Worker(conn=env.conn, config=AppConfig(), worker_id="w1", stages={}, posting=Pass())

    worker.run_forever(stop, poll_seconds=0.01)

    assert len(calls) == 2
    assert "posting pass failed" in caplog.text
