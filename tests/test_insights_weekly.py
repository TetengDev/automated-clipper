"""The weekly insights summary (CLP-154): content, once a week, retried after a failed
send, and sent by the collector's tick."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from clipper.insights import repo
from clipper.insights.collect import InsightsAccount, InsightsCollector
from clipper.insights.repo import ANALYTICS, PUBLIC, ChannelMetrics
from clipper.insights.report import FINDINGS_MIN_AGE_DAYS
from clipper.insights.weekly import build_summary, send_weekly, summary_due, week_start
from clipper.notify import NotifyError
from insights_support import ACCOUNT, CHANNEL, T0, TIMEZONE, open_db, upload

ACC = InsightsAccount(ACCOUNT, CHANNEL, TIMEZONE)
MONDAY = datetime(2027, 1, 18, 15, 0, tzinfo=UTC)  # 10:00 in New York
TOKEN_VAR = "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(tmp_path / "state.db")
    yield c
    c.close()


def seed(conn: sqlite3.Connection) -> None:
    """Four settled Shorts (old enough for the tag findings) and two weeks of subscribers."""
    settled = T0 - timedelta(days=FINDINGS_MIN_AGE_DAYS + 1)
    shorts = {
        "Win": ("question", 400),
        "Good": ("question", 200),
        "Meh": ("list", 100),
        "Flop": ("list", 50),
    }
    for i, (title, (hook, n)) in enumerate(shorts.items()):
        video = upload(conn, f"weekly{i:05d}", title=title, published_at=settled)
        repo.add_video_metrics(conn, video, PUBLIC, repo.PostMetrics(views=n), T0)
        repo.set_tags(conn, {"hook_type": hook}, video_id=video.video_id)
    for day, subs in ((date(2027, 1, 8), 300), (date(2027, 1, 15), 370)):
        repo.upsert_channel_metrics(
            conn,
            ChannelMetrics(
                ACCOUNT,
                day,
                PUBLIC,
                subscribers=subs,
                public_uploads_90d=5,
                shorts_views_90d=1_000_000,
            ),
            T0,
        )


def test_week_start_is_monday_nine_in_the_channel_timezone() -> None:
    before = datetime(2027, 1, 18, 13, 59, tzinfo=UTC)  # 08:59 in New York
    assert week_start(before, TIMEZONE) == datetime(2027, 1, 11, 14, 0, tzinfo=UTC)
    assert week_start(MONDAY, TIMEZONE) == datetime(2027, 1, 18, 14, 0, tzinfo=UTC)


def test_the_summary_names_top_bottom_tags_and_tracker(conn: sqlite3.Connection) -> None:
    seed(conn)
    repo.set_analytics_state(conn, ACCOUNT, repo.AnalyticsState.NEEDS_REAUTH, "x", T0)

    text = build_summary(conn, ACC, T0)

    assert text.startswith(f"Weekly insights for {ACCOUNT}")
    top = text.split("Top:")[1].split("Bottom:")[0]
    assert "1. Win (400 views, 2.7x median)" in top
    assert "Flop" in text.split("Bottom:")[1]
    assert "Do more: hook_type=question (2.0x, 2 Shorts)" in text
    assert "Do less: hook_type=list (0.5x, 2 Shorts)" in text
    assert "Tier 1: fan funding" in text and "Subscribers 370/500" in text
    assert "needs re-authorisation" in text and TOKEN_VAR in text
    assert f"Details: /insights/{ACCOUNT}" in text
    assert len(text) <= 2000


def test_the_tracker_line_counts_shorts_published_after_the_latest_analytics_day(
    conn: sqlite3.Connection,
) -> None:
    """The live run of 2026-11-19: Analytics covered a day before the three Shorts went
    public and said 0; the public figure counts them."""
    today = date(2027, 1, 15)  # T0 in New York
    for row in (
        ChannelMetrics(
            ACCOUNT, today, PUBLIC, subscribers=2, shorts_views_90d=483, public_uploads_90d=3
        ),
        ChannelMetrics(ACCOUNT, today - timedelta(days=3), ANALYTICS, shorts_views_90d=0),
    ):
        repo.upsert_channel_metrics(conn, row, T0)

    text = build_summary(conn, ACC, T0)

    assert "Public Shorts views, last 90 days (estimate) 483/3,000,000" in text
    assert "(estimate) 0/" not in text


def test_fresh_shorts_are_listed_but_make_no_findings(conn: sqlite3.Connection) -> None:
    for i, (hook, views) in enumerate((("question", 400), ("question", 300), ("list", 10))):
        video = upload(conn, f"fresh{i:06d}", published_at=T0 - timedelta(days=1))
        repo.add_video_metrics(conn, video, PUBLIC, repo.PostMetrics(views=views), T0)
        repo.set_tags(conn, {"hook_type": hook}, video_id=video.video_id)

    text = build_summary(conn, ACC, T0)

    assert "Top:" in text
    assert "Do more" not in text and "Do less" not in text
    assert "Per tag: not enough" in text


def test_an_empty_channel_still_gets_a_summary_with_no_data_not_zero(
    conn: sqlite3.Connection,
) -> None:
    text = build_summary(conn, ACC, T0)

    assert "No public Shorts with numbers yet." in text
    assert "Tier 2: ad revenue" in text
    assert "Subscribers: no data yet" in text
    assert "0/500" not in text and " 0 " not in text


def test_an_account_without_a_token_is_named_by_variable(conn: sqlite3.Connection) -> None:
    repo.set_analytics_state(
        conn, ACCOUNT, repo.AnalyticsState.NO_TOKEN, f"no refresh token (set {TOKEN_VAR})", T0
    )

    text = build_summary(conn, ACC, T0)

    assert f"Analytics is off: {TOKEN_VAR} or the OAuth client is not set" in text


def test_sent_once_a_week(conn: sqlite3.Connection) -> None:
    sent: list[str] = []

    assert send_weekly(conn, ACC, MONDAY, sent.append) is True
    assert send_weekly(conn, ACC, MONDAY + timedelta(days=3), sent.append) is False
    assert summary_due(conn, ACC, MONDAY + timedelta(days=7)) is True
    assert send_weekly(conn, ACC, MONDAY + timedelta(days=7), sent.append) is True
    assert len(sent) == 2


def test_a_failed_send_is_retried(conn: sqlite3.Connection) -> None:
    def down(text: str) -> None:
        raise NotifyError("discord webhook returned 503", status=503)

    assert send_weekly(conn, ACC, MONDAY, down) is False
    sent: list[str] = []
    assert send_weekly(conn, ACC, MONDAY + timedelta(hours=1), sent.append) is True
    assert len(sent) == 1


def test_the_collector_tick_sends_the_weekly_summary(conn: sqlite3.Connection) -> None:
    sent: list[str] = []
    collector = InsightsCollector(conn, [ACC], None, None, notify=sent.append, clock=lambda: MONDAY)

    collector.tick()

    assert len(sent) == 1 and sent[0].startswith("Weekly insights")


def test_a_crashing_notify_never_stops_the_tick(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(text: str) -> None:
        raise RuntimeError("boom")

    collector = InsightsCollector(conn, [ACC], None, None, notify=broken, clock=lambda: MONDAY)

    with caplog.at_level(logging.ERROR):
        collector.tick()

    assert f"insights {ACCOUNT}: weekly summary failed: RuntimeError: boom" in caplog.text
