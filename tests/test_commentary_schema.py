"""Migration 0011 (CLP-143): story_candidates, streamer_rankings, commentary_stories."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from clipper.db import bundled_migrations, connect, migrate

NOW = "2026-11-19T11:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    c.execute(
        "INSERT INTO jobs (id, platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES (1, 'commentary', 'random_gaming_yt:2026-11-19', 'random_gaming_yt', '', ?, ?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO clips (id, job_id, rank, created_at, updated_at) VALUES (1, 1, 1, ?, ?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO clip_versions (clip_id, version, start_s, end_s, created_at)"
        " VALUES (1, 1, 0, 42.5, ?)",
        (NOW,),
    )
    yield c
    c.close()


def _candidate(conn: sqlite3.Connection, twitch_clip_id: str, used_by: int | None = None) -> None:
    conn.execute(
        "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id, broadcaster_id,"
        " broadcaster_login, url, view_count, duration_s, clip_created_at, score,"
        " used_by_clip_id, created_at, updated_at)"
        " VALUES (1, 'random_gaming_yt', ?, '123', 'kaicenat', 'https://clips.twitch.tv/x',"
        " 1000, 30.0, ?, 1500.0, ?, ?, ?)",
        (twitch_clip_id, NOW, used_by, NOW, NOW),
    )


def _ranking(conn: sqlite3.Connection, rank: int, broadcaster_id: str) -> None:
    conn.execute(
        "INSERT INTO streamer_rankings (job_id, rank, broadcaster_id, broadcaster_login,"
        " clip_count, view_count, views_per_hour, created_at)"
        " VALUES (1, ?, ?, 'kaicenat', 3, 90000, 4500.0, ?)",
        (rank, broadcaster_id, NOW),
    )


def _story(conn: sqlite3.Connection, version: int, script: str = "A script.") -> None:
    conn.execute(
        "INSERT INTO commentary_stories (clip_id, version, hook, script, excerpts, guardrails,"
        " created_at) VALUES (1, ?, 'Hook', ?, '[]', '{}', ?)",
        (version, script, NOW),
    )


def test_migration_0011_is_bundled_and_makes_the_tables(conn: sqlite3.Connection) -> None:
    assert (11, "commentary_storage") in [(m.version, m.name) for m in bundled_migrations()]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"story_candidates", "streamer_rankings", "commentary_stories"} <= tables


def test_a_twitch_clip_is_one_candidate(conn: sqlite3.Connection) -> None:
    _candidate(conn, "AwkwardHelplessSalamander")
    pinned = conn.execute("SELECT pinned FROM story_candidates").fetchone()[0]

    assert pinned == 0
    with pytest.raises(sqlite3.IntegrityError):
        _candidate(conn, "AwkwardHelplessSalamander")


def test_a_candidate_is_used_by_a_real_clip(conn: sqlite3.Connection) -> None:
    _candidate(conn, "UsedOnce", used_by=1)

    with pytest.raises(sqlite3.IntegrityError):
        _candidate(conn, "NoSuchShort", used_by=999)


def test_a_days_ranking_has_one_row_per_rank_and_streamer(conn: sqlite3.Connection) -> None:
    _ranking(conn, 1, "123")
    _ranking(conn, 2, "456")

    with pytest.raises(sqlite3.IntegrityError):
        _ranking(conn, 2, "789")  # rank 2 is taken
    with pytest.raises(sqlite3.IntegrityError):
        _ranking(conn, 3, "123")  # streamer 123 is already ranked
    with pytest.raises(sqlite3.IntegrityError):
        _ranking(conn, 0, "999")  # ranks start at 1


def test_a_story_belongs_to_one_clip_version(conn: sqlite3.Connection) -> None:
    _story(conn, 1)

    with pytest.raises(sqlite3.IntegrityError):
        _story(conn, 1)  # one story per version
    with pytest.raises(sqlite3.IntegrityError):
        _story(conn, 2)  # clip 1 has no version 2


def test_a_story_script_is_not_blank(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _story(conn, 1, script="   ")
