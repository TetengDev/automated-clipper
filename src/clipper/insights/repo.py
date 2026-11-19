"""Storage for channel insights (CLP-154): the videos of each channel, metric snapshots
of videos and channels, format tags, and each account's Analytics state (migration 0012).

Everything about a Short is keyed by its YouTube video id, so hand-uploaded Shorts are
tracked like pipeline posts; `post_id` links the pipeline ones. Every write runs in its
own `transaction`, so a caller never holds the write lock across an HTTP call. Sources
and states are checked here, not by CHECK constraints.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from clipper.db import transaction
from clipper.jobs.models import from_iso, to_iso
from clipper.posting.models import Post, PostStatus

PUBLIC = "public"
ANALYTICS = "analytics"
SOURCES = frozenset({PUBLIC, ANALYTICS})
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
"""A YouTube video id, as `posts.external_id` holds it for a YouTube post."""
SHORT_MAX_S = 180.0
"""A video of this many seconds or fewer is a Short (YouTube's limit since 2024-10)."""
SHORTS_URL = "https://www.youtube.com/shorts/"


def is_short(duration_s: float | None) -> bool | None:
    if duration_s is None:
        return None
    return duration_s <= SHORT_MAX_S


@dataclass(frozen=True)
class Video:
    video_id: str
    account_id: str
    published_at: datetime
    title: str | None = None
    duration_s: float | None = None
    post_id: int | None = None
    """The pipeline post this video is; None for a hand upload."""

    @property
    def short(self) -> bool | None:
        return is_short(self.duration_s)

    @property
    def url(self) -> str:
        return SHORTS_URL + self.video_id


class AnalyticsState(StrEnum):
    OK = "ok"
    NEEDS_REAUTH = "needs_reauth"
    """The refresh token lacks yt-analytics.readonly: re-authorise the account."""
    NO_TOKEN = "no_token"  # noqa: S105 - a state label, not a secret
    ERROR = "error"


@dataclass(frozen=True)
class PostMetrics:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    watch_time_s: float | None = None
    engaged_views: int | None = None
    avg_view_s: float | None = None
    avg_view_pct: float | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None
    comments_enabled: bool | None = None
    traffic: dict[str, int] | None = None
    retention: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class Snapshot:
    video_id: str
    post_id: int | None
    source: str
    captured_at: datetime
    through_date: date | None
    metrics: PostMetrics


@dataclass(frozen=True)
class ChannelMetrics:
    account_id: str
    day: date
    source: str
    subscribers: int | None = None
    total_views: int | None = None
    shorts_views_90d: int | None = None
    public_uploads_90d: int | None = None


@dataclass(frozen=True)
class StateRow:
    state: AnalyticsState
    detail: str | None
    checked_at: datetime


_SCALARS = (
    "views",
    "likes",
    "comments",
    "shares",
    "watch_time_s",
    "engaged_views",
    "avg_view_s",
    "avg_view_pct",
    "subs_gained",
    "subs_lost",
)


def _source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"unknown metrics source {source!r}")
    return source


def _json(raw: str | None, kind: type) -> Any:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, kind) else None


def _video(row: sqlite3.Row) -> Video:
    published = from_iso(row["published_at"])
    assert published is not None  # NOT NULL column
    return Video(
        row["video_id"],
        row["account_id"],
        published,
        row["title"],
        row["duration_s"],
        row["post_id"],
    )


def upsert_public_video(conn: sqlite3.Connection, video: Video, now: datetime) -> None:
    """Store a video as the Data API reports it. Its title, publish time and duration
    replace earlier values; a linked post stays linked."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO channel_videos (video_id, account_id, post_id, title, published_at,"
            " duration_s, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (video_id) DO UPDATE SET"
            " title = COALESCE(excluded.title, title),"
            " published_at = excluded.published_at,"
            " duration_s = COALESCE(excluded.duration_s, duration_s),"
            " post_id = COALESCE(post_id, excluded.post_id),"
            " updated_at = excluded.updated_at",
            (
                video.video_id,
                video.account_id,
                video.post_id,
                video.title,
                to_iso(video.published_at),
                video.duration_s,
                to_iso(now),
                to_iso(now),
            ),
        )


def register_post_video(conn: sqlite3.Connection, video: Video, now: datetime) -> None:
    """Store a pipeline post's video: link the post, and fill in title and duration only
    where the Data API has not given them. Never moves the publish time of a known video."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO channel_videos (video_id, account_id, post_id, title, published_at,"
            " duration_s, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (video_id) DO UPDATE SET"
            " post_id = excluded.post_id,"
            " title = COALESCE(title, excluded.title),"
            " duration_s = COALESCE(duration_s, excluded.duration_s),"
            " updated_at = excluded.updated_at",
            (
                video.video_id,
                video.account_id,
                video.post_id,
                video.title,
                to_iso(video.published_at),
                video.duration_s,
                to_iso(now),
                to_iso(now),
            ),
        )


def get_video(conn: sqlite3.Connection, video_id: str) -> Video | None:
    row = conn.execute("SELECT * FROM channel_videos WHERE video_id = ?", (video_id,)).fetchone()
    return _video(row) if row else None


def channel_videos(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    shorts_only: bool = False,
) -> list[Video]:
    """The account's known videos published in [since, until], oldest first. With
    `shorts_only`, only videos known to be 180 s or shorter."""
    sql = "SELECT * FROM channel_videos WHERE account_id = ?"
    args: list[Any] = [account_id]
    if since is not None:
        sql += " AND published_at >= ?"
        args.append(to_iso(since))
    if until is not None:
        sql += " AND published_at <= ?"
        args.append(to_iso(until))
    if shorts_only:
        sql += " AND duration_s IS NOT NULL AND duration_s <= ?"
        args.append(SHORT_MAX_S)
    return [_video(r) for r in conn.execute(sql + " ORDER BY published_at, video_id", args)]


def add_video_metrics(
    conn: sqlite3.Connection,
    video: Video,
    source: str,
    metrics: PostMetrics,
    captured_at: datetime,
    *,
    through_date: date | None = None,
) -> None:
    """One snapshot of a video's numbers. A second snapshot of the same video, source and
    capture time is ignored."""
    enabled = None if metrics.comments_enabled is None else int(metrics.comments_enabled)
    traffic = None if metrics.traffic is None else json.dumps(metrics.traffic, sort_keys=True)
    retention = (
        None if metrics.retention is None else json.dumps([list(p) for p in metrics.retention])
    )
    columns = ", ".join(_SCALARS)
    marks = ", ".join("?" * (len(_SCALARS) + 9))
    with transaction(conn):
        conn.execute(
            "INSERT INTO post_metrics (video_id, account_id, post_id, source, captured_at,"  # noqa: S608 - fixed column names
            f" through_date, {columns}, comments_enabled, traffic, retention) VALUES ({marks})"
            " ON CONFLICT (video_id, source, captured_at) DO NOTHING",
            (
                video.video_id,
                video.account_id,
                video.post_id,
                _source(source),
                to_iso(captured_at),
                through_date.isoformat() if through_date else None,
                *(getattr(metrics, name) for name in _SCALARS),
                enabled,
                traffic,
                retention,
            ),
        )


def _snapshot(row: sqlite3.Row) -> Snapshot:
    traffic = _json(row["traffic"], dict)
    retention = _json(row["retention"], list)
    metrics = PostMetrics(
        **{name: row[name] for name in _SCALARS},
        comments_enabled=None if row["comments_enabled"] is None else bool(row["comments_enabled"]),
        traffic=(
            {str(k): int(v) for k, v in traffic.items() if isinstance(v, int)}
            if traffic is not None
            else None
        ),
        retention=(
            [(float(p[0]), float(p[1])) for p in retention if isinstance(p, list) and len(p) == 2]
            if retention is not None
            else None
        ),
    )
    captured = from_iso(row["captured_at"])
    assert captured is not None  # NOT NULL column
    through = date.fromisoformat(row["through_date"]) if row["through_date"] else None
    return Snapshot(row["video_id"], row["post_id"], row["source"], captured, through, metrics)


def latest_video_metrics(conn: sqlite3.Connection, video_id: str, source: str) -> Snapshot | None:
    row = conn.execute(
        "SELECT * FROM post_metrics WHERE video_id = ? AND source = ?"
        " ORDER BY captured_at DESC, id DESC LIMIT 1",
        (video_id, _source(source)),
    ).fetchone()
    return _snapshot(row) if row else None


def last_capture(conn: sqlite3.Connection, video_id: str, source: str) -> datetime | None:
    snap = latest_video_metrics(conn, video_id, source)
    return snap.captured_at if snap else None


def upsert_channel_metrics(
    conn: sqlite3.Connection, row: ChannelMetrics, captured_at: datetime
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO channel_metrics (account_id, date, source, subscribers, total_views,"
            " shorts_views_90d, public_uploads_90d, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (account_id, date, source) DO UPDATE SET"
            " subscribers = excluded.subscribers, total_views = excluded.total_views,"
            " shorts_views_90d = excluded.shorts_views_90d,"
            " public_uploads_90d = excluded.public_uploads_90d,"
            " captured_at = excluded.captured_at",
            (
                row.account_id,
                row.day.isoformat(),
                _source(row.source),
                row.subscribers,
                row.total_views,
                row.shorts_views_90d,
                row.public_uploads_90d,
                to_iso(captured_at),
            ),
        )


def channel_history(
    conn: sqlite3.Connection, account_id: str, source: str, since: date
) -> list[ChannelMetrics]:
    rows = conn.execute(
        "SELECT * FROM channel_metrics WHERE account_id = ? AND source = ? AND date >= ?"
        " ORDER BY date",
        (account_id, _source(source), since.isoformat()),
    )
    return [
        ChannelMetrics(
            account_id=r["account_id"],
            day=date.fromisoformat(r["date"]),
            source=r["source"],
            subscribers=r["subscribers"],
            total_views=r["total_views"],
            shorts_views_90d=r["shorts_views_90d"],
            public_uploads_90d=r["public_uploads_90d"],
        )
        for r in rows
    ]


def set_tags(
    conn: sqlite3.Connection,
    tags: Mapping[str, str],
    *,
    video_id: str | None = None,
    post_id: int | None = None,
    replace: bool = True,
) -> None:
    """Store format tags of a video, of a post, or of both. A key already present keeps
    its value unless `replace`; either way the row gains the id it was missing."""
    if video_id is None and post_id is None:
        raise ValueError("tags need a video id or a post id")
    with transaction(conn):
        for key, value in sorted(tags.items()):
            found = conn.execute(
                "SELECT id FROM post_tags WHERE key = ?"
                " AND ((? IS NOT NULL AND video_id = ?) OR (? IS NOT NULL AND post_id = ?))"
                " ORDER BY id LIMIT 1",
                (key, video_id, video_id, post_id, post_id),
            ).fetchone()
            if found is None:
                conn.execute(
                    "INSERT INTO post_tags (video_id, post_id, key, value) VALUES (?, ?, ?, ?)",
                    (video_id, post_id, key, value),
                )
                continue
            conn.execute(
                "UPDATE OR IGNORE post_tags SET value = CASE WHEN ? THEN ? ELSE value END,"
                " video_id = COALESCE(video_id, ?), post_id = COALESCE(post_id, ?) WHERE id = ?",
                (int(replace), value, video_id, post_id, found["id"]),
            )


def link_post_tags(conn: sqlite3.Connection, post_id: int, video_id: str) -> None:
    """Give a post's tags its video id, once the post is uploaded."""
    with transaction(conn):
        conn.execute(
            "UPDATE OR IGNORE post_tags SET video_id = ? WHERE post_id = ? AND video_id IS NULL",
            (video_id, post_id),
        )


def tags_for_videos(
    conn: sqlite3.Connection, video_ids: Iterable[str]
) -> dict[str, dict[str, str]]:
    ids = sorted(set(video_ids))
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    found: dict[str, dict[str, str]] = {}
    rows = conn.execute(
        f"SELECT video_id, key, value FROM post_tags WHERE video_id IN ({marks})",  # noqa: S608 - placeholders
        ids,
    )
    for video_id, key, value in rows:
        found.setdefault(video_id, {})[key] = value
    return found


def tags_for_post(conn: sqlite3.Connection, post_id: int) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM post_tags WHERE post_id = ?", (post_id,))
    return {key: value for key, value in rows}


def set_analytics_state(
    conn: sqlite3.Connection,
    account_id: str,
    state: AnalyticsState,
    detail: str | None,
    now: datetime,
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO insights_state (account_id, analytics, detail, checked_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (account_id) DO UPDATE SET"
            " analytics = excluded.analytics, detail = excluded.detail,"
            " checked_at = excluded.checked_at",
            (account_id, str(state), detail, to_iso(now)),
        )


def analytics_state(conn: sqlite3.Connection, account_id: str) -> StateRow | None:
    row = conn.execute(
        "SELECT analytics, detail, checked_at FROM insights_state WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    checked = from_iso(row["checked_at"])
    assert checked is not None  # NOT NULL column
    return StateRow(AnalyticsState(row["analytics"]), row["detail"], checked)


def youtube_posts(conn: sqlite3.Connection, account_id: str) -> list[Post]:
    """The account's posted YouTube posts with a valid video id, oldest first."""
    rows = conn.execute(
        "SELECT * FROM posts WHERE account_id = ? AND platform = 'youtube' AND status = ?"
        " AND external_id IS NOT NULL ORDER BY scheduled_at, id",
        (account_id, str(PostStatus.POSTED)),
    )
    return [p for p in map(Post.from_row, rows) if VIDEO_ID.fullmatch(p.external_id or "")]


def went_public(post: Post) -> datetime:
    """When the Short became public: the YouTube publisher uploads it private with
    `publishAt` = `scheduled_at`, so it is public from the later of that and the upload."""
    return max(post.scheduled_at, post.posted_at or post.scheduled_at)
