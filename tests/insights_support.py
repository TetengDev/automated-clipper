"""Shared helpers for the insights tests (CLP-154): a migrated DB with commentary Shorts
posted to YouTube, hand-uploaded Shorts, and the stories behind pipeline Shorts."""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clipper.db import connect, migrate
from clipper.insights import repo
from clipper.jobs.models import to_iso
from clipper.posting import repo as posting_repo
from clipper.posting.models import Post, PostMetadata

T0 = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
ACCOUNT = "random_gaming_yt"
CHANNEL = "UCcMYw3Ae2crqXE2yzFBSUig"
TIMEZONE = "America/New_York"
HAND_UPLOADS = ("2BpbZneO_To", "piveRCCehQ8", "1VNCV6On2-k")
"""Shorts the owner uploaded by hand to UCcMYw3Ae2crqXE2yzFBSUig before the pipeline."""
_SEQ = itertools.count(1)


def open_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def short(
    conn: sqlite3.Connection,
    *,
    account_id: str = ACCOUNT,
    title: str = "Streamer loses it",
    duration_s: float = 42.5,
    scheduled_at: datetime = T0,
    posted: bool = True,
    video_id: str | None = None,
) -> Post:
    """An approved commentary Short (job, clip, version) and its YouTube post. `posted`
    marks the post posted with `video_id` (default: a fresh 11-character id)."""
    n = next(_SEQ)
    stamp = to_iso(T0 - timedelta(days=30))
    job = conn.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, status, created_at,"
        " updated_at) VALUES ('commentary', ?, ?, '', 'clips_ready', ?, ?)",
        (f"{account_id}:test-{n}", account_id, stamp, stamp),
    ).lastrowid
    clip = conn.execute(
        "INSERT INTO clips (job_id, rank, status, created_at, updated_at)"
        " VALUES (?, 1, 'approved', ?, ?)",
        (job, stamp, stamp),
    ).lastrowid
    conn.execute(
        "INSERT INTO clip_versions (clip_id, version, start_s, end_s, title, duration_s,"
        " created_at) VALUES (?, 1, 0, ?, ?, ?, ?)",
        (clip, duration_s, title, duration_s, stamp),
    )
    post = posting_repo.insert_post(
        conn,
        clip_id=int(clip or 0),
        clip_version=1,
        account_id=account_id,
        platform="youtube",
        scheduled_at=scheduled_at,
        video_path=f"{account_id}/clip_{clip}_v1.mp4",
        now=T0 - timedelta(days=30),
        metadata=PostMetadata(title=title, caption=""),
    )
    if posted:
        vid = video_id or f"vid{n:08d}"
        conn.execute(
            "UPDATE posts SET status = 'posted', external_id = ?, url = ?, posted_at = ?"
            " WHERE id = ?",
            (
                vid,
                f"https://www.youtube.com/shorts/{vid}",
                to_iso(scheduled_at - timedelta(hours=1)),
                post.id,
            ),
        )
    found = posting_repo.get_post(conn, post.id)
    assert found is not None
    return found


def upload(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    account_id: str = ACCOUNT,
    title: str = "Hand upload",
    published_at: datetime = T0,
    duration_s: float | None = 30.0,
    post_id: int | None = None,
) -> repo.Video:
    """A video as the Data API reports it (a hand upload unless `post_id` is given)."""
    video = repo.Video(video_id, account_id, published_at, title, duration_s, post_id)
    repo.upsert_public_video(conn, video, T0)
    return video


def job_of(conn: sqlite3.Connection, clip_id: int) -> int:
    return int(conn.execute("SELECT job_id FROM clips WHERE id = ?", (clip_id,)).fetchone()[0])


def story(
    conn: sqlite3.Connection,
    post: Post,
    *,
    excerpts: list[tuple[str, str, float, float]],
    fmt: dict[str, str] | None = None,
) -> None:
    """The commentary story of the post's clip version. Each excerpt is (Twitch clip id,
    streamer login, start_s, end_s) and gets a story candidate row."""
    stamp = to_iso(T0)
    job = job_of(conn, post.clip_id)
    for i, (twitch_clip_id, login, _start, _end) in enumerate(excerpts):
        conn.execute(
            "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id, broadcaster_id,"
            " broadcaster_login, url, view_count, duration_s, clip_created_at, score,"
            " used_by_clip_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1000, 30.0, ?, 10.0, ?, ?, ?)",
            (
                job,
                post.account_id,
                twitch_clip_id,
                f"b{i}",
                login,
                "https://clips.twitch.tv/x",
                stamp,
                post.clip_id,
                stamp,
                stamp,
            ),
        )
    plan = [
        {"clip_id": cid, "start_s": start, "end_s": end, "after_sentence": i}
        for i, (cid, _login, start, end) in enumerate(excerpts)
    ]
    conn.execute(
        "INSERT INTO commentary_stories (clip_id, version, hook, script, excerpts, guardrails,"
        " created_at, format) VALUES (?, ?, 'Hook', 'Script text', ?, '{}', ?, ?)",
        (
            post.clip_id,
            post.clip_version,
            json.dumps(plan),
            stamp,
            json.dumps(fmt) if fmt else None,
        ),
    )
