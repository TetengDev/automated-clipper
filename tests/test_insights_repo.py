"""Migration 0012 and the insights repo (CLP-154): every Short is keyed by its YouTube
video id; a pipeline post is linked when there is one."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from clipper.db import bundled_migrations
from clipper.insights import repo
from insights_support import ACCOUNT, HAND_UPLOADS, T0, open_db, short, upload


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(tmp_path / "state.db")
    yield c
    c.close()


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """Column name to its NOT NULL flag."""
    return {r["name"]: r["notnull"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_0012_is_bundled_and_makes_the_tables(conn: sqlite3.Connection) -> None:
    assert (12, "insights") in [(m.version, m.name) for m in bundled_migrations()]
    metrics = _columns(conn, "post_metrics")
    assert {
        "video_id",
        "account_id",
        "post_id",
        "captured_at",
        "views",
        "watch_time_s",
        "likes",
        "shares",
        "comments",
        "engaged_views",
        "avg_view_pct",
        "subs_gained",
        "traffic",
        "retention",
        "source",
        "through_date",
        "avg_view_s",
        "subs_lost",
        "comments_enabled",
    } <= set(metrics)
    assert metrics["video_id"] == 1 and metrics["post_id"] == 0
    assert {
        "account_id",
        "date",
        "subscribers",
        "total_views",
        "shorts_views_90d",
        "public_uploads_90d",
        "source",
        "captured_at",
    } <= set(_columns(conn, "channel_metrics"))
    tags = _columns(conn, "post_tags")
    assert {"video_id", "post_id", "key", "value"} <= set(tags)
    assert tags["video_id"] == 0 and tags["post_id"] == 0
    assert {"video_id", "account_id", "post_id", "title", "published_at", "duration_s"} <= set(
        _columns(conn, "channel_videos")
    )
    assert "format" in _columns(conn, "commentary_stories")


@pytest.mark.parametrize(
    ("seconds", "expected"), [(None, None), (30.0, True), (180.0, True), (180.5, False)]
)
def test_a_short_is_180_seconds_or_less(seconds: float | None, expected: bool | None) -> None:
    assert repo.is_short(seconds) is expected


def test_hand_uploads_are_videos_without_a_post(conn: sqlite3.Connection) -> None:
    for i, vid in enumerate(HAND_UPLOADS):
        upload(conn, vid, published_at=T0 - timedelta(days=i))
    upload(conn, "longVideo01", duration_s=600.0)

    shorts = repo.channel_videos(conn, ACCOUNT, shorts_only=True)

    assert [v.video_id for v in shorts] == list(reversed(HAND_UPLOADS))
    assert all(v.post_id is None and v.short for v in shorts)
    assert shorts[-1].url == "https://www.youtube.com/shorts/2BpbZneO_To"
    assert len(repo.channel_videos(conn, ACCOUNT)) == 4
    window = repo.channel_videos(conn, ACCOUNT, since=T0 - timedelta(hours=12), until=T0)
    assert [v.video_id for v in window] == ["2BpbZneO_To", "longVideo01"]


def test_a_pipeline_post_links_to_its_video_and_the_data_api_wins(
    conn: sqlite3.Connection,
) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.register_post_video(
        conn, repo.Video(vid, ACCOUNT, T0, "Pipeline title", 42.5, post.id), T0
    )
    upload(conn, vid, title="Title on YouTube", duration_s=43.0, published_at=T0)
    repo.register_post_video(
        conn, repo.Video(vid, ACCOUNT, T0 - timedelta(days=1), "Old", 1.0, post.id), T0
    )

    video = repo.get_video(conn, vid)

    assert video == repo.Video(vid, ACCOUNT, T0, "Title on YouTube", 43.0, post.id)


def test_metrics_round_trip_by_video_with_json_fields(conn: sqlite3.Connection) -> None:
    post = short(conn)
    video = repo.Video(post.external_id or "", ACCOUNT, T0, post_id=post.id)
    metrics = repo.PostMetrics(
        views=4000,
        engaged_views=3100,
        avg_view_pct=55.5,
        comments_enabled=True,
        traffic={"SHORTS": 900, "YT_SEARCH": 100},
        retention=[(0.0, 1.0), (1.0, 0.4)],
    )
    repo.add_video_metrics(conn, video, repo.ANALYTICS, metrics, T0, through_date=date(2027, 1, 13))

    snap = repo.latest_video_metrics(conn, video.video_id, repo.ANALYTICS)

    assert snap is not None
    assert (snap.video_id, snap.post_id) == (video.video_id, post.id)
    assert snap.metrics == metrics
    assert snap.through_date == date(2027, 1, 13)
    assert snap.captured_at == T0
    assert repo.latest_video_metrics(conn, video.video_id, repo.PUBLIC) is None


def test_a_hand_upload_has_metrics_without_a_post(conn: sqlite3.Connection) -> None:
    video = upload(conn, HAND_UPLOADS[0])
    repo.add_video_metrics(conn, video, repo.PUBLIC, repo.PostMetrics(views=1), T0)
    later = T0 + timedelta(hours=1)
    repo.add_video_metrics(conn, video, repo.PUBLIC, repo.PostMetrics(views=2), later)
    repo.add_video_metrics(conn, video, repo.PUBLIC, repo.PostMetrics(views=99), later)

    assert repo.last_capture(conn, video.video_id, repo.PUBLIC) == later
    snap = repo.latest_video_metrics(conn, video.video_id, repo.PUBLIC)
    assert snap is not None and snap.post_id is None and snap.metrics.views == 2


def test_an_unknown_source_is_refused(conn: sqlite3.Connection) -> None:
    video = upload(conn, HAND_UPLOADS[0])
    with pytest.raises(ValueError, match="unknown metrics source"):
        repo.add_video_metrics(conn, video, "studio", repo.PostMetrics(), T0)


def test_channel_metrics_one_row_per_day_and_source(conn: sqlite3.Connection) -> None:
    day = date(2027, 1, 15)
    repo.upsert_channel_metrics(
        conn, repo.ChannelMetrics(ACCOUNT, day, repo.PUBLIC, subscribers=300), T0
    )
    repo.upsert_channel_metrics(
        conn,
        repo.ChannelMetrics(ACCOUNT, day, repo.PUBLIC, subscribers=310),
        T0 + timedelta(hours=1),
    )
    repo.upsert_channel_metrics(
        conn, repo.ChannelMetrics(ACCOUNT, day - timedelta(days=1), repo.PUBLIC, subscribers=1), T0
    )

    history = repo.channel_history(conn, ACCOUNT, repo.PUBLIC, date(2027, 1, 1))

    assert [(r.day, r.subscribers) for r in history] == [
        (date(2027, 1, 14), 1),
        (date(2027, 1, 15), 310),
    ]
    assert repo.channel_history(conn, ACCOUNT, repo.ANALYTICS, date(2027, 1, 1)) == []


def test_post_tags_follow_the_post_to_its_video(conn: sqlite3.Connection) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.set_tags(conn, {"length_bucket": "40-60s", "topic": "unknown"}, post_id=post.id)
    repo.set_tags(conn, {"topic": "gta vi"}, post_id=post.id)
    assert repo.tags_for_videos(conn, [vid]) == {}

    repo.link_post_tags(conn, post.id, vid)

    assert repo.tags_for_videos(conn, [vid]) == {
        vid: {"length_bucket": "40-60s", "topic": "gta vi"}
    }
    assert repo.tags_for_post(conn, post.id) == {"length_bucket": "40-60s", "topic": "gta vi"}
    assert repo.tags_for_videos(conn, []) == {}


def test_video_tags_replace_or_only_fill_in(conn: sqlite3.Connection) -> None:
    vid = HAND_UPLOADS[1]
    repo.set_tags(conn, {"hook_type": "question"}, video_id=vid)
    repo.set_tags(conn, {"hook_type": "unknown", "palette": "unknown"}, video_id=vid, replace=False)

    assert repo.tags_for_videos(conn, [vid]) == {
        vid: {"hook_type": "question", "palette": "unknown"}
    }
    with pytest.raises(ValueError, match="video id or a post id"):
        repo.set_tags(conn, {"topic": "x"})


def test_a_fill_in_replaces_unknown_but_never_a_real_value(conn: sqlite3.Connection) -> None:
    vid = HAND_UPLOADS[1]
    repo.set_tags(conn, {"hook_type": "unknown", "palette": "neon"}, video_id=vid)

    repo.set_tags(conn, {"hook_type": "question", "palette": "pastel"}, video_id=vid, replace=False)

    assert repo.tags_for_videos(conn, [vid]) == {vid: {"hook_type": "question", "palette": "neon"}}


def _tag_rows(conn: sqlite3.Connection) -> list[tuple[str | None, int | None, str, str]]:
    return [
        tuple(r)
        for r in conn.execute("SELECT video_id, post_id, key, value FROM post_tags ORDER BY id")
    ]


def test_linking_merges_an_approval_tag_with_a_default_video_tag(
    conn: sqlite3.Connection,
) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.set_tags(conn, {"hook_type": "question"}, post_id=post.id)
    repo.set_tags(conn, {"hook_type": "unknown"}, video_id=vid, replace=False)

    repo.link_post_tags(conn, post.id, vid)

    assert repo.tags_for_videos(conn, [vid]) == {vid: {"hook_type": "question"}}
    assert repo.tags_for_post(conn, post.id) == {"hook_type": "question"}
    assert _tag_rows(conn) == [(vid, post.id, "hook_type", "question")]


def test_linking_keeps_the_owners_video_tag_over_the_approval_tag(
    conn: sqlite3.Connection,
) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.set_tags(conn, {"hook_type": "reaction"}, video_id=vid)
    repo.set_tags(conn, {"hook_type": "question"}, post_id=post.id)

    repo.link_post_tags(conn, post.id, vid)

    assert repo.tags_for_videos(conn, [vid]) == {vid: {"hook_type": "reaction"}}
    assert repo.tags_for_post(conn, post.id) == {"hook_type": "reaction"}
    assert _tag_rows(conn) == [(vid, post.id, "hook_type", "reaction")]


def test_tags_with_both_ids_replace_both_rows_with_one(conn: sqlite3.Connection) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.set_tags(conn, {"hook_type": "question"}, post_id=post.id)
    repo.set_tags(conn, {"hook_type": "unknown"}, video_id=vid, replace=False)

    repo.set_tags(conn, {"hook_type": "reaction"}, video_id=vid, post_id=post.id)

    assert repo.tags_for_videos(conn, [vid]) == {vid: {"hook_type": "reaction"}}
    assert repo.tags_for_post(conn, post.id) == {"hook_type": "reaction"}
    assert _tag_rows(conn) == [(vid, post.id, "hook_type", "reaction")]


def test_tags_with_both_ids_fill_in_keeps_the_real_value(conn: sqlite3.Connection) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.set_tags(conn, {"hook_type": "question"}, post_id=post.id)
    repo.set_tags(conn, {"hook_type": "unknown"}, video_id=vid, replace=False)

    repo.set_tags(conn, {"hook_type": "unknown"}, video_id=vid, post_id=post.id, replace=False)

    assert _tag_rows(conn) == [(vid, post.id, "hook_type", "question")]


def test_registering_without_a_post_keeps_the_link(conn: sqlite3.Connection) -> None:
    post = short(conn)
    vid = post.external_id or ""
    repo.register_post_video(conn, repo.Video(vid, ACCOUNT, T0, "Title", 42.5, post.id), T0)

    repo.register_post_video(conn, repo.Video(vid, ACCOUNT, T0), T0)

    video = repo.get_video(conn, vid)
    assert video is not None and video.post_id == post.id


def test_analytics_state_is_one_row_per_account(conn: sqlite3.Connection) -> None:
    repo.set_analytics_state(conn, ACCOUNT, repo.AnalyticsState.NEEDS_REAUTH, "re-authorise", T0)
    repo.set_analytics_state(conn, ACCOUNT, repo.AnalyticsState.OK, None, T0 + timedelta(days=1))

    state = repo.analytics_state(conn, ACCOUNT)

    assert state == repo.StateRow(repo.AnalyticsState.OK, None, T0 + timedelta(days=1))
    assert repo.analytics_state(conn, "other_yt") is None


def test_youtube_posts_are_the_posted_ones_with_a_video_id(conn: sqlite3.Connection) -> None:
    posted = short(conn, scheduled_at=T0)
    short(conn, posted=False)
    short(conn, account_id="other_yt")
    bad = short(conn, video_id="not-a-valid-video-id")

    found = repo.youtube_posts(conn, ACCOUNT)

    assert [p.id for p in found] == [posted.id]
    assert bad.id not in [p.id for p in found]
    assert repo.went_public(posted) == T0  # posted_at is an hour before publishAt
