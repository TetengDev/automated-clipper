"""What the insights page and the weekly summary show (CLP-154): the monetisation
tracker, the 28-day trend, the Shorts table with each Short's ratio to the channel
median, and the per-tag breakdown. Read-only queries over the 0012 tables.

Tracker. Tier 1 (fan funding): 500 subscribers, 3 public uploads in the last 90 days and
3,000,000 public Shorts views in the last 90 days. Tier 2 (ad revenue): 1,000 subscribers
and 10,000,000 public Shorts views in the last 90 days. The views figure is an estimate:
on each day, the larger of the public figure (lifetime views of the public Shorts of the
last 90 days) and the latest Analytics `views` with `creatorContentType==shorts` covering
that day or before. Both are lower bounds: Analytics is exact but 1 to 3 days late, so it
misses the newest Shorts. YouTube Studio's Earn tab is the source of truth. Days to go
divide the gap by the pace of the last 7 days: (latest value - value at least 7 days
earlier) / days between them. Without 7 days of data, or with no progress, there is no estimate.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from clipper.insights import repo
from clipper.insights.tags import TAG_KEYS, UNKNOWN
from clipper.pipeline.common import strip_control

PACE_DAYS = 7
TREND_DAYS = 28
TABLE_DAYS = 28
MIN_TAG_SHORTS = 2
FINDINGS_MIN_AGE_DAYS = 3
"""Shorts younger than this are still gaining views: left out of the tag breakdown."""
VIEWS_LABEL = "Public Shorts views, last 90 days (estimate)"
ANALYTICS_ONLY = (
    "engaged views",
    "average % viewed",
    "subscribers gained",
    "shares",
    "traffic mix",
    "retention",
    "Shorts views from Analytics",
)
"""What the page shows only with the yt-analytics.readonly scope."""


@dataclass(frozen=True)
class Goal:
    key: str
    """A ChannelMetrics field."""
    label: str
    target: int
    estimate: bool = False


@dataclass(frozen=True)
class Tier:
    name: str
    goals: tuple[Goal, ...]


TIERS = (
    Tier(
        "Tier 1: fan funding",
        (
            Goal("subscribers", "Subscribers", 500),
            Goal("public_uploads_90d", "Public uploads, last 90 days", 3),
            Goal("shorts_views_90d", VIEWS_LABEL, 3_000_000, estimate=True),
        ),
    ),
    Tier(
        "Tier 2: ad revenue",
        (
            Goal("subscribers", "Subscribers", 1_000),
            Goal("shorts_views_90d", VIEWS_LABEL, 10_000_000, estimate=True),
        ),
    ),
)


@dataclass(frozen=True)
class GoalProgress:
    goal: Goal
    value: int | None
    as_of: date | None
    pace: float | None
    """Per day over the last 7 days; None without 7 days of data."""
    source: str | None = None
    """Where the Shorts views figure came from (`public` or `analytics`); None otherwise."""

    @property
    def reached(self) -> bool:
        return self.value is not None and self.value >= self.goal.target

    @property
    def percent(self) -> int | None:
        """Whole percent of the target, at most 100; None without data (not 0)."""
        if self.value is None:
            return None
        return min(100, self.value * 100 // self.goal.target)

    @property
    def days_to_go(self) -> int | None:
        if self.value is None:
            return None
        if self.reached:
            return 0
        if self.pace is None or self.pace <= 0:
            return None
        return math.ceil((self.goal.target - self.value) / self.pace)

    @property
    def outlook(self) -> str:
        if self.value is None:
            return "no data yet"
        if self.reached:
            return "reached"
        if self.pace is None:
            return "pace known after 7 days of data"
        if self.pace <= 0:
            return "no progress in the last 7 days"
        return f"about {self.days_to_go} days at the last 7 days' pace"


@dataclass(frozen=True)
class TierProgress:
    tier: Tier
    goals: tuple[GoalProgress, ...]

    @property
    def reached(self) -> bool:
        return all(g.reached for g in self.goals)

    @property
    def days_to_go(self) -> int | None:
        days = [g.days_to_go for g in self.goals]
        if any(d is None for d in days):
            return None
        return max(d for d in days if d is not None)

    @property
    def outlook(self) -> str:
        if self.reached:
            return "reached"
        if self.days_to_go is None:
            return "no estimate yet"
        return f"about {self.days_to_go} days at the last 7 days' pace"


def _shorts_views(
    public: Sequence[repo.ChannelMetrics], analytics: Sequence[repo.ChannelMetrics]
) -> dict[date, tuple[int, str]]:
    """Shorts views and their source on each day of `public` and `analytics` (rows in day
    order): the larger of the public figure on that day and the latest Analytics figure
    on or before it (a tie goes to Analytics). Days without either figure are left out."""
    on_day = {r.day: r.shorts_views_90d for r in public if r.shorts_views_90d is not None}
    latest = {r.day: r.shorts_views_90d for r in analytics if r.shorts_views_90d is not None}
    found: dict[date, tuple[int, str]] = {}
    carried: int | None = None
    for day in sorted(set(on_day) | set(latest)):
        carried = latest.get(day, carried)
        p = on_day.get(day)
        if carried is not None and (p is None or carried >= p):
            found[day] = (carried, repo.ANALYTICS)
        elif p is not None:
            found[day] = (p, repo.PUBLIC)
    return found


def _history_since(today: date) -> date:
    """The first day the tracker reads: the trend's days plus the pace window before them.
    The trend reads Analytics from the same day, so a carried Analytics figure is the same
    in both."""
    return today - timedelta(days=TREND_DAYS + PACE_DAYS)


def _series(
    conn: sqlite3.Connection, account_id: str, key: str, today: date
) -> list[tuple[date, int, str | None]]:
    """The goal's (day, value, Shorts views source) in day order; the source is None for
    the other goals."""
    since = _history_since(today)
    public = repo.channel_history(conn, account_id, repo.PUBLIC, since)
    if key == "shorts_views_90d":
        analytics = repo.channel_history(conn, account_id, repo.ANALYTICS, since)
        views = _shorts_views(public, analytics)
        return [(day, value, source) for day, (value, source) in sorted(views.items())]
    return [(r.day, value, None) for r in public if (value := getattr(r, key)) is not None]


def goal_progress(
    conn: sqlite3.Connection, account_id: str, goal: Goal, today: date
) -> GoalProgress:
    series = _series(conn, account_id, goal.key, today)
    if not series:
        return GoalProgress(goal, None, None, None)
    day, value, source = series[-1]
    earlier = [(d, v) for d, v, _ in series if d <= day - timedelta(days=PACE_DAYS)]
    pace = None
    if earlier:
        past_day, past_value = earlier[-1]
        pace = (value - past_value) / (day - past_day).days
    return GoalProgress(goal, value, day, pace, source)


def tracker(conn: sqlite3.Connection, account_id: str, today: date) -> list[TierProgress]:
    return [
        TierProgress(tier, tuple(goal_progress(conn, account_id, g, today) for g in tier.goals))
        for tier in TIERS
    ]


@dataclass(frozen=True)
class TrendDay:
    day: date
    subscribers: int | None
    total_views: int | None
    shorts_views_90d: int | None
    shorts_views_source: str | None


def trend(
    conn: sqlite3.Connection, account_id: str, today: date, days: int = TREND_DAYS
) -> list[TrendDay]:
    since = today - timedelta(days=days - 1)
    public_rows = repo.channel_history(conn, account_id, repo.PUBLIC, since)
    analytics_rows = repo.channel_history(
        conn, account_id, repo.ANALYTICS, min(since, _history_since(today))
    )
    public = {r.day: r for r in public_rows}
    views = _shorts_views(public_rows, analytics_rows)
    found: list[TrendDay] = []
    for day in sorted(set(public) | {r.day for r in analytics_rows if r.day >= since}):
        p = public.get(day)
        value, source = views.get(day, (None, None))
        found.append(
            TrendDay(day, p.subscribers if p else None, p.total_views if p else None, value, source)
        )
    return found


@dataclass(frozen=True)
class ShortRow:
    video_id: str
    post_id: int | None
    """The pipeline post; None for a hand upload."""
    title: str
    url: str | None
    published: datetime
    views: int | None
    engaged_views: int | None
    avg_view_pct: float | None
    subs_gained: int | None
    shares: int | None
    traffic: dict[str, int]
    comments_enabled: bool | None
    through_date: date | None
    tags: dict[str, str]
    ratio: float | None = None

    @property
    def origin(self) -> str:
        return "pipeline" if self.post_id is not None else "hand upload"

    @property
    def traffic_mix(self) -> list[tuple[str, int]]:
        """Percent of views by traffic source, largest first, at most 4."""
        total = sum(self.traffic.values())
        if not total:
            return []
        ranked = sorted(self.traffic.items(), key=lambda item: (-item[1], item[0]))[:4]
        return [(source, round(views * 100 / total)) for source, views in ranked]


def median(values: Iterable[float]) -> float | None:
    found = list(values)
    return float(statistics.median(found)) if found else None


def shorts_table(
    conn: sqlite3.Connection, account_id: str, now: datetime, days: int = TABLE_DAYS
) -> list[ShortRow]:
    """Every known Short of the channel (pipeline posts and hand uploads, 180 s or less)
    that went public in the last `days`, newest first, with its ratio to the median views
    of those Shorts."""
    videos = repo.channel_videos(
        conn, account_id, since=now - timedelta(days=days), until=now, shorts_only=True
    )
    tags = repo.tags_for_videos(conn, [v.video_id for v in videos])
    rows: list[ShortRow] = []
    for video in videos:
        public = repo.latest_video_metrics(conn, video.video_id, repo.PUBLIC)
        analytics = repo.latest_video_metrics(conn, video.video_id, repo.ANALYTICS)
        pm = public.metrics if public else None
        am = analytics.metrics if analytics else None
        views = pm.views if pm and pm.views is not None else (am.views if am else None)
        rows.append(
            ShortRow(
                video_id=video.video_id,
                post_id=video.post_id,
                title=strip_control(video.title or "")[:100] or video.video_id,
                url=video.url,
                published=video.published_at,
                views=views,
                engaged_views=am.engaged_views if am else None,
                avg_view_pct=am.avg_view_pct if am else None,
                subs_gained=am.subs_gained if am else None,
                shares=am.shares if am else None,
                traffic=dict(am.traffic or {}) if am else {},
                comments_enabled=pm.comments_enabled if pm else None,
                through_date=analytics.through_date if analytics else None,
                tags=tags.get(video.video_id, {}),
            )
        )
    return sorted(_with_ratios(rows), key=lambda r: (r.published, r.video_id), reverse=True)


def _with_ratios(rows: Sequence[ShortRow]) -> list[ShortRow]:
    """The rows with each one's views over the median views of `rows`; None without
    views or when the median is 0 or unknown."""
    mid = median(r.views for r in rows if r.views is not None)
    return [replace(r, ratio=r.views / mid if mid and r.views is not None else None) for r in rows]


@dataclass(frozen=True)
class TagGroup:
    key: str
    value: str
    shorts: int
    median_ratio: float | None


def tag_breakdown(rows: Sequence[ShortRow], today: date) -> dict[str, list[TagGroup]]:
    """Per tag key, each value's Short count and median ratio to the channel median, best
    first. Only settled Shorts count: those published FINDINGS_MIN_AGE_DAYS or more days
    before `today`. Their ratios are taken against the median of the settled Shorts, so
    fresh Shorts with few views yet neither join a group nor pull the median down."""
    cutoff = today - timedelta(days=FINDINGS_MIN_AGE_DAYS)
    rows = _with_ratios([r for r in rows if r.published.date() <= cutoff])
    found: dict[str, list[TagGroup]] = {}
    for key in TAG_KEYS:
        counts: Counter[str] = Counter()
        ratios: dict[str, list[float]] = {}
        for row in rows:
            value = row.tags.get(key)
            if value is None:
                continue
            counts[value] += 1
            if row.ratio is not None:
                ratios.setdefault(value, []).append(row.ratio)
        if counts:
            groups = [TagGroup(key, v, counts[v], median(ratios.get(v, []))) for v in counts]
            found[key] = sorted(
                groups, key=lambda g: (g.median_ratio is None, -(g.median_ratio or 0.0), g.value)
            )
    return found


def findings(breakdown: dict[str, list[TagGroup]]) -> tuple[list[TagGroup], list[TagGroup]]:
    """What to do more of and less of: per key with at least two known values of
    MIN_TAG_SHORTS or more Shorts each, the best value when above the median and the
    worst when below. `unknown` is never a finding."""
    more: list[TagGroup] = []
    less: list[TagGroup] = []
    for groups in breakdown.values():
        known = [
            g
            for g in groups
            if g.median_ratio is not None and g.shorts >= MIN_TAG_SHORTS and g.value != UNKNOWN
        ]
        if len(known) < 2:
            continue
        best, worst = known[0], known[-1]
        if best.median_ratio is not None and best.median_ratio > 1:
            more.append(best)
        if worst.median_ratio is not None and worst.median_ratio < 1:
            less.append(worst)
    more.sort(key=lambda g: -(g.median_ratio or 0.0))
    less.sort(key=lambda g: g.median_ratio or 0.0)
    return more, less
