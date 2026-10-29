import dataclasses
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


def test_insert_pending_clip_with_explicit_source_range(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), start_s=125.5, end_s=155.5
    )
    ver = conn.execute("SELECT * FROM clip_versions WHERE clip_id = ?", (ref.clip_id,)).fetchone()
    assert (ver["start_s"], ver["end_s"]) == (125.5, 155.5)


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


def test_rendered_duration_is_stored_apart_from_the_source_range(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """With jump cuts the video (38 s) is shorter than its source range (45 s)."""
    from clipper import clips

    result = dataclasses.replace(_result(tmp_path), duration_s=38.0)
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=parse_edl(EDL_DATA), result=result, start_s=100.0,
        end_s=145.0,
    )  # fmt: skip
    detail = clips.get_clip(conn, ref.clip_id)
    assert detail is not None
    (version,) = detail.versions
    assert (version.start_s, version.end_s, version.duration_s) == (100.0, 145.0, 38.0)
    (card,) = clips.list_queue(conn, clips.QueueFilter())
    assert card.duration_s == 38.0


def test_playbook_version_recorded_on_every_version(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    edl = parse_edl(EDL_DATA)
    meta = ClipMeta(prompt_version="select_v2", playbook_version="playbook_v1")
    ref = insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=meta)
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"), meta=meta)
    rows = conn.execute(
        "SELECT prompt_version, playbook_version FROM clip_versions WHERE clip_id = ?",
        (ref.clip_id,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("select_v2", "playbook_v1")] * 2


def test_playbook_version_defaults_to_null(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=parse_edl(EDL_DATA), result=_result(tmp_path)
    )
    row = conn.execute(
        "SELECT playbook_version FROM clip_versions WHERE clip_id = ?", (ref.clip_id,)
    ).fetchone()
    assert row[0] is None


def test_sensitive_flag_round_trips(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    flagged = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=ClipMeta(sensitive=True)
    )
    plain = insert_pending_clip(conn, job_id=1, rank=2, edl=edl, result=_result(tmp_path))
    again = add_clip_version(
        conn,
        clip_id=flagged.clip_id,
        edl=edl,
        result=_result(tmp_path, "v2"),
        meta=ClipMeta(sensitive=True),
    )
    rows = conn.execute("SELECT clip_id, version, sensitive FROM clip_versions ORDER BY id")
    assert [tuple(r) for r in rows] == [
        (flagged.clip_id, 1, 1),
        (plain.clip_id, 1, 0),
        (flagged.clip_id, again.version, 1),
    ]


def test_new_version_keeps_the_sensitive_flag(conn: sqlite3.Connection, tmp_path: Path) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=ClipMeta(sensitive=True)
    )
    v2 = add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"))
    row = conn.execute(
        "SELECT sensitive FROM clip_versions WHERE clip_id = ? AND version = ?",
        (ref.clip_id, v2.version),
    ).fetchone()
    assert row[0] == 1


# --- quality gates (CLP-74) -------------------------------------------------------------


def _status_and_reasons(conn: sqlite3.Connection, clip_id: int) -> tuple[str, object]:
    row = conn.execute(
        "SELECT c.status, v.gate_reasons FROM clips c JOIN clip_versions v"
        " ON v.clip_id = c.id AND v.version = c.current_version WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    return row[0], None if row[1] is None else json.loads(row[1])


@pytest.mark.parametrize(
    ("reasons", "status"),
    [
        ((), "pending_review"),
        (("gate_error",), "pending_review"),
        (("loudness",), "needs_attention"),
        (("word_crosses_join", "gate_error"), "needs_attention"),
    ],
)
def test_gate_reasons_decide_the_first_status(
    conn: sqlite3.Connection, tmp_path: Path, reasons: tuple[str, ...], status: str
) -> None:
    ref = insert_pending_clip(
        conn,
        job_id=1,
        rank=1,
        edl=parse_edl(EDL_DATA),
        result=_result(tmp_path),
        meta=ClipMeta(gate_reasons=reasons),
    )
    assert _status_and_reasons(conn, ref.clip_id) == (status, list(reasons))


def test_ungated_version_stores_no_reasons(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A re-render is not gated yet: NULL, not an empty (passed) list."""
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path),
        meta=ClipMeta(gate_reasons=("loudness",)),
    )  # fmt: skip
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"))
    assert _status_and_reasons(conn, ref.clip_id) == ("pending_review", None)


def test_gate_reasons_reach_the_clip_detail(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from clipper import clips

    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=parse_edl(EDL_DATA), result=_result(tmp_path),
        meta=ClipMeta(gate_reasons=("late_speech",)),
    )  # fmt: skip
    detail = clips.get_clip(conn, ref.clip_id)
    assert detail is not None and detail.current.gate_reasons == ("late_speech",)


def test_gated_re_render_that_fails_needs_attention(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path))
    add_clip_version(
        conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"),
        meta=ClipMeta(gate_reasons=("loudness",)),
    )  # fmt: skip
    assert _status_and_reasons(conn, ref.clip_id) == ("needs_attention", ["loudness"])


# --- per-platform variants (CLP-96) -----------------------------------------------------------

VARIANTS = {
    p: {"title": f"{p} title", "caption": f"{p} caption", "hashtags": ["#a1", "#b1", "#c1"]}
    for p in ("tiktok", "instagram", "youtube", "facebook")
}


def _variants(conn: sqlite3.Connection, clip_id: int) -> list[object]:
    rows = conn.execute(
        "SELECT variants FROM clip_versions WHERE clip_id = ? ORDER BY version", (clip_id,)
    )
    return [None if r[0] is None else json.loads(r[0]) for r in rows]


def test_variants_are_stored_as_json(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ref = insert_pending_clip(
        conn,
        job_id=1,
        rank=1,
        edl=parse_edl(EDL_DATA),
        result=_result(tmp_path),
        meta=ClipMeta(variants=VARIANTS),
    )
    assert _variants(conn, ref.clip_id) == [VARIANTS]
    plain = insert_pending_clip(
        conn, job_id=1, rank=2, edl=parse_edl(EDL_DATA), result=_result(tmp_path)
    )
    assert _variants(conn, plain.clip_id) == [None]


def test_new_version_carries_the_variants_forward(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A re-render keeps the reviewer's per-platform text unless it brings its own."""
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(
        conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=ClipMeta(variants=VARIANTS)
    )
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"))
    own = {**VARIANTS, "tiktok": {**VARIANTS["tiktok"], "title": "new"}}
    add_clip_version(
        conn,
        clip_id=ref.clip_id,
        edl=edl,
        result=_result(tmp_path, "v3"),
        meta=ClipMeta(variants=own),
    )
    assert _variants(conn, ref.clip_id) == [VARIANTS, VARIANTS, own]


def test_new_text_on_a_re_render_derives_fresh_variants(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Review fix: variants never contradict the version's own title and caption."""
    edl = parse_edl(EDL_DATA)
    meta = ClipMeta(title="T", caption="C", hashtags=("#a1",), variants=VARIANTS)
    ref = insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path), meta=meta)
    same = ClipMeta(title="T", caption="C", hashtags=("#a1",))
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"), meta=same)
    add_clip_version(
        conn,
        clip_id=ref.clip_id,
        edl=edl,
        result=_result(tmp_path, "v3"),
        meta=ClipMeta(title="New title"),
    )
    assert _variants(conn, ref.clip_id) == [VARIANTS, VARIANTS, None]


def test_unreadable_stored_variants_do_not_break_a_re_render(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    edl = parse_edl(EDL_DATA)
    ref = insert_pending_clip(conn, job_id=1, rank=1, edl=edl, result=_result(tmp_path))
    conn.execute("UPDATE clip_versions SET variants = '{broken' WHERE clip_id = ?", (ref.clip_id,))
    add_clip_version(conn, clip_id=ref.clip_id, edl=edl, result=_result(tmp_path, "v2"))
    row = conn.execute(
        "SELECT variants FROM clip_versions WHERE clip_id = ? AND version = 2", (ref.clip_id,)
    ).fetchone()
    assert row[0] is None
