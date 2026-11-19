"""The insights job (CLP-154): store each commentary channel's numbers and the numbers of
every Short on it, pipeline posts and hand uploads alike, keyed by YouTube video id.

run-monitor calls `tick()` after every poll cycle; a run happens at most once an hour, and
`tick` never raises, so insights can never stop detection, rendering, approval or
posting. Each run writes one `runs` row (component `insights`) with the rows stored, the
Data API units spent and the errors (redacted).

Per enabled YouTube commentary account:
- Pipeline posts: each posted YouTube post is registered in `channel_videos` with its
  post id, publish time, title and rendered length; its approval tags gain the video id,
  and a post without tags gets them.
- Public (Data API, with YOUTUBE_API_KEY): every upload of the last 90 days from the
  channel's uploads playlist, with title, publish time and duration from videos.list. A
  video of 180 s or less is a Short; longer videos count as public uploads only. One
  `channel_metrics` row per local day (replaced on each run that day) with subscribers,
  total views, the public uploads of the last 90 days and the lifetime views of the
  public Shorts among them (a lower-bound Shorts views estimate). One `post_metrics` row
  per public Short hourly for 48 h after it went public, then once per UTC day, with
  views, likes, comments and whether comments are on.
- Tags: every known Short lacking a key gets the default (length and publish hour from
  the Data API, `unknown` for the rest).
- Analytics (the account's refresh token, scope yt-analytics.readonly): once per UTC day
  (an error is retried after an hour). The channel's daily Shorts views give an
  `analytics` channel row dated the last day they cover, with the 90-day sum. Each known
  Short of the last 90 days gets an `analytics` row with its totals and, for Shorts of
  the last 28 days, its traffic sources and retention curve. A token without the scope,
  or one Google refuses (revoked or invalid), sets the account's state to needs_reauth;
  the public numbers are stored all the same.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from clipper.accounts import Account, token_env_name
from clipper.db import transaction
from clipper.insights import repo
from clipper.insights.analytics import (
    CredentialsRejected,
    NoCredentials,
    ScopeMissing,
    ShortsDay,
    VideoTotals,
)
from clipper.insights.public import ChannelStats, InsightsError, Upload, VideoStats
from clipper.insights.tags import TAG_KEYS, ensure_video_tags, tag_post
from clipper.jobs.models import Clock, to_iso, utcnow
from clipper.posting import repo as posting_repo
from clipper.posting.models import Post
from clipper.redact import redact

INTERVAL = timedelta(hours=1)
HOURLY_FOR = timedelta(hours=48)
SLACK = timedelta(minutes=5)
"""Monitor cycles are jittered: a capture 55 minutes after the last one counts as hourly."""
WINDOW_DAYS = 90
LAG_DAYS = 3
RECENT = timedelta(days=28)
COMPONENT = "insights"

logger = logging.getLogger(__name__)


def _error_text(exc: BaseException) -> str:
    """An error as it may be logged or stored: its type and redacted message."""
    return redact(f"{type(exc).__name__}: {exc}")


class PublicSource(Protocol):
    units: int

    def channel(self, channel_id: str) -> ChannelStats | None: ...
    def uploads_since(self, playlist_id: str, since: datetime) -> list[Upload]: ...
    def video_stats(self, video_ids: Sequence[str]) -> dict[str, VideoStats]: ...
    def comments_enabled(self, video_id: str) -> bool | None: ...


class Reports(Protocol):
    def shorts_days(self, start: date, end: date) -> list[ShortsDay]: ...
    def video_totals(
        self, video_ids: Sequence[str], start: date, end: date
    ) -> dict[str, VideoTotals]: ...
    def traffic(self, video_id: str, start: date, end: date) -> dict[str, int]: ...
    def retention(self, video_id: str, start: date, end: date) -> list[tuple[float, float]]: ...


class AnalyticsSource(Protocol):
    def for_account(self, account_id: str) -> Reports: ...


@dataclass(frozen=True)
class InsightsAccount:
    id: str
    channel_id: str
    timezone: str


def insights_accounts(accounts: Iterable[Account]) -> list[InsightsAccount]:
    """The enabled YouTube commentary accounts (each has a channel_id, CLP-143)."""
    return [
        InsightsAccount(a.id, a.channel_id, a.timezone)
        for a in accounts
        if a.enabled and a.platform == "youtube" and a.content == "commentary" and a.channel_id
    ]


def public_due(went_public: datetime, last: datetime | None, now: datetime) -> bool:
    if last is None:
        return True
    if now - went_public < HOURLY_FOR:
        return now - last >= INTERVAL - SLACK
    return last.date() < now.date()


def analytics_due(state: repo.StateRow | None, now: datetime) -> bool:
    if state is None:
        return True
    if state.state is repo.AnalyticsState.ERROR:
        return now - state.checked_at >= INTERVAL - SLACK
    return state.checked_at.date() < now.date()


def post_video(conn: sqlite3.Connection, post: Post) -> repo.Video:
    """A pipeline post's video as the pipeline knows it: when it goes public, its title
    and its rendered length (always 180 s or less, so a Short)."""
    clip = repo.post_clip(conn, post)
    duration = None if clip is None else clip.duration_s
    try:
        title = posting_repo.metadata_for(conn, post).title or None
    except LookupError:
        title = None
    return repo.Video(
        post.external_id or "", post.account_id, repo.went_public(post), title, duration, post.id
    )


@dataclass
class CollectReport:
    channel_rows: int = 0
    post_rows: int = 0
    """Video metric rows (public and analytics) stored by the run."""
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class InsightsCollector:
    conn: sqlite3.Connection
    accounts: Sequence[InsightsAccount]
    public: PublicSource | None
    """None without YOUTUBE_API_KEY: no public numbers, and only pipeline posts and
    videos seen on earlier runs are known."""
    analytics: AnalyticsSource | None
    clock: Clock = utcnow
    last_run: datetime | None = None

    def tick(self) -> CollectReport | None:
        """The monitor's hook: run when an hour has passed since the last run. Never raises."""
        now = self.clock()
        if self.last_run is not None and now - self.last_run < INTERVAL:
            return None
        self.last_run = now
        try:
            return self.run()
        except Exception as exc:
            logger.error("insights run failed: %s", _error_text(exc))
            return None

    def run(self) -> CollectReport:
        now = self.clock()
        report = CollectReport()
        units_before = self.public.units if self.public is not None else 0
        with transaction(self.conn):
            run_id = self.conn.execute(
                "INSERT INTO runs (component, started_at) VALUES (?, ?)",
                (COMPONENT, to_iso(now)),
            ).lastrowid
        for account in self.accounts:
            with self._contained(report, account, "videos"):
                self._collect(account, now, report)
        units = (self.public.units if self.public is not None else 0) - units_before
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, items_new = ?, quota_units = ?, errors = ?"
                " WHERE id = ?",
                (
                    to_iso(self.clock()),
                    report.channel_rows + report.post_rows,
                    units,
                    json.dumps(report.errors) if report.errors else None,
                    run_id,
                ),
            )
        logger.info(
            "insights: %d channel row(s), %d video row(s), %d unit(s), %d error(s)",
            report.channel_rows,
            report.post_rows,
            units,
            len(report.errors),
        )
        return report

    @contextmanager
    def _contained(
        self,
        report: CollectReport,
        account: InsightsAccount,
        step: str,
        video_id: str | None = None,
        *,
        passthrough: tuple[type[Exception], ...] = (),
    ) -> Iterator[None]:
        """Record and log (redacted) any failure of one account's or one video's work and
        carry on with the next; `passthrough` errors reach the caller."""
        try:
            yield
        except passthrough:
            raise
        except Exception as exc:
            error = _error_text(exc)
            what = f"{step} {video_id}" if video_id else step
            logger.error("insights %s: %s failed: %s", account.id, what, error)
            entry = {"account": account.id, "step": step, "error": error}
            if video_id:
                entry["video"] = video_id
            report.errors.append(entry)

    def _collect(self, account: InsightsAccount, now: datetime, report: CollectReport) -> None:
        self._register_posts(account, now, report)
        if self.public is not None:
            with self._contained(report, account, "public"):
                self._public(account, now, report)
        self._default_tags(account, now, report)
        if self.analytics is not None and analytics_due(
            repo.analytics_state(self.conn, account.id), now
        ):
            with self._contained(report, account, "analytics"):
                self._analytics(account, now, report)

    def _register_posts(
        self, account: InsightsAccount, now: datetime, report: CollectReport
    ) -> None:
        for post in repo.youtube_posts(self.conn, account.id):
            with self._contained(report, account, "post", post.external_id):
                video = post_video(self.conn, post)
                repo.register_post_video(self.conn, video, now)
                repo.link_post_tags(self.conn, post.id, video.video_id)
                if not repo.tags_for_post(self.conn, post.id):
                    tag_post(self.conn, post, account.timezone)

    def _default_tags(self, account: InsightsAccount, now: datetime, report: CollectReport) -> None:
        shorts = repo.channel_videos(self.conn, account.id, until=now, shorts_only=True)
        tagged = repo.tags_for_videos(self.conn, [v.video_id for v in shorts])
        for video in shorts:
            if set(tagged.get(video.video_id, {})) >= set(TAG_KEYS):
                continue
            with self._contained(report, account, "tags", video.video_id):
                ensure_video_tags(self.conn, video, account.timezone)

    def _public(self, account: InsightsAccount, now: datetime, report: CollectReport) -> None:
        assert self.public is not None
        channel = self.public.channel(account.channel_id)
        if channel is None:
            raise InsightsError(f"channel {account.channel_id} not found or not public")
        since = now - timedelta(days=WINDOW_DAYS)
        uploads = (
            self.public.uploads_since(channel.uploads_playlist, since)
            if channel.uploads_playlist
            else []
        )
        known = repo.channel_videos(self.conn, account.id, since=since, until=now)
        ids = [u.video_id for u in uploads] + [v.video_id for v in known]
        stats = self.public.video_stats(list(dict.fromkeys(ids)))  # each id once: quota
        listed = {u.video_id: u.published_at for u in uploads}
        for vid, found in stats.items():
            published = found.published_at or listed.get(vid)
            if published is None:
                continue
            repo.upsert_public_video(
                self.conn,
                repo.Video(vid, account.id, published, found.title, found.duration_s),
                now,
            )
        videos = {
            v.video_id: v
            for v in repo.channel_videos(self.conn, account.id, since=since, until=now)
        }
        shorts = [videos[vid] for vid in stats if vid in videos and videos[vid].short]
        local_day = now.astimezone(ZoneInfo(account.timezone)).date()
        repo.upsert_channel_metrics(
            self.conn,
            repo.ChannelMetrics(
                account.id,
                local_day,
                repo.PUBLIC,
                subscribers=channel.subscribers,
                total_views=channel.total_views,
                shorts_views_90d=sum(stats[v.video_id].views or 0 for v in shorts),
                public_uploads_90d=len(uploads),
            ),
            now,
        )
        report.channel_rows += 1
        for video in shorts:
            last = repo.last_capture(self.conn, video.video_id, repo.PUBLIC)
            if not public_due(video.published_at, last, now):
                continue
            with self._contained(report, account, "public", video.video_id):
                self._public_video(account, video, stats[video.video_id], now)
                report.post_rows += 1

    def _public_video(
        self, account: InsightsAccount, video: repo.Video, found: VideoStats, now: datetime
    ) -> None:
        assert self.public is not None
        enabled = self.public.comments_enabled(video.video_id)
        if enabled is False:
            logger.warning("insights %s: comments are off on %s", account.id, video.video_id)
        repo.add_video_metrics(
            self.conn,
            video,
            repo.PUBLIC,
            repo.PostMetrics(
                views=found.views,
                likes=found.likes,
                comments=found.comments,
                comments_enabled=enabled,
            ),
            now,
        )

    def _analytics(self, account: InsightsAccount, now: datetime, report: CollectReport) -> None:
        assert self.analytics is not None
        try:
            reports = self.analytics.for_account(account.id)
            through = self._channel_analytics(account, reports, now, report)
            shorts = repo.channel_videos(
                self.conn,
                account.id,
                since=now - timedelta(days=WINDOW_DAYS),
                until=now,
                shorts_only=True,
            )
            self._video_analytics(account, shorts, reports, now, through, report)
        except ScopeMissing:
            name = token_env_name(account.id, "refresh_token")
            logger.warning(
                "insights %s: %s lacks the yt-analytics.readonly scope; public numbers only"
                " until the account is re-authorised",
                account.id,
                name,
            )
            detail = f"re-authorise with yt-analytics.readonly and replace {name}"
            repo.set_analytics_state(
                self.conn, account.id, repo.AnalyticsState.NEEDS_REAUTH, detail, now
            )
            return
        except CredentialsRejected as exc:
            name = token_env_name(account.id, "refresh_token")
            detail = f"{redact(str(exc))}; replace {name}"
            logger.warning("insights %s: analytics off until re-authorised: %s", account.id, detail)
            repo.set_analytics_state(
                self.conn, account.id, repo.AnalyticsState.NEEDS_REAUTH, detail, now
            )
            return
        except NoCredentials as exc:
            detail = redact(str(exc))
            logger.warning("insights %s: analytics off: %s", account.id, detail)
            repo.set_analytics_state(
                self.conn, account.id, repo.AnalyticsState.NO_TOKEN, detail, now
            )
            return
        except Exception as exc:
            detail = _error_text(exc)
            repo.set_analytics_state(self.conn, account.id, repo.AnalyticsState.ERROR, detail, now)
            raise
        repo.set_analytics_state(self.conn, account.id, repo.AnalyticsState.OK, None, now)

    def _channel_analytics(
        self, account: InsightsAccount, reports: Reports, now: datetime, report: CollectReport
    ) -> date | None:
        end = now.date()
        days = reports.shorts_days(end - timedelta(days=WINDOW_DAYS + LAG_DAYS), end)
        if not days:
            return None  # no data yet (a new channel, or the 1-3 day lag)
        through = max(d.day for d in days)
        first = through - timedelta(days=WINDOW_DAYS - 1)
        views = sum(d.views for d in days if d.day >= first)
        repo.upsert_channel_metrics(
            self.conn,
            repo.ChannelMetrics(account.id, through, repo.ANALYTICS, shorts_views_90d=views),
            now,
        )
        report.channel_rows += 1
        return through

    def _video_analytics(
        self,
        account: InsightsAccount,
        shorts: list[repo.Video],
        reports: Reports,
        now: datetime,
        through: date | None,
        report: CollectReport,
    ) -> None:
        if not shorts:
            return
        end = now.date()
        start = min(v.published_at for v in shorts).date()
        totals = reports.video_totals([v.video_id for v in shorts], start, end)
        for video in shorts:
            found = totals.get(video.video_id)
            if found is None:
                continue  # no Analytics rows yet
            # A token refused or without the scope stops the account's Analytics, not just
            # this video.
            with self._contained(
                report,
                account,
                "analytics",
                video.video_id,
                passthrough=(ScopeMissing, CredentialsRejected, NoCredentials),
            ):
                self._video_report(video, found, reports, now, through)
                report.post_rows += 1

    def _video_report(
        self,
        video: repo.Video,
        found: VideoTotals,
        reports: Reports,
        now: datetime,
        through: date | None,
    ) -> None:
        end = now.date()
        detailed = video.published_at >= now - RECENT
        first = video.published_at.date()
        watch = (
            found.avg_view_s * found.views
            if found.avg_view_s is not None and found.views is not None
            else None
        )
        repo.add_video_metrics(
            self.conn,
            video,
            repo.ANALYTICS,
            repo.PostMetrics(
                views=found.views,
                likes=found.likes,
                comments=found.comments,
                shares=found.shares,
                watch_time_s=watch,
                engaged_views=found.engaged_views,
                avg_view_s=found.avg_view_s,
                avg_view_pct=found.avg_view_pct,
                subs_gained=found.subs_gained,
                subs_lost=found.subs_lost,
                traffic=reports.traffic(video.video_id, first, end) if detailed else None,
                retention=reports.retention(video.video_id, first, end) if detailed else None,
            ),
            now,
            through_date=through,
        )
