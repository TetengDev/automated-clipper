"""What the insights page and the weekly summary show (CLP-154)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from clipper.insights import repo, report
from clipper.insights.repo import ANALYTICS, PUBLIC, ChannelMetrics
from insights_support import ACCOUNT, HAND_UPLOADS, T0, open_db, short, upload

TODAY = date(2027, 1, 15)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(tmp_path / "state.db")
    yield c
    c.close()


def channel(conn, day: date, source: str = PUBLIC, **values: int) -> None:
    repo.upsert_channel_metrics(conn, ChannelMetrics(ACCOUNT, day, source, **values), T0)


def seed_week(conn) -> None:
    channel(
        conn,
        TODAY - timedelta(days=7),
        subscribers=300,
        public_uploads_90d=4,
        shorts_views_90d=860_000,
        total_views=40_000,
    )
    channel(
        conn,
        TODAY,
        subscribers=370,
        public_uploads_90d=5,
        shorts_views_90d=1_000_000,
        total_views=50_000,
    )


def test_tier_one_progress_and_days_to_go(conn) -> None:
    seed_week(conn)

    tier1, tier2 = report.tracker(conn, ACCOUNT, TODAY)

    subs, uploads, views = tier1.goals
    assert (subs.value, subs.days_to_go, subs.percent) == (370, 13, 74)
    assert subs.outlook == "about 13 days at the last 7 days' pace"
    assert uploads.reached and uploads.outlook == "reached"
    assert (views.value, views.days_to_go) == (1_000_000, 100)
    assert views.goal.estimate and "estimate" in views.goal.label
    assert tier1.days_to_go == 100 and not tier1.reached
    assert [g.days_to_go for g in tier2.goals] == [63, 450]


def test_shorts_views_come_from_analytics_when_there_are_any(conn) -> None:
    seed_week(conn)
    channel(conn, date(2027, 1, 6), ANALYTICS, shorts_views_90d=1_100_000)
    channel(conn, date(2027, 1, 13), ANALYTICS, shorts_views_90d=1_240_000)

    views = report.tracker(conn, ACCOUNT, TODAY)[0].goals[2]

    assert (views.value, views.as_of, views.days_to_go) == (1_240_000, date(2027, 1, 13), 88)


def test_flat_or_short_history_has_no_days_to_go(conn) -> None:
    channel(conn, TODAY - timedelta(days=7), subscribers=300)
    channel(conn, TODAY, subscribers=300)

    subs = report.tracker(conn, ACCOUNT, TODAY)[0].goals[0]
    assert subs.days_to_go is None and subs.outlook == "no progress in the last 7 days"

    other = "second_yt"
    repo.upsert_channel_metrics(conn, ChannelMetrics(other, TODAY, PUBLIC, subscribers=10), T0)
    new = report.tracker(conn, other, TODAY)[0]
    assert new.goals[0].outlook == "pace known after 7 days of data"
    assert new.goals[1].outlook == "no data yet" and new.goals[1].percent is None
    assert new.days_to_go is None and new.outlook == "no estimate yet"


def test_trend_merges_public_rows_with_analytics_views(conn) -> None:
    seed_week(conn)
    channel(conn, TODAY, ANALYTICS, shorts_views_90d=1_300_000)
    channel(conn, TODAY - timedelta(days=40), subscribers=1)  # outside 28 days

    days = report.trend(conn, ACCOUNT, TODAY)

    assert [d.day for d in days] == [TODAY - timedelta(days=7), TODAY]
    assert (days[-1].subscribers, days[-1].shorts_views_90d, days[-1].shorts_views_source) == (
        370,
        1_300_000,
        "analytics",
    )
    assert days[0].shorts_views_source == "public"


def views(conn, video: repo.Video, count: int) -> None:
    repo.add_video_metrics(conn, video, PUBLIC, repo.PostMetrics(views=count), T0)


def test_shorts_table_has_pipeline_posts_and_hand_uploads_with_ratio_to_the_median(conn) -> None:
    post = short(conn, title="A", scheduled_at=T0 - timedelta(days=1))
    a = upload(
        conn,
        post.external_id or "",
        title="A",
        published_at=T0 - timedelta(days=1),
        post_id=post.id,
    )
    b = upload(conn, HAND_UPLOADS[0], title="B", published_at=T0 - timedelta(days=2))
    c = upload(conn, HAND_UPLOADS[1], title="C", published_at=T0 - timedelta(days=3))
    old = upload(conn, HAND_UPLOADS[2], title="Old", published_at=T0 - timedelta(days=40))
    long_video = upload(conn, "longVideo01", title="Long", published_at=T0, duration_s=600.0)
    for video, n in ((a, 400), (b, 200), (c, 100), (old, 9_999), (long_video, 50_000)):
        views(conn, video, n)
    repo.add_video_metrics(
        conn,
        a,
        ANALYTICS,
        repo.PostMetrics(
            views=380,
            engaged_views=300,
            avg_view_pct=61.0,
            subs_gained=4,
            shares=7,
            traffic={"SHORTS": 90, "YT_SEARCH": 10},
        ),
        T0,
        through_date=date(2027, 1, 13),
    )

    rows = report.shorts_table(conn, ACCOUNT, T0)

    assert [r.title for r in rows] == ["A", "B", "C"]
    assert [r.origin for r in rows] == ["pipeline", "hand upload", "hand upload"]
    assert rows[1].url == "https://www.youtube.com/shorts/2BpbZneO_To"
    assert [r.ratio for r in rows] == [2.0, 1.0, 0.5]
    assert rows[0].views == 400  # the fresher public number
    assert (rows[0].engaged_views, rows[0].shares, rows[0].through_date) == (
        300,
        7,
        date(2027, 1, 13),
    )
    assert rows[0].traffic_mix == [("SHORTS", 90), ("YT_SEARCH", 10)]


def test_a_short_without_public_numbers_shows_its_analytics_views(conn) -> None:
    video = upload(conn, HAND_UPLOADS[0], published_at=T0 - timedelta(days=2))
    repo.add_video_metrics(conn, video, ANALYTICS, repo.PostMetrics(views=77), T0)

    (row,) = report.shorts_table(conn, ACCOUNT, T0)

    assert row.views == 77 and row.ratio == 1.0 and row.title == "Hand upload"


def test_tag_breakdown_and_findings(conn) -> None:
    hooks = {"A": ("question", 400), "B": ("question", 200), "C": ("list", 100), "D": ("list", 100)}
    for i, (title, (hook, n)) in enumerate(hooks.items()):
        video = upload(conn, f"video{i:06d}", title=title, published_at=T0 - timedelta(days=1))
        views(conn, video, n)
        repo.set_tags(conn, {"hook_type": hook, "palette": "unknown"}, video_id=video.video_id)

    rows = report.shorts_table(conn, ACCOUNT, T0)
    breakdown = report.tag_breakdown(rows)
    more, less = report.findings(breakdown)

    question, listing = breakdown["hook_type"]
    assert (question.value, question.shorts) == ("question", 2)
    assert question.median_ratio == pytest.approx(2.0)  # median of 400/150 and 200/150
    assert listing.median_ratio == pytest.approx(100 / 150)
    assert [(g.key, g.value) for g in more] == [("hook_type", "question")]
    assert [(g.key, g.value) for g in less] == [("hook_type", "list")]
    assert "palette" in breakdown  # shown on the page, but 'unknown' never becomes a finding
