"""Format tags of a Short (CLP-154): derived for pipeline posts at approval (never
blocking it), defaulted for hand uploads, and set by the owner for any video id."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper.insights import repo
from clipper.insights.tags import (
    TAG_KEYS,
    derive_tags,
    ensure_video_tags,
    length_bucket,
    owner_tags,
    parse_pairs,
    share_bucket,
    tag_post,
)
from clipper.posting import repo as posting_repo
from clipper.review import routes
from insights_support import HAND_UPLOADS, T0, open_db, short, story, upload
from posting_support import FakeAccount
from review_support import Env, login, make_env


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(tmp_path / "state.db")
    yield c
    c.close()


def test_a_commentary_short_gets_all_eight_tags(conn: sqlite3.Connection) -> None:
    post = short(conn, duration_s=42.5, scheduled_at=datetime(2027, 1, 15, 23, 30, tzinfo=UTC))
    story(
        conn,
        post,
        excerpts=[("ClipA", "kaicenat", 1.0, 6.0), ("ClipB", "xqc", 10.0, 14.5)],
        fmt={
            "hook_type": "Question",
            "structure": "setup-payoff",
            "palette": "neon",
            "topic": "GTA VI trailer",
        },
    )

    tags = tag_post(conn, post, "America/New_York")

    assert tags == {
        "hook_type": "question",
        "structure": "setup-payoff",
        "palette": "neon",
        "topic": "gta vi trailer",
        "length_bucket": "40-60s",
        "clip_share": "20-30%",  # 9.5 s of 42.5 s
        "streamers": "kaicenat,xqc",
        "publish_hour": "18",  # 23:30 UTC is 18:30 in New York
    }
    assert repo.tags_for_post(conn, post.id) == tags
    vid = post.external_id or ""  # an uploaded post is tagged by its video id too
    assert repo.tags_for_videos(conn, [vid]) == {vid: tags}


def test_without_a_story_the_commentary_tags_say_unknown(conn: sqlite3.Connection) -> None:
    post = short(conn, duration_s=18.0)

    tags = derive_tags(conn, post, "UTC")

    assert set(tags) == set(TAG_KEYS)
    assert tags["length_bucket"] == "<20s"
    for key in ("hook_type", "structure", "palette", "topic", "streamers", "clip_share"):
        assert tags[key] == "unknown"


def test_format_values_are_cleaned_and_capped(conn: sqlite3.Connection) -> None:
    post = short(conn)
    story(
        conn,
        post,
        excerpts=[("ClipC", "kaicenat", 0.0, 3.0)],
        fmt={"hook_type": "Hook\u202eType\nX" + "y" * 200},
    )

    value = derive_tags(conn, post, "UTC")["hook_type"]

    assert "\u202e" not in value and "\n" not in value
    assert value.startswith("hooktype x") and len(value) <= 80


def test_a_hand_upload_gets_length_and_hour_and_unknown_for_the_rest(
    conn: sqlite3.Connection,
) -> None:
    video = upload(conn, HAND_UPLOADS[0], duration_s=58.0, published_at=T0)  # 07:00 New York

    ensure_video_tags(conn, video, "America/New_York")

    tags = repo.tags_for_videos(conn, [video.video_id])[video.video_id]
    assert tags["length_bucket"] == "40-60s" and tags["publish_hour"] == "07"
    assert {tags[k] for k in TAG_KEYS if k not in ("length_bucket", "publish_hour")} == {"unknown"}


def test_the_owner_tags_a_hand_upload_and_keys_not_given_stay_unknown(
    conn: sqlite3.Connection,
) -> None:
    video = upload(conn, HAND_UPLOADS[1], duration_s=25.0, published_at=T0)
    ensure_video_tags(conn, video, "UTC")

    tags = owner_tags(
        conn, video.video_id, parse_pairs(["hook_type=Question", "streamers=kaicenat,xqc"]), "UTC"
    )

    assert tags["hook_type"] == "question" and tags["streamers"] == "kaicenat,xqc"
    assert tags["length_bucket"] == "20-40s" and tags["publish_hour"] == "12"
    assert tags["palette"] == "unknown" and tags["topic"] == "unknown"
    again = owner_tags(conn, video.video_id, {"topic": "GTA VI"})
    assert again["hook_type"] == "question" and again["topic"] == "gta vi"


def test_the_owner_can_tag_a_video_the_job_has_not_seen_yet(conn: sqlite3.Connection) -> None:
    tags = owner_tags(conn, HAND_UPLOADS[2], {"palette": "neon"})

    assert tags["palette"] == "neon"
    assert {tags[k] for k in TAG_KEYS if k != "palette"} == {"unknown"}


def test_tagging_an_uploaded_post_keeps_the_owners_video_tags(conn: sqlite3.Connection) -> None:
    vid = HAND_UPLOADS[0]
    owner_tags(conn, vid, {"hook_type": "reaction"})  # before the job knew the post
    post = short(conn, duration_s=42.5, video_id=vid)
    story(conn, post, excerpts=[("ClipA", "kaicenat", 1.0, 6.0)], fmt={"hook_type": "question"})

    tag_post(conn, post, "UTC")

    tags = repo.tags_for_videos(conn, [vid])[vid]
    assert tags["hook_type"] == "reaction"  # the owner's value wins
    assert tags["length_bucket"] == "40-60s" and tags["streamers"] == "kaicenat"  # filled in
    assert repo.tags_for_post(conn, post.id) == tags


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["hook_type"], "key=value"),
        (["mood=happy"], "unknown tag key 'mood'"),
        (["topic="], "empty value"),
    ],
)
def test_bad_owner_tags_are_refused(args: list[str], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_pairs(args)


def test_a_bad_video_id_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="not a YouTube video id"):
        owner_tags(conn, "https://youtu.be/x", {"topic": "x"})


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [
        (None, "unknown"),
        (19.9, "<20s"),
        (20.0, "20-40s"),
        (59.9, "40-60s"),
        (60.0, "60-90s"),
        (90.0, "90s+"),
    ],
)
def test_length_buckets(seconds: float | None, bucket: str) -> None:
    assert length_bucket(seconds) == bucket


@pytest.mark.parametrize(
    ("share", "bucket"),
    [(None, "unknown"), (0.0, "0-10%"), (0.35, "30-40%"), (1.0, "90-100%"), (1.7, "90-100%")],
)
def test_share_buckets(share: float | None, bucket: str) -> None:
    assert share_bucket(share) == bucket


# --- approval ----------------------------------------------------------------------------

ACCOUNTS = (
    FakeAccount(id="tt_main", windows=["00:00-23:59"]),
    FakeAccount(id="yt_main", platform="youtube", windows=["00:00-23:59"]),
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def logged_in(env: Env) -> TestClient:
    client = env.client(accounts=ACCOUNTS)
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


def approve(client: TestClient, clip_id: int):
    headers = {"X-CSRF-Token": client.csrf, "HX-Request": "true"}  # type: ignore[attr-defined]
    return client.post(f"/clip/{clip_id}/approve", data={"version": 1}, headers=headers)


def video_clip(env: Env) -> int:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    target = env.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake mp4")
    return env.clip(job, video_path=rel)  # 125-155 s: 30 s


def test_approval_writes_format_tags_for_the_youtube_post_only(env: Env) -> None:
    client = logged_in(env)
    clip_id = video_clip(env)

    assert approve(client, clip_id).status_code == 200

    posts = {p.platform: p for p in posting_repo.list_posts(env.conn, clip_id=clip_id)}
    assert set(posts) == {"tiktok", "youtube"}
    found = repo.tags_for_post(env.conn, posts["youtube"].id)
    assert set(found) == set(TAG_KEYS)
    assert found["length_bucket"] == "20-40s"
    assert found["streamers"] == "mrbeast"  # one spelling per creator
    assert found["clip_share"] == "90-100%"
    assert repo.tags_for_post(env.conn, posts["tiktok"].id) == {}  # insights are YouTube only


def test_a_derived_streamer_and_an_owner_set_streamer_are_one_spelling(env: Env) -> None:
    client = logged_in(env)
    clip_id = video_clip(env)
    assert approve(client, clip_id).status_code == 200
    (post,) = [
        p for p in posting_repo.list_posts(env.conn, clip_id=clip_id) if p.platform == "youtube"
    ]

    derived = repo.tags_for_post(env.conn, post.id)["streamers"]
    owned = owner_tags(env.conn, HAND_UPLOADS[0], parse_pairs(["streamers=MrBeast"]))

    assert derived == owned["streamers"] == "mrbeast"


def test_a_tagging_failure_never_blocks_the_approval(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: object, **__: object) -> dict[str, str]:
        raise RuntimeError("tagger exploded")

    monkeypatch.setattr(routes, "tag_post", boom)
    client = logged_in(env)
    clip_id = video_clip(env)

    assert approve(client, clip_id).status_code == 200

    assert env.status(clip_id) in ("approved", "exported")
    posts = posting_repo.list_posts(env.conn, clip_id=clip_id)
    assert len(posts) == 2
    assert all(repo.tags_for_post(env.conn, p.id) == {} for p in posts)
    assert "tagging post" in caplog.text
