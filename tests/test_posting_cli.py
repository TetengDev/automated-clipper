"""`clipper posts list|cancel|reschedule|retry` (CLP-92)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from clipper.cli import app
from clipper.posting import repo
from clipper.posting.models import Post, PostStatus
from review_support import Env, make_env

runner = CliRunner()
# The CLI uses the real clock: keep test times a few days ahead of it.
AT = (datetime.now(UTC) + timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
LATER = AT + timedelta(days=2)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def make_post(env: Env, account: str = "tt_main", platform: str = "tiktok") -> Post:
    clip_id = env.clip(env.job(), status="approved", video_path="a.mp4")
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=AT,
        video_path=f"clip-{clip_id}.mp4",
        now=AT - timedelta(days=1),
    )


def run(env: Env, *args: str):
    return runner.invoke(app, ["--db", str(env.db_path), "posts", *args])


def current(env: Env, post: Post) -> Post:
    found = repo.get_post(env.conn, post.id)
    assert found is not None
    return found


def test_posts_is_a_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert "posts" in result.output


def test_list_shows_open_posts(env: Env) -> None:
    first = make_post(env)
    second = make_post(env, account="yt_main", platform="youtube")
    repo.cancel(env.conn, second.id, AT)

    result = run(env, "list")

    assert result.exit_code == 0, result.output
    assert f"{first.id}" in result.output
    assert "tt_main" in result.output and "scheduled" in result.output
    assert AT.strftime("%Y-%m-%dT%H:%M:%SZ") in result.output
    assert "yt_main" not in result.output  # cancelled is hidden by default


def test_list_all_and_status_filter(env: Env) -> None:
    first = make_post(env)
    second = make_post(env, account="yt_main", platform="youtube")
    repo.cancel(env.conn, second.id, AT)

    everything = run(env, "list", "--all")
    cancelled = run(env, "list", "--status", "cancelled")
    by_clip = run(env, "list", "--clip", str(first.clip_id))

    assert "yt_main" in everything.output and "tt_main" in everything.output
    assert "yt_main" in cancelled.output and "tt_main" not in cancelled.output
    assert "tt_main" in by_clip.output and "yt_main" not in by_clip.output


def test_list_rejects_an_unknown_status(env: Env) -> None:
    result = run(env, "list", "--status", "lost")

    assert result.exit_code == 1
    assert "unknown status" in result.stderr


def test_list_empty(env: Env) -> None:
    result = run(env, "list")

    assert result.exit_code == 0
    assert "no posts" in result.output


def test_cancel(env: Env) -> None:
    target = make_post(env)

    result = run(env, "cancel", str(target.id))

    assert result.exit_code == 0, result.output
    assert current(env, target).status == PostStatus.CANCELLED
    assert env.reviews(target.clip_id)[-1]["action"] == "post_cancelled"


def test_cancel_unknown_or_posted_fails(env: Env) -> None:
    target = make_post(env)
    env.conn.execute("UPDATE posts SET status = 'posted' WHERE id = ?", (target.id,))

    missing = run(env, "cancel", "999")
    posted = run(env, "cancel", str(target.id))

    assert missing.exit_code == 1 and "not found" in missing.stderr
    assert posted.exit_code == 1 and "posted" in posted.stderr


def test_reschedule(env: Env) -> None:
    target = make_post(env)

    local = LATER.replace(hour=10, minute=15).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"

    result = run(env, "reschedule", str(target.id), local)

    assert result.exit_code == 0, result.output
    assert current(env, target).scheduled_at == LATER.replace(hour=2, minute=15)


def test_reschedule_naive_time_is_utc(env: Env) -> None:
    target = make_post(env)

    run(
        env,
        "reschedule",
        str(target.id),
        LATER.replace(hour=10, minute=15).strftime("%Y-%m-%dT%H:%M"),
    )

    assert current(env, target).scheduled_at == LATER.replace(hour=10, minute=15)


def test_reschedule_rejects_a_bad_time(env: Env) -> None:
    target = make_post(env)

    result = run(env, "reschedule", str(target.id), "soon")

    assert result.exit_code == 1
    assert "ISO 8601" in result.stderr


@pytest.mark.parametrize("when", ["2001-01-01T00:00", "9999-12-31T23:59-01:00"])
def test_reschedule_rejects_past_far_or_overflowing_times(env: Env, when: str) -> None:
    target = make_post(env)

    result = run(env, "reschedule", str(target.id), when)

    assert result.exit_code == 1
    assert current(env, target).scheduled_at == AT


def test_retry_requeues_a_failed_post(env: Env) -> None:
    target = make_post(env)
    env.conn.execute(
        "UPDATE posts SET status = 'post_failed', attempts = 3, error = 'boom' WHERE id = ?",
        (target.id,),
    )

    result = run(env, "retry", str(target.id))

    assert result.exit_code == 0, result.output
    requeued = current(env, target)
    assert requeued.status == PostStatus.SCHEDULED
    assert requeued.attempts == 0 and requeued.error is None


def test_retry_of_a_scheduled_post_fails(env: Env) -> None:
    target = make_post(env)

    result = run(env, "retry", str(target.id))

    assert result.exit_code == 1
    assert "scheduled" in result.stderr
