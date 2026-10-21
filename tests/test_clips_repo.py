import json
import sqlite3
from pathlib import Path

import pytest

from clipper.clips_repo import ClipMeta, add_clip_version, insert_pending_clip
from clipper.db import connect, migrate
from clipper.edl import parse_edl
from clipper.render import RenderResult

EDL_DATA = {"source": {"path": "a.mp4", "in_s": 10, "out_s": 40}, "captions_enabled": False}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "state.db")
    migrate(c)
    c.execute(
        "INSERT INTO jobs (id, platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES (1, 'youtube', 'abc', 'Kai', 'https://x', 't', 't')"
    )
    return c


def _result(tmp_path: Path, name: str = "clip") -> RenderResult:
    return RenderResult(
        video_path=tmp_path / f"{name}.mp4",
        thumbnail_path=tmp_path / f"{name}.jpg",
        sidecar_path=tmp_path / f"{name}.json",
        duration_s=30.0,
        render_seconds=12.0,
    )


def test_insert_pending_clip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    meta = ClipMeta(title="T", caption="C", hashtags=("a", "b"), score=0.9, reason="r")
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=meta, now="2026-10-21"
    )
    assert ref.version == 1
    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (ref.clip_id,)).fetchone()
    assert (clip["status"], clip["current_version"], clip["rank"]) == ("pending_review", 1, 1)
    ver = conn.execute("SELECT * FROM clip_versions WHERE clip_id = ?", (ref.clip_id,)).fetchone()
    assert (ver["start_s"], ver["end_s"], ver["captions_enabled"]) == (10.0, 40.0, 0)
    assert json.loads(ver["hashtags"]) == ["a", "b"]
    assert ver["video_path"] == str(tmp_path / "clip.mp4")
    assert ver["thumb_path"] == str(tmp_path / "clip.jpg")
    assert (ver["title"], ver["score"], ver["created_at"]) == ("T", 0.9, "2026-10-21")


def test_duplicate_rank_rolls_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM clip_versions").fetchone()[0] == 1


def test_add_clip_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(conn, job_id=1, rank=2, edl=edl, result=_result(tmp_path))
    conn.execute("UPDATE clips SET status = 'changes_requested' WHERE id = ?", (ref.clip_id,))
    edl2 = parse_edl({**EDL_DATA, "source": {"path": "a.mp4", "in_s": 12, "out_s": 40}})
    ref2 = add_clip_version(
        conn,
        clip_id=ref.clip_id,
        edl=edl2,
        result=_result(tmp_path, "clip_v2"),
        meta=ClipMeta(note="tighter start"),
    )
    assert ref2.version == 2
    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (ref.clip_id,)).fetchone()
    assert (clip["status"], clip["current_version"]) == ("pending_review", 2)
    ver = conn.execute(
        "SELECT * FROM clip_versions WHERE clip_id = ? AND version = 2", (ref.clip_id,)
    ).fetchone()
    assert (ver["start_s"], ver["note"]) == (12.0, "tighter start")


def test_add_version_unknown_clip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        add_clip_version(conn, clip_id=999, edl=parse_edl(EDL_DATA), result=_result(tmp_path))


def test_each_version_keeps_its_own_files(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(conn, job_id=1, rank=3, edl=edl, result=_result(tmp_path, "v1"))
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"))
    rows = conn.execute(
        "SELECT version, video_path, thumb_path FROM clip_versions WHERE clip_id = ?"
        " ORDER BY version",
        (ref.clip_id,),
    ).fetchall()
    assert [(r[0], Path(r[1]).name, Path(r[2]).name) for r in rows] == [
        (1, "v1.mp4", "v1.jpg"),
        (2, "v2.mp4", "v2.jpg"),
    ]
