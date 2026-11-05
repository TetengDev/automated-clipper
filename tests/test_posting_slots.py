"""Slot-aware backoff, retry and reschedule (CLP-106): later scheduling paths respect the
account's windows, gap, posts per day, warm-up and platform cap, like the first one."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipper.cli import app
from clipper.posting import repo
from clipper.posting.models import MAX_ATTEMPTS, Post, PostStatus, PublishResult
from clipper.posting.scheduler import MAX_DAYS_AHEAD, PLATFORM_DAILY_CAPS, slot_problems
from clipper.posting.stage import BACKOFF_BASE, PostingStage
from conftest import VALID_YAML
from posting_support import FakeAccount, FakePublisher
from review_support import Env, login, make_env

MANILA = ZoneInfo("Asia/Manila")  # UTC+8, no DST
T0 = datetime(2027, 1, 15, 1, 0, tzinfo=UTC)  # 09:00 in Manila, a window opens


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A Manila wall-clock time in January 2027, as an aware UTC datetime."""
    return datetime(2027, 1, day, hour, minute, tzinfo=MANILA).astimezone(UTC)


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
    account: str = "tt_main",
    platform: str = "tiktok",
    at: datetime = T0,
    status: str | None = None,
    posted_at: datetime | None = None,
) -> Post:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    target = env.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake mp4")
    clip_id = env.clip(job, status="approved", video_path=rel, title="t", caption="c")
    post = repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=at,
        video_path=rel,
        now=T0 - timedelta(days=1),
    )
    if status is not None:
        env.conn.execute(
            "UPDATE posts SET status = ?, posted_at = ? WHERE id = ?",
            (status, posted_at.strftime("%Y-%m-%dT%H:%M:%SZ") if posted_at else None, post.id),
        )
    return post


def stage(env: Env, clock: Clock, publisher: object, *accounts: FakeAccount, **kw: object):
    return PostingStage(
        conn=env.conn,
        publishers={"tiktok": publisher},  # type: ignore[dict-item]
        worker_id="w1",
        media_root=env.media_root,
        clock=clock,
        accounts={a.id: a for a in accounts},
        **kw,  # type: ignore[arg-type]
    )


def get(env: Env, post: Post) -> Post:
    found = repo.get_post(env.conn, post.id)
    assert found is not None
    return found


# --- slot_problems -----------------------------------------------------------------------


def test_slot_problems_is_empty_for_a_slot_the_picker_would_choose() -> None:
    assert slot_problems(FakeAccount(), [], local(15, 9)) == []


def test_slot_problems_flags_a_time_outside_the_windows() -> None:
    [problem] = slot_problems(FakeAccount(), [], local(15, 13))

    assert "outside" in problem and "09:00-12:00" in problem and "Asia/Manila" in problem


def test_slot_problems_flags_a_time_inside_another_posts_gap() -> None:
    [problem] = slot_problems(FakeAccount(), [local(15, 10)], local(15, 11))

    assert "120" in problem and "gap" in problem


def test_slot_problems_ignores_a_post_on_another_local_day() -> None:
    late = FakeAccount(windows=["00:00-23:59"])

    assert slot_problems(late, [local(15, 23, 30)], local(16, 0, 30)) == []


def test_slot_problems_flags_a_full_day() -> None:
    account = FakeAccount(posts_per_day=2, min_gap_minutes=0)

    [problem] = slot_problems(account, [local(15, 9), local(15, 19)], local(15, 20))

    assert "2 post(s)" in problem and "limit" in problem


def test_slot_problems_flags_the_warmup_ramp() -> None:
    account = FakeAccount(warmup_start=date(2027, 1, 15), warmup_days=14, posts_per_day=3)

    [problem] = slot_problems(account, [local(15, 9)], local(15, 19))  # day 0 allows 1

    assert "limit 1" in problem


# --- AC2: an automatic retry lands inside the windows and the gap ------------------------


def test_backoff_retry_moves_to_the_next_free_slot(env: Env, clock: Clock) -> None:
    account = FakeAccount()
    post = make_post(env)
    make_post(env, at=local(15, 11))  # 09:10 is inside its 120-minute gap

    stage(env, clock, FakePublisher(fail=1), account).run_once()

    retried = get(env, post)
    assert retried.status == PostStatus.SCHEDULED and retried.attempts == 1
    assert retried.scheduled_at == local(15, 18)  # 13:00 is past the morning window
    assert slot_problems(account, [local(15, 11)], retried.scheduled_at) == []


def test_backoff_retry_inside_the_window_keeps_the_backoff_time(env: Env, clock: Clock) -> None:
    post = make_post(env)

    stage(env, clock, FakePublisher(fail=1), FakeAccount()).run_once()

    assert get(env, post).scheduled_at == T0 + BACKOFF_BASE * 2


def test_backoff_without_a_known_account_is_plain_backoff(env: Env, clock: Clock) -> None:
    post = make_post(env, account="ghost")

    stage(env, clock, FakePublisher(fail=1), FakeAccount()).run_once()

    assert get(env, post).scheduled_at == T0 + BACKOFF_BASE * 2


# --- AC1: retries never exceed posts_per_day or the platform cap --------------------------


def test_backoff_retry_skips_a_full_day(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=2, min_gap_minutes=0)
    post = make_post(env)
    make_post(env, at=local(15, 11))
    make_post(env, at=local(15, 19))

    stage(env, clock, FakePublisher(fail=1), account).run_once()

    assert get(env, post).scheduled_at == local(16, 9)


def test_backoff_retry_with_no_free_slot_gives_up_and_alerts(env: Env, clock: Clock) -> None:
    # Every day the picker searches already holds the account's one post a day.
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    post = make_post(env)
    for offset in range(MAX_DAYS_AHEAD + 1):
        make_post(env, at=local(15 + offset, 11))
    alerts: list[str] = []

    stage(env, clock, FakePublisher(fail=1), account, alert=alerts.append).run_once()

    failed = get(env, post)
    assert failed.status == PostStatus.POST_FAILED and failed.attempts == 1
    assert failed.error is not None and "no free slot" in failed.error
    assert len(alerts) == 1


def test_a_warmup_day_with_no_allowance_defers_instead_of_publishing(
    env: Env, clock: Clock
) -> None:
    account = FakeAccount(warmup_start=date(2027, 6, 1))
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at > T0


def test_last_attempt_still_fails_without_looking_for_a_slot(env: Env, clock: Clock) -> None:
    post = make_post(env)
    env.conn.execute("UPDATE posts SET attempts = ? WHERE id = ?", (MAX_ATTEMPTS - 1, post.id))

    stage(env, clock, FakePublisher(fail=1), FakeAccount(), alert=lambda _: None).run_once()

    assert get(env, post).status == PostStatus.POST_FAILED


def test_claim_defers_a_post_that_would_exceed_the_daily_limit(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    make_post(env, at=local(15, 8), status="posted", posted_at=local(15, 8))
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    deferred = get(env, due)
    assert deferred.status == PostStatus.SCHEDULED and deferred.attempts == 0
    assert deferred.scheduled_at == local(16, 9)
    assert env.reviews(due.clip_id)[-1]["action"] == "post_deferred"


def test_claim_counts_uploads_in_flight_toward_the_limit(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    busy = make_post(env, at=local(15, 8))
    env.conn.execute(
        "UPDATE posts SET status = 'uploading', worker_id = 'w2', claimed_at = ? WHERE id = ?",
        (T0.strftime("%Y-%m-%dT%H:%M:%SZ"), busy.id),
    )
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at > T0


def test_claim_applies_the_platform_cap_to_an_unknown_account(env: Env, clock: Clock) -> None:
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    for _ in range(cap):
        make_post(env, account="ghost", at=T0, status="posted", posted_at=T0)
    due = make_post(env, account="ghost")
    publisher = FakePublisher()

    stage(env, clock, publisher).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at == datetime(2027, 1, 16, tzinfo=UTC)  # next UTC day


def test_a_deferred_post_does_not_block_other_accounts(env: Env, clock: Clock) -> None:
    full = FakeAccount(id="tt_full", posts_per_day=1, min_gap_minutes=0)
    make_post(env, account="tt_full", at=local(15, 8), status="posted", posted_at=local(15, 8))
    blocked = make_post(env, account="tt_full", at=T0 - timedelta(minutes=5))
    other = make_post(env, account="tt_main")
    publisher = FakePublisher()

    stage(env, clock, publisher, full, FakeAccount()).run_once()

    assert [sent.id for sent, _, _ in publisher.calls] == [other.id]
    assert get(env, blocked).status == PostStatus.SCHEDULED
    assert get(env, other).status == PostStatus.POSTED


def test_posts_under_the_limit_are_claimed_as_before(env: Env, clock: Clock) -> None:
    make_post(env, at=local(15, 8), status="posted", posted_at=local(15, 8))
    due = make_post(env)

    stage(env, clock, FakePublisher(), FakeAccount()).run_once()

    assert get(env, due).status == PostStatus.POSTED


def test_posts_of_yesterday_do_not_count_toward_today(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    make_post(env, at=local(14, 20), status="posted", posted_at=local(14, 20))
    due = make_post(env)

    stage(env, clock, FakePublisher(), account).run_once()

    assert get(env, due).status == PostStatus.POSTED


def test_a_retry_that_went_through_is_recorded_even_on_a_full_day(env: Env, clock: Clock) -> None:
    # The cap stops new uploads; it must never stop recording one that already happened.
    from posting_support import LookupPublisher

    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    make_post(env, at=local(15, 8), status="posted", posted_at=local(15, 8))
    due = make_post(env)
    env.conn.execute("UPDATE posts SET attempts = 1 WHERE id = ?", (due.id,))
    publisher = LookupPublisher(found={due.idempotency_key: PublishResult(external_id="late")})

    stage(env, clock, publisher, account).run_once()

    # Deferred rather than claimed; the lookup happens when it is claimed in its new slot.
    assert publisher.calls == []
    assert get(env, due).status == PostStatus.SCHEDULED


# --- CLI: accounts config, retry into the next free slot, flagged reschedules -------------

ACCOUNTS_YAML = """\
accounts:
  - id: tt_main
    platform: tiktok
    handle: "@clips.main"
    market: US
    timezone: UTC
    creators: [MrBeast]
    posts_per_day: 2
    min_gap_minutes: 60
    windows: ["09:00-12:00"]
    licence: "signed licence TEST-1"
  - id: tt_cold
    platform: tiktok
    handle: "@clips.cold"
    market: US
    timezone: UTC
    creators: [MrBeast]
    posts_per_day: 2
    min_gap_minutes: 60
    windows: ["09:00-12:00"]
    licence: "signed licence TEST-1"
    warmup_start: 2099-01-01
"""

runner = CliRunner()
# The CLI uses the real clock: keep test times a few days ahead of it.
DAY = (datetime.now(UTC) + timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def cli_env(env: Env, tmp_path: Path) -> Env:
    (tmp_path / "creators.yaml").write_text(VALID_YAML)
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    return env


def cli(env: Env, *args: str, stdin: str | None = None):
    config = env.db_path.parent / "creators.yaml"
    return runner.invoke(
        app, ["--db", str(env.db_path), "--config", str(config), *args], input=stdin
    )


def failed_post(env: Env, account: str = "tt_main") -> Post:
    post = make_post(env, account=account, at=DAY + timedelta(hours=9))
    env.conn.execute(
        "UPDATE posts SET status = 'post_failed', attempts = 3, error = 'boom' WHERE id = ?",
        (post.id,),
    )
    return post


def test_cli_retry_uses_the_next_free_slot(cli_env: Env) -> None:
    post = failed_post(cli_env)
    before = datetime.now(UTC)

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 0, result.output
    requeued = get(cli_env, post)
    assert requeued.status == PostStatus.SCHEDULED and requeued.attempts == 0
    assert requeued.scheduled_at >= before.replace(second=0, microsecond=0)
    assert time(9) <= requeued.scheduled_at.time() <= time(12)
    others = [p.scheduled_at for p in repo.list_posts(cli_env.conn) if p.id != post.id]
    assert slot_problems(_main(), others, requeued.scheduled_at) == []


def _main() -> FakeAccount:
    return FakeAccount(
        id="tt_main", timezone="UTC", posts_per_day=2, min_gap_minutes=60, windows=["09:00-12:00"]
    )


def test_cli_retry_with_no_free_slot_fails_and_says_so(cli_env: Env) -> None:
    post = failed_post(cli_env, account="tt_cold")

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 1
    assert "no free slot" in result.stderr and "--at" in result.stderr
    assert get(cli_env, post).status == PostStatus.POST_FAILED


def test_cli_retry_at_a_good_time_needs_no_confirmation(cli_env: Env) -> None:
    post = failed_post(cli_env)
    at = (DAY + timedelta(days=1, hours=10)).strftime("%Y-%m-%dT%H:%M")

    result = cli(cli_env, "posts", "retry", str(post.id), "--at", at)

    assert result.exit_code == 0, result.output
    assert get(cli_env, post).scheduled_at == DAY + timedelta(days=1, hours=10)


def test_cli_retry_at_a_rule_breaking_time_asks_first(cli_env: Env) -> None:
    post = failed_post(cli_env)
    at = (DAY + timedelta(days=1, hours=15)).strftime("%Y-%m-%dT%H:%M")

    declined = cli(cli_env, "posts", "retry", str(post.id), "--at", at, stdin="n\n")

    assert declined.exit_code != 0
    assert "outside" in declined.output
    assert get(cli_env, post).status == PostStatus.POST_FAILED

    forced = cli(cli_env, "posts", "retry", str(post.id), "--at", at, "--yes")

    assert forced.exit_code == 0, forced.output
    assert "outside" in forced.output
    assert get(cli_env, post).scheduled_at == DAY + timedelta(days=1, hours=15)


def test_cli_reschedule_that_breaks_the_gap_is_flagged_and_confirmed(cli_env: Env) -> None:
    make_post(cli_env, at=DAY + timedelta(hours=10))
    post = make_post(cli_env, at=DAY + timedelta(hours=11, minutes=30))
    at = (DAY + timedelta(hours=10, minutes=30)).strftime("%Y-%m-%dT%H:%M")

    declined = cli(cli_env, "posts", "reschedule", str(post.id), at, stdin="n\n")
    accepted = cli(cli_env, "posts", "reschedule", str(post.id), at, stdin="y\n")

    assert declined.exit_code != 0 and "gap" in declined.output
    assert accepted.exit_code == 0, accepted.output
    assert get(cli_env, post).scheduled_at == DAY + timedelta(hours=10, minutes=30)


def test_cli_reschedule_within_the_rules_does_not_ask(cli_env: Env) -> None:
    post = make_post(cli_env, at=DAY + timedelta(hours=9))
    at = (DAY + timedelta(days=1, hours=11)).strftime("%Y-%m-%dT%H:%M")

    result = cli(cli_env, "posts", "reschedule", str(post.id), at)

    assert result.exit_code == 0, result.output
    assert "warning" not in result.output.lower()


def test_cli_retry_for_an_account_missing_from_the_config_requeues_now(cli_env: Env) -> None:
    post = failed_post(cli_env, account="gone")
    before = datetime.now(UTC).replace(microsecond=0)

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 0, result.output
    assert "not in the accounts config" in result.output
    assert get(cli_env, post).scheduled_at >= before - timedelta(seconds=1)


def test_cli_without_an_accounts_file_keeps_the_old_behaviour(env: Env) -> None:
    post = failed_post(env)

    result = runner.invoke(app, ["--db", str(env.db_path), "posts", "retry", str(post.id)])

    assert result.exit_code == 0, result.output
    assert get(env, post).status == PostStatus.SCHEDULED


def test_cli_with_a_broken_accounts_file_fails(cli_env: Env, tmp_path: Path) -> None:
    (tmp_path / "accounts.yaml").write_text("accounts: [{id: Bad}]\n")
    post = failed_post(cli_env)

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 1
    assert "accounts" in result.stderr
    assert get(cli_env, post).status == PostStatus.POST_FAILED


def test_run_worker_loads_the_accounts_config(cli_env: Env, tmp_path: Path) -> None:
    (tmp_path / "accounts.yaml").write_text("accounts: [{id: Bad}]\n")

    result = cli(cli_env, "run-worker", "--once")

    assert result.exit_code == 1
    assert "accounts" in result.output


# --- review app: a rule-breaking reschedule is flagged to the reviewer --------------------

REVIEW_NOW = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)  # review_support.WALL_START


def reviewer(env: Env, *accounts: FakeAccount) -> TestClient:
    client = env.client(accounts=accounts)
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


def reschedule(client: TestClient, post: Post, value: str):
    return client.post(
        f"/posts/{post.id}/reschedule",
        data={"scheduled_at": value, "next": "/scheduled"},
        headers={"X-CSRF-Token": client.csrf, "HX-Request": "true"},  # type: ignore[attr-defined]
    )


def test_review_reschedule_that_breaks_a_rule_is_flagged(env: Env) -> None:
    make_post(env, at=REVIEW_NOW + timedelta(hours=2))
    post = make_post(env, at=REVIEW_NOW + timedelta(hours=6))
    client = reviewer(env, FakeAccount(windows=["00:00-23:59"]))

    response = reschedule(client, post, "2027-01-15T10:30")  # 30 min after the other post

    assert response.status_code == 200
    page = client.get("/scheduled").text
    assert "rescheduled" in page
    assert "120-minute gap" in page
    assert get(env, post).scheduled_at == datetime(2027, 1, 15, 10, 30, tzinfo=UTC)


def test_review_reschedule_within_the_rules_is_not_flagged(env: Env) -> None:
    post = make_post(env, at=REVIEW_NOW + timedelta(hours=6))
    client = reviewer(env, FakeAccount(windows=["00:00-23:59"]))

    reschedule(client, post, "2027-01-15T10:30")

    page = client.get("/scheduled").text
    assert "rescheduled" in page and "Breaks" not in page
