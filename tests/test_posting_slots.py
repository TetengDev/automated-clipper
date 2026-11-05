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
from clipper.jobs.models import to_iso
from clipper.posting import repo
from clipper.posting.models import MAX_ATTEMPTS, Post, PostStatus, PublishResult
from clipper.posting.scheduler import (
    MAX_DAYS_AHEAD,
    PLATFORM_DAILY_CAPS,
    day_bounds,
    slot_problems,
)
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
            (status, to_iso(posted_at) if posted_at else None, post.id),
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


def test_a_warmup_with_no_allowance_for_weeks_holds_the_post_for_a_human(
    env: Env, clock: Clock
) -> None:
    account = FakeAccount(warmup_start=date(2027, 6, 1))  # no slot within MAX_DAYS_AHEAD
    due = make_post(env)
    publisher = FakePublisher()
    alerts: list[str] = []

    stage(env, clock, publisher, account, alert=alerts.append).run_once()

    assert publisher.calls == []
    held = get(env, due)
    assert held.status == PostStatus.POST_FAILED and held.attempts == 0
    assert len(alerts) == 1


def test_a_warmup_day_with_no_allowance_defers_to_the_first_allowed_day(
    env: Env, clock: Clock
) -> None:
    account = FakeAccount(warmup_start=date(2027, 1, 17))
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at == local(17, 9)


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
        (to_iso(T0), busy.id),
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
    make_post(env, at=local(15, 6), status="posted", posted_at=local(15, 6))  # outside the gap
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

    assert publisher.calls == []
    done = get(env, due)
    assert done.status == PostStatus.POSTED and done.external_id == "late"


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


# --- review pass 1 -------------------------------------------------------------------------


def fail_attempt(env: Env, post: Post, at: datetime, error: str = "boom") -> Post:
    """Record one failed upload attempt of `post` at `at`, the way a worker does."""
    env.conn.execute(
        "UPDATE posts SET status = 'uploading', worker_id = 'w0', claimed_at = ? WHERE id = ?",
        (to_iso(at), post.id),
    )
    return repo.record_failure(
        env.conn, get(env, post), "w0", error, at, max_attempts=MAX_ATTEMPTS, backoff=BACKOFF_BASE
    )


def failed_today(env: Env, account: str = "tt_main", attempts: int = 1) -> Post:
    """A post whose attempt(s) reached the platform today and failed (now backing off)."""
    post = make_post(env, account=account, at=T0 + timedelta(hours=5))
    for _ in range(attempts):
        post = fail_attempt(env, post, T0 - timedelta(minutes=30))
    env.conn.execute(
        "UPDATE posts SET scheduled_at = ? WHERE id = ?",
        (to_iso(T0 + timedelta(hours=5)), post.id),
    )
    return post


def test_claim_defer_with_no_free_slot_gives_up_and_alerts(env: Env, clock: Clock) -> None:
    # Over today's limit and every searched day full: never a midnight burst; a human decides.
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    make_post(env, at=local(15, 8), status="posted", posted_at=local(15, 8))
    for offset in range(1, MAX_DAYS_AHEAD + 2):  # the search from tomorrow finds nothing
        make_post(env, at=local(15 + offset, 11))
    due = make_post(env)
    alerts: list[str] = []
    publisher = FakePublisher()

    stage(env, clock, publisher, account, alert=alerts.append).run_once()

    assert publisher.calls == []
    failed = get(env, due)
    assert failed.status == PostStatus.POST_FAILED and failed.attempts == 0
    assert failed.error is not None and "no free slot" in failed.error
    assert len(alerts) == 1


def test_failed_attempts_today_count_toward_the_daily_limit(env: Env, clock: Clock) -> None:
    # An attempt that reached the platform and failed may still have used quota (or gone
    # live): it counts, so a burst of failing posts cannot exceed the cap.
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    failed_today(env)
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at == local(16, 9)


def test_an_unresolved_upload_today_counts_toward_the_daily_limit(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    gone = make_post(env, at=local(15, 8))
    stale_at = T0 - timedelta(minutes=5)
    env.conn.execute(
        "UPDATE posts SET status = 'uploading', worker_id = 'w0', claimed_at = ? WHERE id = ?",
        (to_iso(stale_at), gone.id),
    )
    repo.mark_failed(
        env.conn, get(env, gone), "w0", "uploading state unresolved: not found", stale_at
    )
    make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []


def test_a_post_deferred_with_past_attempts_is_not_deferred_for_ever(
    env: Env, clock: Clock
) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    due = make_post(env)
    env.conn.execute(
        "UPDATE posts SET attempts = 1, updated_at = ? WHERE id = ?",
        (to_iso(T0 - timedelta(days=1)), due.id),
    )
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert get(env, due).status == PostStatus.POSTED  # its own old attempt does not block it


def test_rolling_24_hours_cap_stops_a_burst_across_midnight(env: Env, clock: Clock) -> None:
    # An account not in the config uses UTC days: 15 TikTok uploads late yesterday must
    # still block more right after midnight.
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    clock.now = datetime(2027, 1, 15, 0, 30, tzinfo=UTC)
    for _ in range(cap):
        late = datetime(2027, 1, 14, 23, 30, tzinfo=UTC)
        make_post(env, account="ghost", at=late, status="posted", posted_at=late)
    due = make_post(env, account="ghost", at=clock.now)
    publisher = FakePublisher()

    stage(env, clock, publisher).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at > clock.now


def test_claim_keeps_the_gap_after_the_last_upload(env: Env, clock: Clock) -> None:
    # Two posts squeezed into one minute by a forced reschedule do not upload back to back.
    account = FakeAccount(min_gap_minutes=120)
    make_post(env, at=T0, status="posted", posted_at=T0 - timedelta(minutes=10))
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []
    assert get(env, due).scheduled_at >= T0 - timedelta(minutes=10) + timedelta(minutes=120)


def test_several_over_limit_posts_are_each_deferred_once_and_others_still_go(
    env: Env, clock: Clock
) -> None:
    full = FakeAccount(id="tt_full", posts_per_day=1, min_gap_minutes=0)
    make_post(env, account="tt_full", at=local(15, 8), status="posted", posted_at=local(15, 8))
    blocked = [make_post(env, account="tt_full", at=T0 - timedelta(minutes=m)) for m in (3, 2)]
    other = make_post(env, account="tt_main")
    publisher = FakePublisher()

    stage(env, clock, publisher, full, FakeAccount()).run_once()

    assert [sent.id for sent, _, _ in publisher.calls] == [other.id]
    for post in blocked:
        assert get(env, post).scheduled_at > T0
        actions = [r["action"] for r in env.reviews(post.clip_id)]
        assert actions.count("post_deferred") == 1


def test_a_database_error_in_the_cap_check_does_not_leave_the_post_uploading(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def broken(*_: object, **__: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repo, "uploads_between", broken)
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, FakeAccount()).run_once()

    assert publisher.calls == []
    assert get(env, due).status == PostStatus.SCHEDULED


def test_a_bad_account_config_falls_back_to_the_platform_cap(env: Env, clock: Clock) -> None:
    broken = FakeAccount(timezone="Nowhere/Land")
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    for _ in range(cap):
        make_post(env, at=T0, status="posted", posted_at=T0 - timedelta(minutes=1))
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, broken).run_once()

    assert publisher.calls == []
    assert get(env, due).status == PostStatus.SCHEDULED


def test_local_day_not_utc_day_bounds_the_count() -> None:
    # 02:00 Manila on Jan 15 is Jan 14 in UTC, but it is the Manila day of Jan 15.
    start, end = day_bounds("Asia/Manila", local(15, 2))

    assert start == local(15, 0) and end == local(16, 0)


def test_posts_early_in_the_local_day_count_toward_it(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    make_post(env, at=local(15, 2), status="posted", posted_at=local(15, 2))
    make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert publisher.calls == []


def test_day_bounds_on_a_dst_day() -> None:
    ny = ZoneInfo("America/New_York")
    start, end = day_bounds("America/New_York", datetime(2027, 3, 14, 12, tzinfo=ny))

    assert end - start == timedelta(hours=23)
    assert start == datetime(2027, 3, 14, tzinfo=ny).astimezone(UTC)


def test_slot_problems_window_end_minute_and_midnight_end() -> None:
    account = FakeAccount(windows=["09:00-12:00", "22:00-00:00"])

    assert slot_problems(account, [], local(15, 12)) == []
    assert slot_problems(account, [], local(15, 23, 59)) == []
    assert slot_problems(account, [], local(15, 12, 1)) != []
    assert slot_problems(account, [], local(16, 0)) != []


def test_review_reschedule_for_an_account_not_in_the_config_is_not_flagged(env: Env) -> None:
    post = make_post(env, account="ghost", at=REVIEW_NOW + timedelta(hours=6))
    client = reviewer(env, FakeAccount(windows=["09:00-09:30"]))

    reschedule(client, post, "2027-01-15T15:30")

    page = client.get("/scheduled").text
    assert "rescheduled" in page and "Breaks" not in page


def test_cli_reschedule_of_an_uploading_post_fails_before_asking(cli_env: Env) -> None:
    post = make_post(cli_env, at=DAY + timedelta(hours=9))
    cli_env.conn.execute("UPDATE posts SET status = 'uploading' WHERE id = ?", (post.id,))
    at = (DAY + timedelta(days=1, hours=15)).strftime("%Y-%m-%dT%H:%M")

    result = cli(cli_env, "posts", "reschedule", str(post.id), at)

    assert result.exit_code == 1
    assert "uploading" in result.stderr
    assert "Schedule it anyway" not in result.output


def test_cli_reschedule_warns_about_an_account_missing_from_the_config(cli_env: Env) -> None:
    post = make_post(cli_env, account="gone", at=DAY + timedelta(hours=9))
    at = (DAY + timedelta(days=1, hours=15)).strftime("%Y-%m-%dT%H:%M")

    result = cli(cli_env, "posts", "reschedule", str(post.id), at)

    assert result.exit_code == 0, result.output
    assert "not in the accounts config" in result.output


def test_cli_retry_slot_is_chosen_inside_the_write(cli_env: Env, monkeypatch) -> None:
    # The slot is picked in the same transaction that writes it, so a post scheduled in
    # between (another process) is seen.
    post = failed_post(cli_env)
    seen: list[bool] = []
    real = repo.account_post_times

    def watching(conn, *args, **kwargs):
        seen.append(conn.in_transaction)
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(repo, "account_post_times", watching)

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 0, result.output
    assert seen and all(seen)


def test_stage_retry_slot_is_chosen_inside_the_write(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = make_post(env)
    seen: list[bool] = []
    real = repo.account_post_times

    def watching(conn, *args, **kwargs):
        seen.append(conn.in_transaction)
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(repo, "account_post_times", watching)

    stage(env, clock, FakePublisher(fail=1), FakeAccount()).run_once()

    assert get(env, post).attempts == 1
    assert seen and all(seen)


def test_account_post_times_can_skip_old_history(env: Env) -> None:
    make_post(env, at=T0 - timedelta(days=30))
    recent = make_post(env, at=T0)

    times = repo.account_post_times(env.conn, "tt_main", since=T0 - timedelta(days=2))

    assert times == [recent.scheduled_at]


# --- review pass 2 -------------------------------------------------------------------------


def test_deferred_posts_with_old_attempts_do_not_hold_each_other_off(
    env: Env, clock: Clock
) -> None:
    # A deferral is not an attempt: posts that failed twice a day and a half ago, then were
    # deferred an hour ago, must not count each other's old attempts and stall for ever.
    long_ago = T0 - timedelta(hours=30)
    stuck = []
    for _ in range(9):
        post = make_post(env, account="ghost")
        post = fail_attempt(env, post, long_ago)
        post = fail_attempt(env, post, long_ago + timedelta(minutes=10))
        stuck.append(post)
    for post in stuck:
        env.conn.execute(
            "UPDATE posts SET scheduled_at = ?, updated_at = ? WHERE id = ?",
            (to_iso(T0), to_iso(T0 - timedelta(hours=1)), post.id),
        )
    publisher = FakePublisher()

    stage(env, clock, publisher).run_once()

    assert len(publisher.calls) == 9


def test_a_failure_recorded_yesterday_and_changed_today_counts_yesterday(
    env: Env, clock: Clock
) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    old = make_post(env, at=local(14, 20))
    old = fail_attempt(env, old, local(14, 20))
    env.conn.execute(  # e.g. deferred this morning
        "UPDATE posts SET scheduled_at = ?, updated_at = ? WHERE id = ?",
        (to_iso(local(16, 9)), to_iso(local(15, 8)), old.id),
    )
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, account).run_once()

    assert [sent.id for sent, _, _ in publisher.calls] == [due.id]


def test_a_retry_does_not_erase_attempts_from_the_rolling_cap(env: Env, clock: Clock) -> None:
    # A reviewer's retry starts the attempt count over, but the API calls already made in
    # the last 24 hours still used the platform's quota.
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    for _ in range(cap // MAX_ATTEMPTS):
        post = make_post(env, account="ghost", at=T0)
        for minutes in range(MAX_ATTEMPTS):
            post = fail_attempt(env, post, T0 - timedelta(hours=3, minutes=-minutes))
        assert post.status == PostStatus.POST_FAILED
        repo.retry(env.conn, post.id, T0, at=T0 + timedelta(days=1))
    due = make_post(env, account="ghost")
    publisher = FakePublisher()

    stage(env, clock, publisher).run_once()

    assert publisher.calls == []
    assert get(env, due).status == PostStatus.SCHEDULED


def test_the_claimed_posts_own_recent_attempts_count_toward_the_rolling_cap(
    env: Env, clock: Clock
) -> None:
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    hour_ago = T0 - timedelta(hours=1)
    for _ in range(cap - 1):
        make_post(env, account="ghost", at=hour_ago, status="posted", posted_at=hour_ago)
    due = make_post(env, account="ghost")
    due = fail_attempt(env, due, T0 - timedelta(minutes=50))
    env.conn.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?", (to_iso(T0), due.id))
    publisher = FakePublisher()

    stage(env, clock, publisher).run_once()

    assert publisher.calls == []  # 14 uploads + its own attempt: one more would be 16


def test_a_retried_post_with_earlier_attempts_is_looked_up_first(env: Env, clock: Clock) -> None:
    # A reviewer's retry resets the attempt count, but an earlier attempt may have gone
    # live: look it up before uploading again (same idempotency key).
    from posting_support import LookupPublisher

    post = make_post(env, account="ghost")
    for minutes in range(MAX_ATTEMPTS):
        post = fail_attempt(env, post, T0 - timedelta(hours=2, minutes=-minutes))
    repo.retry(env.conn, post.id, T0)
    publisher = LookupPublisher(found={post.idempotency_key: PublishResult(external_id="late")})

    stage(env, clock, publisher).run_once()

    assert publisher.calls == []
    assert get(env, post).status == PostStatus.POSTED


def test_an_unexpected_error_in_the_cap_check_releases_the_claim(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> int:
        raise RuntimeError("bug")

    monkeypatch.setattr(repo, "uploads_between", broken)
    due = make_post(env)
    publisher = FakePublisher()

    stage(env, clock, publisher, FakeAccount()).run_once()

    assert publisher.calls == []
    assert get(env, due).status == PostStatus.SCHEDULED


def test_cli_retry_with_a_broken_account_rule_fails_cleanly(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipper.posting.cli as posting_cli

    def broken(*_: object, **__: object) -> None:
        raise ValueError("bad window")

    monkeypatch.setattr(posting_cli, "pick_slot", broken)
    post = failed_post(cli_env)

    result = cli(cli_env, "posts", "retry", str(post.id))

    assert result.exit_code == 1
    assert "bad window" in result.stderr and "--at" in result.stderr
    assert get(cli_env, post).status == PostStatus.POST_FAILED


def test_the_attempt_count_uses_an_index(env: Env) -> None:
    plan = env.conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM clip_reviews"
        " WHERE action IN ('post_scheduled', 'post_failed') AND created_at >= ? AND created_at < ?",
        ("a", "b"),
    ).fetchall()

    assert any("clip_reviews_action_time" in str(tuple(row)) for row in plan)
