"""RENDERING stage (CLP-63): sections.json -> rendered `pending_review` clips.

`render_clip` is faked (it writes the three output files and records the EDL); the real
Remotion render is covered by tests/test_render_integration.py and the e2e test.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from clipper.config import AppConfig
from clipper.db import connect, migrate
from clipper.edl import EDL
from clipper.jobs import Worker, enqueue, get_job
from clipper.jobs.models import Job, JobStatus
from clipper.jobs.stages import PermanentError, StageContext
from clipper.pipeline import build_stages
from clipper.pipeline import render_clips as rc
from clipper.pipeline.common import Deadline, StageTimeout
from clipper.render import InvalidClipError, RenderError, RenderResult
from clipper.urls import SourceRef

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class Crash(BaseException):
    """Stands in for the worker process being killed."""


class FakeRender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on: dict[int, BaseException] = {}

    def __call__(
        self, edl: EDL, media_root: Path, out_dir: Path, *, basename: str, **kw: Any
    ) -> RenderResult:
        self.calls.append({"edl": edl, "media_root": media_root, "out_dir": out_dir, **kw})
        if len(self.calls) in self.fail_on:
            raise self.fail_on[len(self.calls)]
        assert (media_root / edl.source.path).is_file()
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [out_dir / f"{basename}.{ext}" for ext in ("mp4", "jpg", "json")]
        for path in paths:
            path.write_bytes(b"x")
        return RenderResult(*paths, duration_s=edl.duration_s, render_seconds=1.0)


def _highlight(rank: int, start: float, end: float, **kw: Any) -> dict[str, Any]:
    return {
        "rank": rank,
        "start_s": start,
        "end_s": end,
        "hook": f"Hook {rank}",
        "title": f"Title {rank}",
        "caption": f"Caption {rank}",
        "hashtags": ["#clip", f"#r{rank}"],
        "score": 90 - rank,
        "reason": f"Reason {rank}",
        **kw,
    }


def _section(rank: int, start: float, end: float, pad: float = 2.0) -> dict[str, Any]:
    return {
        "rank": rank,
        "path": f"clips/{rank}/source.mp4",
        "requested_start_s": start,
        "requested_end_s": end,
        "pad_start_s": pad,
        "duration_s": end - start + 2 * pad,
        "width": 320,
        "height": 180,
    }


def _setup(
    media: Path,
    highlights: list[dict[str, Any]],
    sections: list[dict[str, Any]] | None = None,
    words: list[dict[str, Any]] | None = None,
) -> None:
    if sections is None:
        sections = [_section(h["rank"], h["start_s"], h["end_s"]) for h in highlights]
    for s in sections:
        (media / s["path"]).parent.mkdir(parents=True, exist_ok=True)
        (media / s["path"]).write_bytes(b"mp4")
    if words is None:
        words = [{"word": f" w{i}", "start": i + 0.1, "end": i + 0.6} for i in range(60)]
    (media / "transcript.json").write_text(json.dumps({"language": "en", "words": words}))
    (media / "highlights.json").write_text(
        json.dumps({"model": "m", "prompt_version": "select_v1", "clips": highlights})
    )
    (media / "sections.json").write_text(json.dumps({"clips": sections}))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def job(conn: sqlite3.Connection) -> Job:
    job, _ = enqueue(conn, SourceRef("youtube", "dQw4w9WgXcQ"), T0, creator="manual")
    return job


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "media" / "youtube_x"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRender:
    render = FakeRender()
    monkeypatch.setattr(rc, "render_clip", render)
    return render


def _ctx(media: Path, conn: sqlite3.Connection) -> StageContext:
    return StageContext(media_dir=media, conn=conn, config=None)  # type: ignore[arg-type]


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.job_id, c.rank, c.status, c.current_version, v.* FROM clips c"
        " JOIN clip_versions v ON v.clip_id = c.id ORDER BY c.rank"
    ).fetchall()


def test_registered_for_rendering() -> None:
    assert build_stages()[JobStatus.RENDERING] is rc.STAGE


def test_renders_each_section_into_a_pending_clip(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0), _highlight(2, 30.5, 40.5)])

    rc.render_clips(job, _ctx(media, conn))

    first = fake.calls[0]
    assert first["media_root"] == media and first["out_dir"] == media / "clips/1"
    edl = first["edl"]
    # The requested range inside the padded file: [pad, pad + length].
    assert (edl.source.path, edl.source.in_s, edl.source.out_s) == ("clips/1/source.mp4", 2.0, 7.0)
    assert edl.hook is not None and edl.hook.text == "Hook 1"
    # Section-relative words (section starts at 8.0 s): only words overlapping [10, 15].
    words = [(w.text, w.start_s, w.end_s) for w in edl.words]
    assert words[0] == ("w10", pytest.approx(2.1), pytest.approx(2.6))
    assert words[-1] == ("w14", pytest.approx(6.1), pytest.approx(6.6))
    assert len(words) == 5
    assert 0 < first["timeout_s"] <= rc.DEFAULT_RENDER_TIMEOUT_S

    rows = _rows(conn)
    assert [(r["job_id"], r["rank"], r["status"], r["version"]) for r in rows] == [
        (job.id, 1, "pending_review", 1),
        (job.id, 2, "pending_review", 1),
    ]
    one = rows[0]
    assert (one["title"], one["caption"], one["score"], one["reason"]) == (
        "Title 1",
        "Caption 1",
        89,
        "Reason 1",
    )
    assert json.loads(one["hashtags"]) == ["#clip", "#r1"]
    assert one["prompt_version"] == "select_v1"
    # Stored range is the position in the source video (the review page links there).
    assert (one["start_s"], one["end_s"]) == (10.0, 15.0)
    assert one["video_path"] == str(media / "clips/1/v1.mp4")
    assert one["thumb_path"] == str(media / "clips/1/v1.jpg")


def test_word_at_clamped_section_start_and_no_hook(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    # Clip at 0.5 s: padding was clamped to 0.5; a word straddling the start is kept.
    words = [{"word": "a", "start": 0.2, "end": 0.9}, {"word": "  ", "start": 1.0, "end": 1.2}]
    _setup(
        media,
        [_highlight(1, 0.5, 5.0, hook="")],
        sections=[_section(1, 0.5, 5.0, pad=0.5)],
        words=words,
    )

    rc.render_clips(job, _ctx(media, conn))

    edl = fake.calls[0]["edl"]
    assert edl.hook is None
    assert [(w.text, w.start_s, w.end_s) for w in edl.words] == [("a", 0.2, 0.9)]


def test_crash_resumes_without_duplicates(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2, 3)])
    fake.fail_on[2] = Crash()

    with pytest.raises(Crash):
        rc.render_clips(job, _ctx(media, conn))
    assert [r["rank"] for r in _rows(conn)] == [1]

    rc.render_clips(job, _ctx(media, conn))
    assert [r["rank"] for r in _rows(conn)] == [1, 2, 3]
    # Clip 1 was not rendered again.
    assert [c["out_dir"].name for c in fake.calls] == ["1", "2", "2", "3"]

    rc.render_clips(job, _ctx(media, conn))  # everything done: a no-op
    assert len(fake.calls) == 4 and len(_rows(conn)) == 3


def test_render_failure_is_retryable(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    fake.fail_on[1] = RenderError("remotion render timed out after 900s")

    with pytest.raises(RenderError):
        rc.render_clips(job, _ctx(media, conn))
    assert _rows(conn) == []


def test_invalid_clip_is_skipped_others_render(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    highlights = [
        _highlight(1, 10.0, 15.0, hook="x" * 121),  # too long for the hook card
        _highlight(2, 20.0, 25.0),
        _highlight(3, 30.0, 300.0),  # longer than an EDL allows
    ]
    _setup(media, highlights)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [2]
    assert len(fake.calls) == 1
    (summary,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "2 clip(s) left out" in summary.message
    assert "clip 1: invalid EDL" in summary.message and "clip 3: invalid EDL" in summary.message


def test_renderer_rejecting_a_clip_skips_only_that_clip(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0), _highlight(2, 20.0, 25.0)])
    fake.fail_on[1] = InvalidClipError("source media not found: x")

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [2]


@pytest.mark.parametrize(
    "bad",
    [
        {"title": 5},
        {"caption": ["x"]},
        {"reason": {"a": 1}},
        {"score": True},
        {"score": "90"},
        {"score": float("nan")},
        {"hashtags": "#clip"},
        {"hashtags": ["#ok", 3]},
    ],
)
def test_bad_metadata_is_invalid(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, bad: dict[str, Any]
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0, **bad)])
    with pytest.raises(PermanentError, match="clip 1: invalid metadata"):
        rc.render_clips(job, _ctx(media, conn))
    assert fake.calls == []


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1.0, 5.0), (5.0, 5.0), (6.0, 5.0), ("1", 5.0), (1.0, float("inf")), (True, 5.0)],
)
def test_bad_requested_range_is_invalid(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, start: Any, end: Any
) -> None:
    section = {**_section(1, 10.0, 15.0), "requested_start_s": start, "requested_end_s": end}
    _setup(media, [_highlight(1, 10.0, 15.0)], sections=[section])
    with pytest.raises(PermanentError, match="clip 1: invalid range"):
        rc.render_clips(job, _ctx(media, conn))


def test_control_and_format_characters_stripped_from_words(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    words = [
        {"word": "a\u202eb\u200b", "start": 10.1, "end": 10.5},
        {"word": "c\x00\nd", "start": 11.1, "end": 11.5},
        {"word": "\u200d", "start": 12.1, "end": 12.5},
        {"word": "x" * 70, "start": 13.1, "end": 13.5},
    ]
    _setup(media, [_highlight(1, 10.0, 15.0)], words=words)

    rc.render_clips(job, _ctx(media, conn))

    texts = [w.text for w in fake.calls[0]["edl"].words]
    assert texts == ["ab", "c d", "x" * 64]


def test_small_stage_budget_stops_before_a_clip(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    monkeypatch.setattr(rc, "Deadline", lambda: Deadline(budget_s=rc.CLIP_WORST_CASE_S - 1))
    with pytest.raises(StageTimeout):
        rc.render_clips(job, _ctx(media, conn))
    assert fake.calls == [] and _rows(conn) == []


def test_transient_render_error_is_retried_by_the_worker(
    conn: sqlite3.Connection, job: Job, tmp_path: Path, fake: FakeRender
) -> None:
    conn.execute("UPDATE jobs SET status = 'rendering' WHERE id = ?", (job.id,))
    media_root = tmp_path / "media"
    media = media_root / job.ref.key
    media.mkdir(parents=True)
    _setup(media, [_highlight(1, 10.0, 15.0)])
    fake.fail_on[1] = RenderError("remotion render failed with exit code 1")
    worker = Worker(
        conn=conn,
        config=AppConfig(),
        worker_id="w",
        stages=build_stages(),
        media_root=media_root,
        clock=lambda: T0,
    )

    assert worker.run_once()

    after = get_job(conn, job.id)
    assert after is not None and after.status is JobStatus.RENDERING
    assert after.attempts == 1
    assert after.last_error is not None and "remotion render failed" in after.last_error
    assert _rows(conn) == []


def test_all_clips_invalid_fails_the_job(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(
        media,
        [_highlight(1, 10.0, 15.0)],
        sections=[{**_section(1, 10.0, 15.0), "path": "../escape.mp4"}],
    )
    with pytest.raises(PermanentError, match="no clip could be rendered: clip 1: invalid EDL"):
        rc.render_clips(job, _ctx(media, conn))
    assert fake.calls == []


@pytest.mark.parametrize(
    "section",
    [
        {"rank": True},
        {"rank": "1"},
        {"rank": 0},
        "not an object",
        {"rank": 1},  # missing fields
    ],
)
def test_malformed_section_is_invalid(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, section: Any
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)], sections=[])
    (media / "sections.json").write_text(json.dumps({"clips": [section]}))
    with pytest.raises(PermanentError, match="no clip could be rendered"):
        rc.render_clips(job, _ctx(media, conn))


def test_section_without_highlight_is_invalid(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)], sections=[_section(2, 10.0, 15.0)])
    with pytest.raises(PermanentError, match="clip 2: no highlight"):
        rc.render_clips(job, _ctx(media, conn))


@pytest.mark.parametrize("name", ["sections.json", "highlights.json", "transcript.json"])
def test_missing_input_is_permanent(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, name: str
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    (media / name).unlink()
    with pytest.raises(PermanentError, match=name):
        rc.render_clips(job, _ctx(media, conn))


def test_sections_without_clips_list_is_permanent(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    (media / "sections.json").write_text("[]")
    with pytest.raises(PermanentError, match="sections.json has no clips list"):
        rc.render_clips(job, _ctx(media, conn))
