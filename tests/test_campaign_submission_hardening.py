"""Campaign submissions under failure (CLP-97 review findings): two workers, a Discord
outage, a broken config, links first seen after their deadline, short windows, bad
figures and odd URLs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from clipper.campaigns import Campaign
from clipper.config import ConfigError
from clipper.notify import DiscordWebhook, NotifyError
from clipper.submissions import (
    AlreadySubmitted,
    SubmissionPass,
    SubmissionStatus,
    get_submission,
    list_submissions,
    mark_submitted,
    parse_payout,
    pending_submissions,
    recent_submitted,
    record_earnings,
    rpm_rollup,
)
from review_support import Env, make_env
from test_campaign_submissions import (
    CAMPAIGNS,
    POSTED,
    Recorder,
    campaign,
    make_pass,
    posted_post,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


class Down:
    """A webhook that always fails, counting the attempts."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> None:
        self.calls += 1
        raise NotifyError("discord webhook unreachable (ConnectTimeout)")


def test_two_workers_never_send_the_same_ping(env: Env) -> None:
    posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]

    make_pass(env, now, ping).run_once()
    make_pass(env, now, ping).run_once()

    assert len(ping.messages) == 1


def test_a_down_webhook_is_tried_once_per_backoff_not_once_per_row(env: Env) -> None:
    for n in range(3):
        posted_post(env, url=f"https://www.tiktok.com/@kai.clips/video/{n}")
    down = Down()
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, down)  # type: ignore[arg-type]

    sub_pass.run_once()
    now[0] += timedelta(seconds=10)
    sub_pass.run_once()

    assert down.calls == 1
    assert all(s.notified_at is None for s in list_submissions(env.conn))


def test_a_failed_send_is_retried_after_the_backoff(env: Env) -> None:
    posted_post(env)
    ping = Recorder(fail=1)
    now = [POSTED + timedelta(seconds=5)]
    sub_pass = make_pass(env, now, ping)

    sub_pass.run_once()
    now[0] += timedelta(seconds=31)
    sub_pass.run_once()

    assert len(ping.messages) == 1


class BrokenCampaigns(dict[str, Campaign]):
    def get(self, key: str, default: object = None) -> Campaign | None:  # type: ignore[override]
        raise ConfigError("campaigns.yaml: bad")


def test_a_broken_config_still_sends_reminders_and_alerts(env: Env) -> None:
    posted_post(env)
    ping, alert = Recorder(), Recorder()
    now = [POSTED + timedelta(seconds=5)]
    make_pass(env, now, ping, alert).run_once()
    posted_post(env, url="https://www.tiktok.com/@kai.clips/video/2")  # not recorded yet

    now[0] = POSTED + timedelta(minutes=25)
    broken = SubmissionPass(env.conn, BrokenCampaigns(), ping, alert, clock=lambda: now[0])
    broken.run_once()
    now[0] = POSTED + timedelta(minutes=31)
    broken.run_once()

    assert len(list_submissions(env.conn)) == 1
    assert "Reminder" in ping.messages[-1]
    assert len(alert.messages) == 1


def test_a_link_first_seen_after_its_deadline_gets_one_message(env: Env) -> None:
    posted_post(env)
    ping, alert = Recorder(), Recorder()
    now = [POSTED + timedelta(hours=2)]  # posted while the worker was down
    sub_pass = make_pass(env, now, ping, alert)

    sub_pass.run_once()
    now[0] += timedelta(minutes=1)
    sub_pass.run_once()

    assert ping.messages == []
    [missed] = alert.messages
    assert "missed" in missed
    [submission] = list_submissions(env.conn)
    assert submission.notified_at is not None and submission.reminded_at is not None


def test_a_short_window_gets_no_reminder_right_after_the_ping(env: Env) -> None:
    posted_post(env, campaign_id="quick")
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]
    quick = {"quick": campaign(id="quick", submit_within_minutes=5)}
    sub_pass = SubmissionPass(env.conn, quick, ping, ping, clock=lambda: now[0])

    sub_pass.run_once()
    now[0] += timedelta(minutes=1)
    sub_pass.run_once()

    assert len(ping.messages) == 1


def test_a_campaign_back_in_the_config_gets_its_deadline(env: Env) -> None:
    posted_post(env)
    ping = Recorder()
    now = [POSTED + timedelta(seconds=5)]
    SubmissionPass(env.conn, {}, ping, ping, clock=lambda: now[0]).run_once()
    [missing] = list_submissions(env.conn)
    assert missing.deadline_at is None

    now[0] += timedelta(minutes=1)
    make_pass(env, now, ping).run_once()

    found = get_submission(env.conn, missing.id)
    assert found is not None and found.program == "reach"
    assert found.deadline_at == POSTED + timedelta(minutes=30)


def test_one_failed_auto_submit_does_not_stop_the_others(env: Env) -> None:
    posted_post(env, url="https://www.tiktok.com/@kai.clips/video/1")
    posted_post(env, url="https://www.tiktok.com/@kai.clips/video/2")
    calls: list[str] = []

    def submitter(submission: object) -> None:
        url = submission.url  # type: ignore[attr-defined]
        calls.append(url)
        if url.endswith("/1"):
            raise RuntimeError("program API down")

    now = [POSTED + timedelta(seconds=5)]
    SubmissionPass(
        env.conn,
        CAMPAIGNS,
        Recorder(),
        Recorder(),
        clock=lambda: now[0],
        submitters={"reach": submitter},
    ).run_once()

    statuses = sorted(s.status for s in list_submissions(env.conn))
    assert len(calls) == 2
    assert statuses == [SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED]


def test_auto_submit_after_the_operator_is_not_an_error(env: Env) -> None:
    posted_post(env)

    def submitter(submission: object) -> None:
        mark_submitted(env.conn, submission.id, POSTED, by="review")  # type: ignore[attr-defined]

    SubmissionPass(
        env.conn,
        CAMPAIGNS,
        Recorder(),
        Recorder(),
        clock=lambda: POSTED,
        submitters={"reach": submitter},
    ).run_once()

    [submission] = list_submissions(env.conn)
    assert submission.submitted_by == "review"


def test_pending_links_are_all_listed_nearest_deadline_first(env: Env) -> None:
    posted_post(env, url="https://www.tiktok.com/@kai.clips/video/late", posted_at=POSTED)
    posted_post(
        env,
        url="https://www.tiktok.com/@kai.clips/video/early",
        posted_at=POSTED - timedelta(minutes=10),
    )
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    first, second = list_submissions(env.conn)
    mark_submitted(env.conn, first.id, POSTED + timedelta(minutes=1), by="cli")

    assert [s.id for s in pending_submissions(env.conn)] == [second.id]
    assert [s.id for s in recent_submitted(env.conn)] == [first.id]
    with pytest.raises(AlreadySubmitted):
        mark_submitted(env.conn, first.id, POSTED, by="cli")


@pytest.mark.parametrize(
    "url",
    [
        "https://a.example/x\n> @everyone [y](https://evil.example)",
        "https://a.example/x>tail",
        "https://a.example/x y",
    ],
)
def test_a_url_that_could_forge_message_text_is_never_handed_out(env: Env, url: str) -> None:
    posted_post(env, url=url)

    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()

    assert list_submissions(env.conn) == []


@pytest.mark.parametrize("text", ["1e30", "1E+999999999", "1000000000.01", "NaN", "-1"])
def test_a_payout_out_of_range_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_payout(text)


def test_payout_is_normalised_to_cents() -> None:
    assert str(parse_payout("1E+2")) == "100.00"
    assert str(parse_payout("-0")) == "0.00"


def test_record_earnings_bounds_the_figures(env: Env) -> None:
    posted_post(env)
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    [submission] = list_submissions(env.conn)

    with pytest.raises(ValueError):
        record_earnings(env.conn, submission.id, views=10**16, payout=Decimal(1), now=POSTED)
    with pytest.raises(ValueError):
        record_earnings(env.conn, submission.id, views=1, payout=Decimal("1e30"), now=POSTED)


def test_a_stored_figure_that_overflows_never_breaks_the_rollup(env: Env) -> None:
    posted_post(env)
    make_pass(env, [POSTED + timedelta(seconds=5)], Recorder()).run_once()
    env.conn.execute("UPDATE campaign_submissions SET approved_views = 1, payout = '1E+999999999'")

    [row] = rpm_rollup(env.conn).by_account

    assert row.rpm is None


def test_a_webhook_url_httpx_cannot_send_to_is_refused_up_front() -> None:
    with httpx.Client() as http, pytest.raises(ValueError):
        DiscordWebhook("https://discord.com/api/webhooks/1/tok\x7fen", http)


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com:99999/api/webhooks/1/t",
        "https://discord.com:8443/api/webhooks/1/t",
        "https://discord.com/api/webhooks/1/t ok",
    ],
)
def test_a_webhook_url_that_would_fail_every_send_is_refused(url: str) -> None:
    with httpx.Client() as http, pytest.raises(ValueError):
        DiscordWebhook(url, http)


def test_a_payout_with_fractions_of_a_cent_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_payout("12.505")
    assert str(parse_payout("12.500")) == "12.50"


QUICK = {"quick": campaign(id="quick", submit_within_minutes=5)}


def test_first_ping_and_reminder_stamp_are_claimed_together(env: Env) -> None:
    posted_post(env, campaign_id="quick")
    now = [POSTED + timedelta(seconds=5)]
    other = Recorder()

    class Racing(Recorder):
        def __call__(self, text: str) -> None:
            # Another worker polls while this one is sending the first ping.
            SubmissionPass(env.conn, QUICK, other, other, clock=lambda: now[0]).run_once()
            super().__call__(text)

    racing = Racing()
    SubmissionPass(env.conn, QUICK, racing, racing, clock=lambda: now[0]).run_once()

    assert len(racing.messages) == 1 and other.messages == []


def test_a_failed_send_releases_every_stamp_it_claimed(env: Env) -> None:
    posted_post(env, campaign_id="quick")
    down = Down()

    SubmissionPass(env.conn, QUICK, down, down, clock=lambda: POSTED).run_once()  # type: ignore[arg-type]

    [submission] = list_submissions(env.conn)
    assert submission.notified_at is None and submission.reminded_at is None
