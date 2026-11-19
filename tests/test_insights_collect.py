"""The insights collector (CLP-154) with fake public and Analytics sources: every Short on
the channel (pipeline posts and hand uploads), how often, and that a failure never
escapes `tick`."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from clipper.accounts import Account
from clipper.insights import repo
from clipper.insights.analytics import (
    CredentialsRejected,
    NoCredentials,
    ScopeMissing,
    ShortsDay,
    VideoTotals,
)
from clipper.insights.collect import InsightsAccount, InsightsCollector, insights_accounts
from clipper.insights.public import ChannelStats, InsightsError, Upload, VideoStats
from clipper.insights.tags import TAG_KEYS, owner_tags
from insights_support import ACCOUNT, CHANNEL, HAND_UPLOADS, T0, TIMEZONE, open_db, short, upload

ACC = InsightsAccount(ACCOUNT, CHANNEL, TIMEZONE)
SECRET_KEY = "AIzaSySecretKeyThatMustNeverShow00000"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakePublic:
    def __init__(self) -> None:
        self.units = 0
        self.subscribers: int | None = 320
        self.videos: dict[str, VideoStats] = {}
        self.listed: list[Upload] = []
        self.comments: dict[str, bool | None] = {}
        self.failing: set[str] = set()

    def add(
        self,
        vid: str,
        views: int,
        published: datetime,
        *,
        duration: float = 42.0,
        title: str = "A Short",
    ) -> None:
        """A public video in the channel's uploads playlist."""
        self.videos[vid] = VideoStats(vid, views, 10, 2, title, published, duration)
        self.listed.append(Upload(vid, published))

    def channel(self, channel_id: str) -> ChannelStats | None:
        self.units += 1
        if channel_id in self.failing:
            raise RuntimeError(
                f"GET https://www.googleapis.com/youtube/v3/channels?key={SECRET_KEY}"
            )
        return ChannelStats(self.subscribers, 50_000, len(self.listed), "UU" + channel_id[2:])

    def uploads_since(self, playlist_id: str, since: datetime) -> list[Upload]:
        self.units += 1
        return [u for u in self.listed if u.published_at >= since]

    def video_stats(self, video_ids: Sequence[str]) -> dict[str, VideoStats]:
        self.units += 1
        return {v: self.videos[v] for v in video_ids if v in self.videos}

    def comments_enabled(self, video_id: str) -> bool | None:
        self.units += 1
        return self.comments.get(video_id, True)


class FakeReports:
    def __init__(self, *, days=(), totals=None, error: Exception | None = None) -> None:
        self.days = list(days)
        self.totals: dict[str, VideoTotals] = totals or {}
        self.error = error
        self.asked: list[str] = []

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def shorts_days(self, start: date, end: date) -> list[ShortsDay]:
        self._check()
        return self.days

    def video_totals(
        self, video_ids: Sequence[str], start: date, end: date
    ) -> dict[str, VideoTotals]:
        self._check()
        self.asked = list(video_ids)
        return {v: t for v, t in self.totals.items() if v in video_ids}

    def traffic(self, video_id: str, start: date, end: date) -> dict[str, int]:
        return {"SHORTS": 900, "YT_SEARCH": 100}

    def retention(self, video_id: str, start: date, end: date) -> list[tuple[float, float]]:
        return [(0.0, 1.0), (0.5, 0.62), (1.0, 0.4)]


class FakeAnalytics:
    def __init__(self, reports: FakeReports | None = None, error: Exception | None = None) -> None:
        self.reports = reports or FakeReports()
        self.error = error
        self.accounts: list[str] = []

    def for_account(self, account_id: str) -> FakeReports:
        self.accounts.append(account_id)
        if self.error is not None:
            raise self.error
        return self.reports


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(tmp_path / "state.db")
    yield c
    c.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def collector(conn, clock, public=None, analytics=None, accounts=(ACC,)) -> InsightsCollector:
    return InsightsCollector(conn, list(accounts), public, analytics, clock=clock)


def public_rows(conn: sqlite3.Connection, video_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM post_metrics WHERE video_id = ? AND source = 'public'", (video_id,)
    ).fetchone()[0]


def test_a_run_tracks_every_short_on_the_channel_including_hand_uploads(conn, clock) -> None:
    post = short(conn, scheduled_at=T0 - timedelta(hours=3))
    vid = post.external_id or ""
    public = FakePublic()
    public.add(vid, 1200, T0 - timedelta(hours=3), title="Pipeline Short")
    for i, hand in enumerate(HAND_UPLOADS, 1):
        public.add(hand, 100 * i, T0 - timedelta(days=i), title=f"Hand {i}", duration=58.0)
    public.add("longVideo01", 5000, T0 - timedelta(days=4), duration=600.0)

    report = collector(conn, clock, public).run()

    assert (report.channel_rows, report.post_rows, report.errors) == (1, 4, [])
    (row,) = repo.channel_history(conn, ACCOUNT, repo.PUBLIC, date(2027, 1, 1))
    assert row == repo.ChannelMetrics(
        ACCOUNT,
        date(2027, 1, 15),
        repo.PUBLIC,
        subscribers=320,
        total_views=50_000,
        shorts_views_90d=1200 + 100 + 200 + 300,
        public_uploads_90d=5,
    )
    mine = repo.latest_video_metrics(conn, vid, repo.PUBLIC)
    assert mine is not None and mine.post_id == post.id
    assert (mine.metrics.views, mine.metrics.likes, mine.metrics.comments_enabled) == (
        1200,
        10,
        True,
    )
    for i, hand in enumerate(HAND_UPLOADS, 1):
        snap = repo.latest_video_metrics(conn, hand, repo.PUBLIC)
        assert snap is not None and snap.post_id is None and snap.metrics.views == 100 * i
        video = repo.get_video(conn, hand)
        assert video is not None and video.title == f"Hand {i}" and video.short
    assert repo.latest_video_metrics(conn, "longVideo01", repo.PUBLIC) is None
    long_video = repo.get_video(conn, "longVideo01")
    assert long_video is not None and long_video.short is False
    tags = repo.tags_for_videos(conn, [vid, *HAND_UPLOADS])
    assert all(set(found) == set(TAG_KEYS) for found in tags.values()) and len(tags) == 4
    assert tags[HAND_UPLOADS[0]]["length_bucket"] == "40-60s"
    assert tags[HAND_UPLOADS[0]]["hook_type"] == "unknown"
    run = conn.execute("SELECT * FROM runs WHERE component = 'insights'").fetchone()
    assert run["finished_at"] and run["items_new"] == 5 and run["errors"] is None
    assert run["quota_units"] == public.units


def test_a_short_that_is_not_public_yet_gets_no_row(conn, clock) -> None:
    post = short(conn, scheduled_at=T0 + timedelta(hours=2))  # publishAt still ahead

    report = collector(conn, clock, FakePublic()).run()

    assert report.post_rows == 0
    assert repo.latest_video_metrics(conn, post.external_id or "", repo.PUBLIC) is None


def test_public_numbers_hourly_for_48_hours_then_daily(conn, clock) -> None:
    public = FakePublic()
    public.add(HAND_UPLOADS[0], 10, T0)
    c = collector(conn, clock, public)
    vid = HAND_UPLOADS[0]

    clock.now = T0 + timedelta(minutes=10)
    c.run()
    assert public_rows(conn, vid) == 1
    clock.now += timedelta(minutes=30)
    c.run()
    assert public_rows(conn, vid) == 1
    clock.now += timedelta(minutes=30)
    c.run()
    assert public_rows(conn, vid) == 2
    clock.now = T0 + timedelta(hours=50)
    c.run()
    assert public_rows(conn, vid) == 3
    clock.now = T0 + timedelta(hours=51)  # same UTC day: daily now
    c.run()
    assert public_rows(conn, vid) == 3
    clock.now = T0 + timedelta(hours=60)  # the next UTC day
    c.run()
    assert public_rows(conn, vid) == 4


def test_without_the_analytics_scope_public_numbers_stay_and_reauth_is_flagged(
    conn, clock, caplog: pytest.LogCaptureFixture
) -> None:
    public = FakePublic()
    public.add(HAND_UPLOADS[0], 500, T0 - timedelta(days=3))
    missing = ScopeMissing(
        "the token lacks the scope", status=403, reasons=["insufficientPermissions"]
    )
    analytics = FakeAnalytics(FakeReports(error=missing))

    with caplog.at_level(logging.WARNING):
        report = collector(conn, clock, public, analytics).run()

    assert report.errors == []
    snap = repo.latest_video_metrics(conn, HAND_UPLOADS[0], repo.PUBLIC)
    assert snap is not None and snap.metrics.views == 500
    assert repo.latest_video_metrics(conn, HAND_UPLOADS[0], repo.ANALYTICS) is None
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.NEEDS_REAUTH
    assert "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT" in (state.detail or "")
    assert "yt-analytics.readonly" in caplog.text


def test_analytics_cover_pipeline_posts_and_known_hand_uploads(conn, clock) -> None:
    post = short(conn, scheduled_at=T0 - timedelta(days=5))  # no API key: its clip length
    vid = post.external_id or ""
    upload(conn, HAND_UPLOADS[0], published_at=T0 - timedelta(days=4))  # seen on a past run
    upload(conn, "longVideo01", published_at=T0 - timedelta(days=4), duration_s=600.0)
    days = [ShortsDay(date(2027, 1, d), 1000 * d, 2, 1) for d in range(10, 14)]
    totals = {
        vid: VideoTotals(4000, 3100, 21.0, 55.5, 12, 1, 300, 20, 15),
        HAND_UPLOADS[0]: VideoTotals(900, 700, 30.0, 70.0, 3, 0, 50, 4, 2),
    }
    reports = FakeReports(days=days, totals=totals)

    collector(conn, clock, None, FakeAnalytics(reports)).run()

    assert sorted(reports.asked) == sorted([vid, HAND_UPLOADS[0]])  # no long video
    (row,) = repo.channel_history(conn, ACCOUNT, repo.ANALYTICS, date(2027, 1, 1))
    assert row.day == date(2027, 1, 13) and row.shorts_views_90d == 46_000
    snap = repo.latest_video_metrics(conn, vid, repo.ANALYTICS)
    assert snap is not None and snap.post_id == post.id
    assert snap.through_date == date(2027, 1, 13)
    assert snap.metrics.engaged_views == 3100 and snap.metrics.watch_time_s == 84_000.0
    assert snap.metrics.traffic == {"SHORTS": 900, "YT_SEARCH": 100}
    assert snap.metrics.retention == [(0.0, 1.0), (0.5, 0.62), (1.0, 0.4)]
    hand = repo.latest_video_metrics(conn, HAND_UPLOADS[0], repo.ANALYTICS)
    assert hand is not None and hand.post_id is None and hand.metrics.views == 900
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.OK


def test_empty_analytics_reports_store_nothing_and_are_ok(conn, clock) -> None:
    short(conn, scheduled_at=T0 - timedelta(hours=2))  # published today: no rows yet

    report = collector(conn, clock, None, FakeAnalytics(FakeReports())).run()

    assert report.errors == [] and report.channel_rows == 0 and report.post_rows == 0
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.OK


def test_analytics_once_a_utc_day_and_an_error_is_retried_after_an_hour(conn, clock) -> None:
    analytics = FakeAnalytics(FakeReports(days=[ShortsDay(date(2027, 1, 13), 10, 0, 0)]))
    c = collector(conn, clock, None, analytics)

    c.run()
    clock.now += timedelta(hours=2)
    c.run()
    assert len(analytics.accounts) == 1
    clock.now = T0 + timedelta(days=1)
    c.run()
    assert len(analytics.accounts) == 2

    analytics.error = InsightsError("Analytics reports.query failed: HTTP 500 backendError")
    clock.now = T0 + timedelta(days=2)
    report = c.run()
    assert report.errors and len(analytics.accounts) == 3
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.ERROR
    clock.now += timedelta(minutes=30)
    c.run()
    assert len(analytics.accounts) == 3
    clock.now += timedelta(minutes=30)
    c.run()
    assert len(analytics.accounts) == 4


def test_one_failing_account_does_not_stop_the_others_and_errors_are_redacted(
    conn, clock, caplog: pytest.LogCaptureFixture
) -> None:
    other = InsightsAccount("second_channel_yt", "UCaaaaaaaaaaaaaaaaaaaaaa", TIMEZONE)
    public = FakePublic()
    public.failing = {other.channel_id}

    report = collector(conn, clock, public, accounts=(other, ACC)).run()

    assert [e["account"] for e in report.errors] == ["second_channel_yt"]
    assert repo.channel_history(conn, ACCOUNT, repo.PUBLIC, date(2027, 1, 1))
    stored = conn.execute("SELECT errors FROM runs WHERE component = 'insights'").fetchone()[0]
    assert SECRET_KEY not in stored and SECRET_KEY not in json.dumps(report.errors)
    assert SECRET_KEY not in caplog.text


def test_tick_runs_at_most_hourly_and_never_raises(conn, clock, monkeypatch) -> None:
    c = collector(conn, clock, FakePublic())

    assert c.tick() is not None
    clock.now += timedelta(minutes=59)
    assert c.tick() is None
    clock.now += timedelta(minutes=1)
    assert c.tick() is not None

    def boom() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(c, "run", boom)
    clock.now += timedelta(hours=1)
    assert c.tick() is None


def test_posts_without_tags_get_them_by_post_and_by_video(conn, clock) -> None:
    post = short(conn)

    collector(conn, clock, FakePublic()).run()

    vid = post.external_id or ""
    assert set(repo.tags_for_post(conn, post.id)) == set(TAG_KEYS)
    assert repo.tags_for_videos(conn, [vid])[vid] == repo.tags_for_post(conn, post.id)


def test_approval_tags_follow_the_post_to_its_video(conn, clock) -> None:
    post = short(conn)
    repo.set_tags(conn, {"hook_type": "question"}, post_id=post.id)  # tagged at approval

    collector(conn, clock, None).run()

    vid = post.external_id or ""
    tags = repo.tags_for_videos(conn, [vid])[vid]
    assert tags["hook_type"] == "question" and set(tags) == set(TAG_KEYS)


def test_insights_accounts_are_enabled_youtube_commentary_accounts() -> None:
    base = {"platform": "youtube", "market": "US", "timezone": TIMEZONE}
    accounts = [
        Account.model_validate(
            {
                **base,
                "id": ACCOUNT,
                "handle": "@RandomGaming",
                "content": "commentary",
                "channel_id": CHANNEL,
            }
        ),
        Account.model_validate(
            {
                **base,
                "id": "off_yt",
                "handle": "@Off",
                "enabled": False,
                "content": "commentary",
                "channel_id": "UCbbbbbbbbbbbbbbbbbbbbbb",
            }
        ),
        Account.model_validate(
            {**base, "id": "clips_yt", "handle": "@Clips", "creators": ["MrBeast"]}
        ),
    ]

    assert insights_accounts(accounts) == [ACC]


def test_the_collector_keeps_the_owners_tags_on_a_pipeline_short(conn, clock) -> None:
    vid = HAND_UPLOADS[0]
    owner_tags(conn, vid, {"hook_type": "reaction"})  # the owner tagged it by video id first
    post = short(conn, video_id=vid, scheduled_at=T0 - timedelta(hours=3))

    collector(conn, clock, None).run()

    tags = repo.tags_for_videos(conn, [vid])[vid]
    assert tags["hook_type"] == "reaction"
    assert tags["length_bucket"] == "40-60s"  # derived from the rendered length
    assert repo.tags_for_post(conn, post.id) == tags


class FlakyPublic(FakePublic):
    """`comments_enabled` fails for one video, as a malformed response would."""

    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    def comments_enabled(self, video_id: str) -> bool | None:
        if video_id == self.broken:
            raise AttributeError("'list' object has no attribute 'get'")
        return super().comments_enabled(video_id)


def test_one_failing_video_does_not_stop_the_others(conn, clock) -> None:
    public = FlakyPublic(HAND_UPLOADS[0])
    for hand in HAND_UPLOADS:
        public.add(hand, 100, T0 - timedelta(days=1))

    report = collector(conn, clock, public).run()

    assert report.post_rows == 2 and report.channel_rows == 1
    assert [(e["video"], e["step"]) for e in report.errors] == [(HAND_UPLOADS[0], "public")]
    assert repo.latest_video_metrics(conn, HAND_UPLOADS[0], repo.PUBLIC) is None
    for hand in HAND_UPLOADS[1:]:
        assert public_rows(conn, hand) == 1


class FlakyReports(FakeReports):
    """`traffic` fails for one video."""

    def __init__(self, broken: str, failure: Exception, **kwargs) -> None:
        super().__init__(**kwargs)
        self.broken = broken
        self.failure = failure

    def traffic(self, video_id: str, start: date, end: date) -> dict[str, int]:
        if video_id == self.broken:
            raise self.failure
        return super().traffic(video_id, start, end)


def test_one_failing_video_does_not_stop_the_analytics_of_the_others(conn, clock) -> None:
    for hand in HAND_UPLOADS:
        upload(conn, hand, published_at=T0 - timedelta(days=2))
    totals = {hand: VideoTotals(900, 700, 30.0, 70.0, 3, 0, 50, 4, 2) for hand in HAND_UPLOADS}
    reports = FlakyReports(HAND_UPLOADS[1], ValueError("invalid video id"), totals=totals)

    report = collector(conn, clock, None, FakeAnalytics(reports)).run()

    assert report.post_rows == 2
    assert [(e["video"], e["step"]) for e in report.errors] == [(HAND_UPLOADS[1], "analytics")]
    assert repo.latest_video_metrics(conn, HAND_UPLOADS[1], repo.ANALYTICS) is None
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.OK


def test_a_scope_error_on_a_video_report_flags_reauth(conn, clock) -> None:
    upload(conn, HAND_UPLOADS[0], published_at=T0 - timedelta(days=2))
    totals = {HAND_UPLOADS[0]: VideoTotals(900, 700, 30.0, 70.0, 3, 0, 50, 4, 2)}
    missing = ScopeMissing("the token lacks the scope", status=403)
    reports = FlakyReports(HAND_UPLOADS[0], missing, totals=totals)

    report = collector(conn, clock, None, FakeAnalytics(reports)).run()

    assert report.errors == []
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.NEEDS_REAUTH


def test_an_unexpected_analytics_error_is_contained_and_redacted(
    conn, clock, caplog: pytest.LogCaptureFixture
) -> None:
    upload(conn, HAND_UPLOADS[0], published_at=T0 - timedelta(days=2))
    analytics = FakeAnalytics(error=KeyError(f"https://x/?access_token={SECRET_KEY}"))
    public = FakePublic()
    public.add(HAND_UPLOADS[0], 500, T0 - timedelta(days=2))

    report = collector(conn, clock, public, analytics).run()

    assert [e["step"] for e in report.errors] == ["analytics"]
    assert public_rows(conn, HAND_UPLOADS[0]) == 1
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.ERROR
    assert SECRET_KEY not in (state.detail or "") and SECRET_KEY not in caplog.text


def test_without_credentials_analytics_are_off_and_public_numbers_stay(conn, clock) -> None:
    public = FakePublic()
    public.add(HAND_UPLOADS[0], 500, T0 - timedelta(days=3))
    off = NoCredentials("CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT is not set")

    report = collector(conn, clock, public, FakeAnalytics(error=off)).run()

    assert report.errors == [] and public_rows(conn, HAND_UPLOADS[0]) == 1
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.NO_TOKEN
    assert state.detail == "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT is not set"


def test_a_revoked_token_flags_reauth_and_public_numbers_stay(conn, clock) -> None:
    public = FakePublic()
    public.add(HAND_UPLOADS[0], 500, T0 - timedelta(days=3))
    revoked = CredentialsRejected(
        "Google rejected the OAuth credentials (invalid_grant); re-authorise the account"
    )

    report = collector(conn, clock, public, FakeAnalytics(error=revoked)).run()

    assert report.errors == [] and public_rows(conn, HAND_UPLOADS[0]) == 1
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.NEEDS_REAUTH
    assert "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT" in (state.detail or "")
    assert "invalid_grant" in (state.detail or "")


def test_a_revoked_token_on_a_video_report_flags_reauth(conn, clock) -> None:
    upload(conn, HAND_UPLOADS[0], published_at=T0 - timedelta(days=2))
    totals = {HAND_UPLOADS[0]: VideoTotals(900, 700, 30.0, 70.0, 3, 0, 50, 4, 2)}
    revoked = CredentialsRejected("Google rejected the OAuth credentials (invalid_grant)")
    reports = FlakyReports(HAND_UPLOADS[0], revoked, totals=totals)

    report = collector(conn, clock, None, FakeAnalytics(reports)).run()

    assert report.errors == []
    state = repo.analytics_state(conn, ACCOUNT)
    assert state is not None and state.state is repo.AnalyticsState.NEEDS_REAUTH
