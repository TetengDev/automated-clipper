"""Campaign link submissions (CLP-97): a posted campaign clip gets a submission record and
an operator ping with the deadline; submission time is recorded and late ones flagged;
earnings roll up to RPM per account and campaign."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipper.campaigns import Campaign
from clipper.cli import app
from clipper.jobs.models import to_iso
from clipper.jobs.worker import ChainedPasses
from clipper.notify import DiscordWebhook, NotifyError
from clipper.posting import repo
from clipper.submissions import (
    REMIND_BEFORE,
    AlreadySubmitted,
    SubmissionPass,
    SubmissionStatus,
    get_submission,
    list_submissions,
    mark_submitted,
    record_earnings,
    rpm_rollup,
)
from review_support import Env, login, make_env

POSTED = datetime(2027, 1, 15, 14, 0, tzinfo=UTC)
WEBHOOK = "https://discord.com/api/webhooks/123/secret-token-abc"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def campaign(**overrides: object) -> Campaign:
    base: dict[str, object] = {
        "id": "reach_kai",
        "program": "reach",
        "creator": "Kai Cenat",
        "platforms": ["tiktok", "youtube"],
        "brief_verified": True,
        "submit_within_minutes": 30,
    }
    base.update(overrides)
    return Campaign.model_validate(base)


CAMPAIGNS = {"reach_kai": campaign()}


def posted_post(
    env: Env,
    *,
    account: str = "kai_tt",
    platform: str = "tiktok",
    campaign_id: str | None = "reach_kai",
    url: str | None = "https://www.tiktok.com/@kai.clips/video/1",
    posted_at: datetime = POSTED,
) -> int:
    clip_id = env.clip(env.job("Kai Cenat"), status="approved", video_path="a.mp4")
    post = repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=posted_at,
        video_path=f"clip-{clip_id}-{account}.mp4",
        now=posted_at - timedelta(hours=1),
        campaign_id=campaign_id,
    )
    env.conn.execute(
        "UPDATE posts SET status = 'posted', posted_at = ?, url = ?, external_id = 'x'"
        " WHERE id = ?",
        (to_iso(posted_at), url, post.id),
    )
    return post.id


class Recorder:
    def __init__(self, fail: int = 0) -> None:
        self.messages: list[str] = []
        self.fail = fail

    def __call__(self, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise NotifyError("discord returned 503")
        self.messages.append(text)


def make_pass(
    env: Env, now: list[datetime], notify: Recorder, alert: Recorder | None = None
) -> SubmissionPass:
    return SubmissionPass(
        conn=env.conn,
        campaigns=CAMPAIGNS,
        notify=notify,
        alert=alert or notify,
        clock=lambda: now[0],
    )


# --- AC2: a posted clip produces a submission record and a ping within 1 minute ---------


def test_posted_campaign_clip_gets_a_submission_and_a_ping_with_the_deadline(env: Env) -> None:
    post_id = posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]  # the next worker poll

    make_pass(env, now, ping).run_once()

    [submission] = list_submissions(env.conn)
    assert submission.post_id == post_id
    assert submission.status == SubmissionStatus.PENDING
    assert submission.url == "https://www.tiktok.com/@kai.clips/video/1"
    assert submission.deadline_at == POSTED + timedelta(minutes=30)
    assert submission.notified_at == now[0]
    [message] = ping.messages
    assert "https://www.tiktok.com/@kai.clips/video/1" in message
    assert "reach_kai" in message
    assert "14:30 UTC" in message
    assert "30 min left" in message
    assert f"clipper campaigns submit {submission.id}" in message


def test_ping_is_sent_once(env: Env) -> None:
    posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, ping)

    sub_pass.run_once()
    now[0] += timedelta(seconds=5)
    sub_pass.run_once()

    assert len(ping.messages) == 1
    assert len(list_submissions(env.conn)) == 1


def test_failed_ping_is_retried_with_backoff_and_the_record_kept(env: Env) -> None:
    posted_post(env)
    ping = Recorder(fail=1)
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, ping)

    sub_pass.run_once()
    [submission] = list_submissions(env.conn)
    assert submission.notified_at is None
    now[0] += timedelta(seconds=5)  # inside the backoff: not retried yet
    sub_pass.run_once()
    assert ping.messages == []
    now[0] += timedelta(seconds=30)
    sub_pass.run_once()

    assert len(ping.messages) == 1
    found = get_submission(env.conn, submission.id)
    assert found is not None and found.notified_at is not None


def test_deadline_counts_from_the_post_time_not_the_pass(env: Env) -> None:
    posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(minutes=12)]  # the worker was down for a while

    make_pass(env, now, ping).run_once()

    [submission] = list_submissions(env.conn)
    assert submission.deadline_at == POSTED + timedelta(minutes=30)
    assert "18 min left" in ping.messages[0]


def test_campaign_without_a_deadline_still_gets_a_record_and_ping(env: Env) -> None:
    env_campaigns = {"vyro_mb": campaign(id="vyro_mb", program="vyro", submit_within_minutes=None)}
    posted_post(env, campaign_id="vyro_mb")
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]

    SubmissionPass(env.conn, env_campaigns, ping, ping, clock=lambda: now[0]).run_once()

    [submission] = list_submissions(env.conn)
    assert submission.deadline_at is None
    assert "no deadline" in ping.messages[0]


@pytest.mark.parametrize(
    ("campaign_id", "url"),
    [
        (None, "https://www.tiktok.com/@kai.clips/video/1"),  # not a campaign post
        ("reach_kai", None),  # no URL from the platform
        ("reach_kai", "javascript:alert(1)"),  # never a link we would hand out
    ],
)
def test_no_submission_without_a_campaign_or_a_safe_url(
    env: Env, campaign_id: str | None, url: str | None
) -> None:
    posted_post(env, campaign_id=campaign_id, url=url)
    ping = Recorder()

    make_pass(env, [POSTED + timedelta(seconds=5)], ping).run_once()

    assert list_submissions(env.conn) == []
    assert all("javascript:" not in m for m in ping.messages)


def test_unconfigured_campaign_still_records_and_pings(env: Env) -> None:
    posted_post(env, campaign_id="gone_campaign")
    ping = Recorder()

    make_pass(env, [POSTED + timedelta(seconds=5)], ping).run_once()

    [submission] = list_submissions(env.conn)
    assert submission.campaign_id == "gone_campaign"
    assert submission.deadline_at is None
    assert "not configured" in ping.messages[0]


def test_reminder_before_the_deadline_and_one_overdue_alert(env: Env) -> None:
    posted_post(env)
    ping, alert = Recorder(), Recorder()
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, ping, alert)
    sub_pass.run_once()

    now[0] = POSTED + timedelta(minutes=30) - REMIND_BEFORE + timedelta(seconds=1)
    sub_pass.run_once()
    sub_pass.run_once()
    now[0] = POSTED + timedelta(minutes=31)
    sub_pass.run_once()
    sub_pass.run_once()

    assert len(ping.messages) == 2
    assert "left" in ping.messages[1] and "reminder" in ping.messages[1].lower()
    [overdue] = alert.messages
    assert "missed" in overdue and "reach_kai" in overdue


def test_submitted_posts_get_no_reminder(env: Env) -> None:
    posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, ping)
    sub_pass.run_once()
    [submission] = list_submissions(env.conn)
    mark_submitted(env.conn, submission.id, POSTED + timedelta(minutes=3), by="cli")

    now[0] = POSTED + timedelta(minutes=40)
    sub_pass.run_once()

    assert len(ping.messages) == 1


def test_a_program_with_a_submitter_is_submitted_automatically(env: Env) -> None:
    posted_post(env)
    calls: list[str] = []

    def whop(submission: object) -> None:
        calls.append(submission.url)

    now = [POSTED + timedelta(seconds=5)]
    SubmissionPass(
        env.conn,
        CAMPAIGNS,
        Recorder(),
        Recorder(),
        clock=lambda: now[0],
        submitters={"reach": whop},
    ).run_once()

    [submission] = list_submissions(env.conn)
    assert calls == ["https://www.tiktok.com/@kai.clips/video/1"]
    assert submission.status == SubmissionStatus.SUBMITTED
    assert submission.submitted_by == "api:reach"


def test_chained_passes_isolate_a_failing_pass() -> None:
    ran: list[str] = []

    class Boom:
        def run_once(self) -> int:
            raise RuntimeError("db locked")

    class Ok:
        def run_once(self) -> int:
            ran.append("ok")
            return 2

    assert ChainedPasses([Boom(), Ok()]).run_once() == 2
    assert ran == ["ok"]


# --- AC3: submission time is recorded; late submissions are flagged ----------------------


def _pending(env: Env) -> int:
    posted_post(env)
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    [submission] = list_submissions(env.conn)
    return submission.id


def test_submission_time_is_recorded_and_on_time_is_not_late(env: Env) -> None:
    sub_id = _pending(env)
    at = POSTED + timedelta(minutes=20)

    done = mark_submitted(env.conn, sub_id, at, by="review")

    assert done.status == SubmissionStatus.SUBMITTED
    assert done.submitted_at == at
    assert done.submitted_by == "review"
    assert done.late is False


def test_late_submission_is_flagged(env: Env) -> None:
    sub_id = _pending(env)

    done = mark_submitted(env.conn, sub_id, POSTED + timedelta(minutes=45), by="cli")

    assert done.late is True


def test_overdue_pending_submission_reads_as_late(env: Env) -> None:
    sub_id = _pending(env)
    found = get_submission(env.conn, sub_id)

    assert found is not None
    assert found.overdue(POSTED + timedelta(minutes=29)) is False
    assert found.overdue(POSTED + timedelta(minutes=31)) is True


def test_submitting_twice_is_refused(env: Env) -> None:
    sub_id = _pending(env)
    mark_submitted(env.conn, sub_id, POSTED + timedelta(minutes=5), by="cli")

    with pytest.raises(AlreadySubmitted):
        mark_submitted(env.conn, sub_id, POSTED + timedelta(minutes=6), by="cli")


def test_submit_is_audited_on_the_clip(env: Env) -> None:
    sub_id = _pending(env)
    mark_submitted(env.conn, sub_id, POSTED + timedelta(minutes=5), by="review")

    rows = env.conn.execute(
        "SELECT payload FROM clip_reviews WHERE action = 'campaign_submitted'"
    ).fetchall()
    [payload] = [json.loads(r[0]) for r in rows]
    assert payload["submission_id"] == sub_id and payload["late"] is False


# --- AC4: earnings roll up to RPM per account and campaign -------------------------------


def test_earnings_roll_up_to_rpm_per_account_and_campaign(env: Env) -> None:
    posted_post(env, account="kai_tt")
    posted_post(env, account="kai_tt")
    posted_post(env, account="kai_yt", platform="youtube")
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    a, b, c = (s.id for s in list_submissions(env.conn))
    now = POSTED + timedelta(days=7)
    record_earnings(env.conn, a, views=100_000, payout=Decimal("80.00"), now=now)
    record_earnings(env.conn, b, views=50_000, payout=Decimal("45.50"), now=now)
    record_earnings(env.conn, c, views=10_000, payout=Decimal("12.00"), now=now)

    rollup = rpm_rollup(env.conn)

    by_account = {r.key: r for r in rollup.by_account}
    assert by_account["kai_tt"].views == 150_000
    assert by_account["kai_tt"].payout == Decimal("125.50")
    assert by_account["kai_tt"].rpm == Decimal("0.84")  # 125.50 / 150 per 1,000, rounded
    assert by_account["kai_yt"].rpm == Decimal("1.20")
    [by_campaign] = rollup.by_campaign
    assert by_campaign.key == "reach_kai"
    assert by_campaign.views == 160_000 and by_campaign.payout == Decimal("137.50")
    assert by_campaign.submissions == 3


def test_submission_without_earnings_does_not_skew_rpm(env: Env) -> None:
    posted_post(env)
    posted_post(env)
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    first = list_submissions(env.conn)[0].id
    record_earnings(env.conn, first, views=1_000, payout=Decimal("1"), now=POSTED)

    [row] = rpm_rollup(env.conn).by_campaign

    assert row.views == 1_000 and row.rpm == Decimal("1.00")
    assert row.submissions == 2 and row.with_earnings == 1


@pytest.mark.parametrize(("views", "payout"), [(-1, Decimal("1")), (1, Decimal("-0.01"))])
def test_negative_earnings_are_refused(env: Env, views: int, payout: Decimal) -> None:
    sub_id = _pending(env)

    with pytest.raises(ValueError):
        record_earnings(env.conn, sub_id, views=views, payout=payout, now=POSTED)


def test_zero_views_has_no_rpm(env: Env) -> None:
    sub_id = _pending(env)
    record_earnings(env.conn, sub_id, views=0, payout=Decimal("0"), now=POSTED)

    [row] = rpm_rollup(env.conn).by_account
    assert row.rpm is None


# --- Discord webhook ---------------------------------------------------------------------


@respx.mock
def test_discord_payload_never_pings_anyone_and_is_cut_to_the_limit() -> None:
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    with httpx.Client() as http:
        DiscordWebhook(WEBHOOK, http).send("@everyone " + "x" * 3000)

    body = json.loads(route.calls.last.request.content)
    assert body["allowed_mentions"] == {"parse": []}
    assert len(body["content"]) <= 2000


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
def test_discord_error_never_shows_the_webhook_url(status: int) -> None:
    respx.post(WEBHOOK).mock(return_value=httpx.Response(status))
    with httpx.Client() as http, pytest.raises(NotifyError) as info:
        DiscordWebhook(WEBHOOK, http).send("hi")

    assert "secret-token-abc" not in str(info.value)
    assert str(status) in str(info.value)


@respx.mock
def test_discord_transport_error_never_shows_the_webhook_url() -> None:
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError(f"cannot reach {WEBHOOK}"))
    with httpx.Client() as http, pytest.raises(NotifyError) as info:
        DiscordWebhook(WEBHOOK, http).send("hi")

    assert "secret-token-abc" not in str(info.value)


def test_discord_webhook_must_be_https_discord() -> None:
    with httpx.Client() as http, pytest.raises(ValueError):
        DiscordWebhook("http://evil.test/hook", http)


# --- review app -------------------------------------------------------------------------


def logged_in(env: Env) -> TestClient:
    client = env.client()
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


def test_campaigns_page_lists_pending_submissions_with_the_link(env: Env) -> None:
    _pending(env)
    client = logged_in(env)

    page = client.get("/campaigns")

    assert page.status_code == 200
    assert 'href="https://www.tiktok.com/@kai.clips/video/1"' in page.text
    assert "reach_kai" in page.text
    assert "Mark submitted" in page.text


def test_campaigns_page_needs_a_login(env: Env) -> None:
    response = env.client().get("/campaigns")

    assert response.status_code in (303, 401)


def test_mark_submitted_from_the_review_app(env: Env) -> None:
    sub_id = _pending(env)
    client = logged_in(env)

    response = client.post(
        f"/campaigns/submissions/{sub_id}/submitted",
        headers={"X-CSRF-Token": client.csrf},  # type: ignore[attr-defined]
    )

    assert response.status_code == 303
    found = get_submission(env.conn, sub_id)
    assert found is not None and found.status == SubmissionStatus.SUBMITTED
    assert found.submitted_by == "review"


def test_mark_submitted_needs_the_csrf_token(env: Env) -> None:
    sub_id = _pending(env)
    client = logged_in(env)

    response = client.post(f"/campaigns/submissions/{sub_id}/submitted")

    assert response.status_code == 403
    found = get_submission(env.conn, sub_id)
    assert found is not None and found.status == SubmissionStatus.PENDING


def test_mark_submitted_twice_is_a_conflict(env: Env) -> None:
    sub_id = _pending(env)
    client = logged_in(env)
    headers = {"X-CSRF-Token": client.csrf}  # type: ignore[attr-defined]
    client.post(f"/campaigns/submissions/{sub_id}/submitted", headers=headers)

    response = client.post(
        f"/campaigns/submissions/{sub_id}/submitted", headers={**headers, "HX-Request": "true"}
    )

    assert response.status_code == 409


def test_campaigns_page_shows_rpm(env: Env) -> None:
    sub_id = _pending(env)
    record_earnings(env.conn, sub_id, views=2_000, payout=Decimal("3"), now=POSTED)

    page = logged_in(env).get("/campaigns")

    assert "1.50" in page.text  # RPM: 3 / 2 per 1,000


# --- CLI --------------------------------------------------------------------------------

runner = CliRunner()


def run(env: Env, *args: str):
    return runner.invoke(app, ["--db", str(env.db_path), "campaigns", *args])


def test_cli_lists_and_submits(env: Env) -> None:
    sub_id = _pending(env)

    listed = run(env, "list")
    submitted = run(env, "submit", str(sub_id))

    assert listed.exit_code == 0, listed.output
    assert "https://www.tiktok.com/@kai.clips/video/1" in listed.output
    assert "pending" in listed.output
    assert submitted.exit_code == 0, submitted.output
    assert "submitted" in submitted.output
    again = run(env, "submit", str(sub_id))
    assert again.exit_code == 1


def test_cli_records_earnings_and_prints_rpm(env: Env) -> None:
    sub_id = _pending(env)

    recorded = run(env, "earnings", str(sub_id), "--views", "4000", "--payout", "5.00")
    rpm = run(env, "rpm")

    assert recorded.exit_code == 0, recorded.output
    assert rpm.exit_code == 0, rpm.output
    assert "kai_tt" in rpm.output and "reach_kai" in rpm.output
    assert "1.25" in rpm.output


def test_cli_refuses_a_bad_payout(env: Env) -> None:
    sub_id = _pending(env)

    result = run(env, "earnings", str(sub_id), "--views", "10", "--payout", "lots")

    assert result.exit_code != 0


def test_cli_unknown_submission(env: Env) -> None:
    assert run(env, "submit", "999").exit_code == 1
