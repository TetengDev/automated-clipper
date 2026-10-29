"""Posts table and `schedule_for_clip` (CLP-92): one post per eligible account, never a
sensitive clip, never the same file to two accounts on one platform."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clipper.jobs.models import from_iso
from clipper.posting import repo
from clipper.posting.models import (
    POST_TRANSITIONS,
    InvalidPostTransition,
    PostStatus,
)
from clipper.posting.scheduler import schedule_for_clip
from posting_support import FakeAccount
from review_support import Env, make_env

NOW = datetime(2027, 1, 15, 0, 30, tzinfo=UTC)  # 08:30 in Manila
MANILA = ZoneInfo("Asia/Manila")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def approved_clip(env: Env, creator: str = "MrBeast", **kwargs: object) -> int:
    job = env.job(creator)
    kwargs.setdefault("video_path", f"youtube_x/clip_{job}_v1.mp4")
    return env.clip(job, status="approved", **kwargs)  # type: ignore[arg-type]


def post_rows(env: Env) -> list[sqlite3.Row]:
    return env.conn.execute("SELECT * FROM posts ORDER BY id").fetchall()


# --- migration ---------------------------------------------------------------------------


def test_posts_table_has_the_story_columns(env: Env) -> None:
    columns = {r["name"] for r in env.conn.execute("PRAGMA table_info(posts)")}

    assert {
        "id",
        "clip_id",
        "clip_version",
        "account_id",
        "platform",
        "status",
        "scheduled_at",
        "posted_at",
        "external_id",
        "url",
        "error",
        "attempts",
        "created_at",
        "idempotency_key",
        "video_path",
    } <= columns


def test_posts_indexes(env: Env) -> None:
    indexes = {r["name"]: r for r in env.conn.execute("PRAGMA index_list(posts)")}
    by_columns = {
        tuple(c["name"] for c in env.conn.execute(f"PRAGMA index_info('{name}')")): row
        for name, row in indexes.items()
    }

    assert by_columns[("clip_id", "account_id")]["unique"] == 1
    assert by_columns[("idempotency_key",)]["unique"] == 1
    assert ("status", "scheduled_at") in by_columns
    file_index = by_columns[("platform", "video_path")]
    assert file_index["unique"] == 1 and file_index["partial"] == 1


def _insert(env: Env, clip_id: int, account: str, platform: str, path: str) -> int:
    post = repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=NOW,
        video_path=path,
        now=NOW,
    )
    return post.id


def test_db_refuses_one_file_to_two_accounts_on_one_platform(env: Env) -> None:
    clip_id = approved_clip(env)
    _insert(env, clip_id, "tt_a", "tiktok", "a.mp4")

    with pytest.raises(sqlite3.IntegrityError):
        _insert(env, clip_id, "tt_b", "tiktok", "a.mp4")


def test_db_allows_the_file_again_once_the_first_post_is_cancelled(env: Env) -> None:
    clip_id = approved_clip(env)
    first = _insert(env, clip_id, "tt_a", "tiktok", "a.mp4")
    repo.cancel(env.conn, first, NOW)

    _insert(env, clip_id, "tt_b", "tiktok", "a.mp4")  # no error
    _insert(env, clip_id, "yt_a", "youtube", "a.mp4")  # other platform: fine


def test_insert_generates_a_unique_idempotency_key(env: Env) -> None:
    clip_id = approved_clip(env)
    _insert(env, clip_id, "tt_a", "tiktok", "a.mp4")
    _insert(env, clip_id, "yt_a", "youtube", "a.mp4")

    keys = [r["idempotency_key"] for r in post_rows(env)]
    assert len(set(keys)) == 2 and all(len(k) >= 32 for k in keys)


# --- statuses ----------------------------------------------------------------------------


def test_post_statuses_and_transitions() -> None:
    assert [s.value for s in PostStatus] == [
        "scheduled",
        "uploading",
        "posted",
        "post_failed",
        "cancelled",
    ]
    assert POST_TRANSITIONS[PostStatus.POSTED] == frozenset()
    assert POST_TRANSITIONS[PostStatus.CANCELLED] == frozenset()
    assert PostStatus.UPLOADING in POST_TRANSITIONS[PostStatus.SCHEDULED]
    assert PostStatus.SCHEDULED in POST_TRANSITIONS[PostStatus.POST_FAILED]


def test_cancel_a_posted_post_is_refused(env: Env) -> None:
    clip_id = approved_clip(env)
    post_id = _insert(env, clip_id, "tt_a", "tiktok", "a.mp4")
    env.conn.execute("UPDATE posts SET status = 'posted' WHERE id = ?", (post_id,))

    with pytest.raises(InvalidPostTransition) as info:
        repo.cancel(env.conn, post_id, NOW)
    assert info.value.current == "posted"


def test_unknown_post_is_a_lookup_error(env: Env) -> None:
    with pytest.raises(LookupError):
        repo.cancel(env.conn, 999, NOW)


# --- schedule_for_clip -------------------------------------------------------------------


def test_one_post_per_eligible_account_inside_its_window(env: Env) -> None:
    clip_id = approved_clip(env)
    accounts = [
        FakeAccount(id="tt_main", platform="tiktok"),
        FakeAccount(id="yt_main", platform="youtube", windows=["12:00-14:00"]),
        FakeAccount(id="ig_kai", platform="instagram", creators=["Kai Cenat"]),
        FakeAccount(id="fb_off", platform="facebook", enabled=False),
    ]

    created = schedule_for_clip(env.conn, clip_id, accounts, NOW)

    assert sorted(p.account_id for p in created) == ["tt_main", "yt_main"]
    by_account = {p.account_id: p for p in created}
    assert by_account["tt_main"].scheduled_at == datetime(2027, 1, 15, 1, 0, tzinfo=UTC)
    assert by_account["yt_main"].scheduled_at == datetime(2027, 1, 15, 4, 0, tzinfo=UTC)
    rows = post_rows(env)
    assert [r["status"] for r in rows] == ["scheduled", "scheduled"]
    assert {r["clip_version"] for r in rows} == {1}
    stored = env.conn.execute(
        "SELECT video_path FROM clip_versions WHERE clip_id = ?", (clip_id,)
    ).fetchone()[0]
    assert {r["video_path"] for r in rows} == {stored}


def test_posts_keep_the_accounts_gap_and_warmup(env: Env) -> None:
    account = FakeAccount(posts_per_day=3, warmup_start=date(2027, 1, 15), warmup_days=14)
    first = schedule_for_clip(env.conn, approved_clip(env), [account], NOW)
    second = schedule_for_clip(env.conn, approved_clip(env), [account], NOW)

    assert first[0].scheduled_at.astimezone(MANILA) == datetime(2027, 1, 15, 9, tzinfo=MANILA)
    # Warm-up day 0 allows one post, so the next goes to tomorrow's first window.
    assert second[0].scheduled_at.astimezone(MANILA) == datetime(2027, 1, 16, 9, tzinfo=MANILA)


def test_a_day_never_exceeds_posts_per_day_or_the_platform_cap(env: Env) -> None:
    busy = FakeAccount(posts_per_day=40, min_gap_minutes=1, windows=["00:00-23:59"])
    small = FakeAccount(id="yt_small", platform="youtube", posts_per_day=2, min_gap_minutes=1)
    for _ in range(20):
        schedule_for_clip(env.conn, approved_clip(env), [busy, small], NOW)

    per_day = Counter(
        (r["account_id"], from_iso(r["scheduled_at"]).astimezone(MANILA).date())
        for r in post_rows(env)
    )
    assert max(n for (acc, _), n in per_day.items() if acc == "tt_main") == 15
    assert max(n for (acc, _), n in per_day.items() if acc == "yt_small") == 2


def test_a_sensitive_clip_is_never_scheduled(env: Env) -> None:
    flagged = approved_clip(env, sensitive=True)
    by_rule = approved_clip(env)

    assert schedule_for_clip(env.conn, flagged, [FakeAccount()], NOW) == []
    assert schedule_for_clip(env.conn, by_rule, [FakeAccount()], NOW, sensitive=True) == []
    assert post_rows(env) == []


def test_same_file_is_never_scheduled_to_two_accounts_on_one_platform(env: Env) -> None:
    clip_id = approved_clip(env)
    accounts = [
        FakeAccount(id="tt_a"),
        FakeAccount(id="tt_b"),
        FakeAccount(id="yt_a", platform="youtube"),
    ]

    created = schedule_for_clip(env.conn, clip_id, accounts, NOW)

    assert sorted(p.account_id for p in created) == ["tt_a", "yt_a"]
    platforms = [r["platform"] for r in post_rows(env)]
    assert platforms.count("tiktok") == 1


def test_scheduling_twice_is_idempotent(env: Env) -> None:
    clip_id = approved_clip(env)
    schedule_for_clip(env.conn, clip_id, [FakeAccount()], NOW)

    again = schedule_for_clip(env.conn, clip_id, [FakeAccount()], NOW)

    assert again == []
    assert len(post_rows(env)) == 1


def test_unapproved_or_unrendered_clips_are_not_scheduled(env: Env) -> None:
    pending = env.clip(env.job(), video_path="a.mp4")
    no_video = env.clip(env.job(), status="approved", video_path=None)

    assert schedule_for_clip(env.conn, pending, [FakeAccount()], NOW) == []
    assert schedule_for_clip(env.conn, no_video, [FakeAccount()], NOW) == []
    assert schedule_for_clip(env.conn, 999, [FakeAccount()], NOW) == []


def test_exported_clips_are_scheduled_too(env: Env) -> None:
    clip_id = env.clip(env.job(), status="exported", video_path="b.mp4")

    assert len(schedule_for_clip(env.conn, clip_id, [FakeAccount()], NOW)) == 1


def test_no_slot_is_logged_and_skipped(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    account = FakeAccount(warmup_start=date(2027, 3, 1))

    created = schedule_for_clip(env.conn, approved_clip(env), [account], NOW)

    assert created == []
    assert "no slot" in caplog.text


def test_an_account_with_a_bad_timezone_is_skipped(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    bad = FakeAccount(id="tt_bad", timezone="Mars/Olympus")
    good = FakeAccount(id="yt_ok", platform="youtube")

    created = schedule_for_clip(env.conn, approved_clip(env), [bad, good], NOW)

    assert [p.account_id for p in created] == ["yt_ok"]
    assert "tt_bad" in caplog.text


def test_scheduling_writes_an_audit_row(env: Env) -> None:
    clip_id = approved_clip(env)

    [post] = schedule_for_clip(env.conn, clip_id, [FakeAccount()], NOW)

    rows = env.reviews(clip_id)
    assert [r["action"] for r in rows] == ["post_scheduled"]
    payload = json.loads(rows[0]["payload"])
    assert payload["post_id"] == post.id
    assert payload["account_id"] == "tt_main"
    assert payload["platform"] == "tiktok"


def test_cancelled_posts_free_their_slot(env: Env) -> None:
    account = FakeAccount(posts_per_day=1)
    [first] = schedule_for_clip(env.conn, approved_clip(env), [account], NOW)
    repo.cancel(env.conn, first.id, NOW)

    [second] = schedule_for_clip(env.conn, approved_clip(env), [account], NOW)

    assert second.scheduled_at == first.scheduled_at


def test_reschedule_and_retry(env: Env) -> None:
    [post] = schedule_for_clip(env.conn, approved_clip(env), [FakeAccount()], NOW)
    later = NOW + timedelta(days=2)

    moved = repo.reschedule(env.conn, post.id, later, NOW)
    assert moved.scheduled_at == later and moved.status == PostStatus.SCHEDULED

    env.conn.execute(
        "UPDATE posts SET status = 'post_failed', attempts = 3, error = 'x' WHERE id = ?",
        (post.id,),
    )
    retried = repo.retry(env.conn, post.id, NOW)
    assert retried.status == PostStatus.SCHEDULED
    assert retried.attempts == 0 and retried.error is None
    assert retried.scheduled_at == NOW
    actions = [r["action"] for r in env.reviews(post.clip_id)]
    assert actions[-2:] == ["post_rescheduled", "post_scheduled"]


def test_retry_only_requeues_failed_posts(env: Env) -> None:
    [post] = schedule_for_clip(env.conn, approved_clip(env), [FakeAccount()], NOW)

    with pytest.raises(InvalidPostTransition):
        repo.retry(env.conn, post.id, NOW)
