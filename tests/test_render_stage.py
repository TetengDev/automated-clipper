"""RENDERING stage (CLP-63): sections.json -> rendered `pending_review` clips.

`render_clip` is faked (it writes the three output files and records the EDL); the real
Remotion render is covered by tests/test_render_integration.py and the e2e test.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clipper.config import AppConfig, Creator, CreatorOverrides, Settings, YouTubeChannel
from clipper.db import connect, migrate
from clipper.edl import EDL, parse_edl
from clipper.jobs import Worker, enqueue, get_job
from clipper.jobs.models import Job, JobStatus, to_iso
from clipper.jobs.queue import MAX_ATTEMPTS, backoff
from clipper.jobs.stages import Deferred, PermanentError, StageContext
from clipper.pipeline import boundaries as bd
from clipper.pipeline import build_stages
from clipper.pipeline import reframe as rf
from clipper.pipeline import render_clips as rc
from clipper.pipeline.common import Deadline, StageTimeout
from clipper.render import InvalidClipError, RenderError, RenderResult, segment_props
from clipper.urls import SourceRef

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class Crash(BaseException):
    """Stands in for the worker process being killed."""


class FakeRender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on: dict[int, BaseException] = {}
        self.fail_rank: dict[int, BaseException] = {}  # raised on every call for that rank
        self.after: list[Callable[[], None]] = []  # run after each successful render

    def __call__(
        self, edl: EDL, media_root: Path, out_dir: Path, *, basename: str, **kw: Any
    ) -> RenderResult:
        self.calls.append({"edl": edl, "media_root": media_root, "out_dir": out_dir, **kw})
        if len(self.calls) in self.fail_on:
            raise self.fail_on[len(self.calls)]
        if int(out_dir.name) in self.fail_rank:
            raise self.fail_rank[int(out_dir.name)]
        assert (media_root / edl.source.path).is_file()
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [out_dir / f"{basename}.{ext}" for ext in ("mp4", "jpg", "json")]
        for path in paths:
            path.write_bytes(b"x")
        for hook in self.after:
            hook()
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
    versions: dict[str, Any] | None = None,
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
        json.dumps(
            {"model": "m", **(versions or {"prompt_version": "select_v1"}), "clips": highlights}
        )
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


@pytest.fixture(autouse=True)
def cuts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Scene cuts the fake detector reports (none by default)."""
    found: list[float] = []
    monkeypatch.setattr(rc, "detect_cuts", lambda path, deadline: found)
    return found


class FakeSilences(list[Any]):
    """What the fake silencedetect reports, in section time (none by default); a None
    entry makes it fail. `calls` holds the paths it ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Path] = []

    def detect(self, path: Path, deadline: Deadline) -> list[tuple[float, float]] | None:
        self.calls.append(path)
        return None if None in self else list(self)


@pytest.fixture(autouse=True)
def silences(monkeypatch: pytest.MonkeyPatch) -> FakeSilences:
    fake = FakeSilences()
    monkeypatch.setattr(rc, "detect_silences", fake.detect)
    return fake


class FakeFaces:
    """Stands in for the face model fetch and the detection child (no faces by default)."""

    def __init__(self) -> None:
        self.model: Path | None = Path("model.tflite")
        self.result: rf.Faces | None = None
        self.calls: list[tuple[Path, Path]] = []

    def detect(self, path: Path, model: Path, deadline: Deadline) -> rf.Faces | None:
        self.calls.append((path, model))
        return self.result


@pytest.fixture(autouse=True)
def faces(monkeypatch: pytest.MonkeyPatch) -> FakeFaces:
    fake = FakeFaces()
    monkeypatch.setattr(rc, "ensure_model", lambda **kw: fake.model)
    monkeypatch.setattr(rc, "detect_faces", fake.detect)
    return fake


class FakeGates:
    """Stands in for the quality gates (CLP-74): every clip passes unless `reasons` says."""

    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def check(self, edl: EDL, video: Path, **kw: Any) -> list[str]:
        self.calls.append({"edl": edl, "video": video, **kw})
        return list(self.reasons)


@pytest.fixture(autouse=True)
def gates(monkeypatch: pytest.MonkeyPatch) -> FakeGates:
    fake = FakeGates()
    monkeypatch.setattr(rc, "check_clip", fake.check)
    return fake


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRender:
    render = FakeRender()
    monkeypatch.setattr(rc, "render_clip", render)
    return render


def _ctx(media: Path, conn: sqlite3.Connection) -> StageContext:
    return StageContext(media_dir=media, conn=conn, config=AppConfig())


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
    assert 0 < first["ffmpeg_timeout_s"] <= rc.DEFAULT_FFMPEG_TIMEOUT_S

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
    assert one["playbook_version"] is None  # v1 highlights carry no playbook
    # Stored range is the position in the source video (the review page links there).
    assert (one["start_s"], one["end_s"]) == (10.0, 15.0)
    assert one["video_path"] == str(media / "clips/1/v1.mp4")
    assert one["thumb_path"] == str(media / "clips/1/v1.jpg")


def test_boundaries_are_trimmed_then_snapped(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Section [8, 42] in the source (pad 2), clip [10, 40]: in section time [2, 32].
    words = [
        {"word": "early", "start": 9.6, "end": 10.4},
        {"word": "mid", "start": 20.0, "end": 21.0},
        {"word": "last", "start": 37.0, "end": 37.5},
    ]
    _setup(
        media,
        [_highlight(1, 10.0, 40.0)],
        sections=[_section(1, 10.0, 40.0)],
        words=words,
    )
    seen: list[Path] = []

    def detect(path: Path, deadline: Deadline) -> list[float]:
        seen.append(path)
        return [1.5, 31.2]

    monkeypatch.setattr(rc, "detect_cuts", detect)

    rc.render_clips(job, _ctx(media, conn))

    assert seen == [(media / "clips/1/source.mp4").resolve()]
    edl = fake.calls[0]["edl"]
    # Start: "early" straddles 2.0, so no trim; the cut at 1.5 completes the word.
    # End: trimmed to 0.4 s after "last" (29.5); the cut at 31.2 is beyond the tolerance.
    assert (edl.source.in_s, edl.source.out_s) == (1.5, pytest.approx(29.9))
    assert [(w.text, w.start_s, w.end_s) for w in edl.words] == [
        ("early", pytest.approx(1.6), pytest.approx(2.4)),
        ("mid", 12.0, 13.0),
        ("last", 29.0, 29.5),
    ]
    (row,) = _rows(conn)
    assert (row["start_s"], row["end_s"]) == (9.5, pytest.approx(37.9))

    rc.render_clips(job, _ctx(media, conn))  # done: no second detection
    assert len(seen) == 1


def test_creator_clip_limits_reach_the_polish(
    conn: sqlite3.Connection, media: Path, fake: FakeRender, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        settings=Settings(clip_min_seconds=20, clip_max_seconds=60),
        creators=(
            Creator(
                name="Long",
                region="global",
                youtube=YouTubeChannel(handle="@LongForm"),
                overrides=CreatorOverrides(clip_min_seconds=25, clip_max_seconds=300),
            ),
        ),
    )
    job, _ = enqueue(conn, SourceRef("youtube", "dQw4w9WgXcQ"), T0, creator="Long")
    _setup(media, [_highlight(1, 10.0, 40.0)])
    limits: list[tuple[float, float]] = []
    real = rc.polish

    def spy(*args: Any, **kw: Any) -> tuple[float, float]:
        limits.append(args[4:6])
        return real(*args, **kw)

    monkeypatch.setattr(rc, "polish", spy)
    rc.render_clips(job, StageContext(media_dir=media, conn=conn, config=config))
    # The creator's minimum; the maximum is capped at what an EDL allows.
    assert limits == [(25, rc.MAX_CLIP_SECONDS)]


@pytest.mark.parametrize(("known", "out_s"), [(True, 32.5 - bd.FRAME_GUARD_S), (False, 32.0)])
def test_unknown_section_length_never_extends_the_end(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    cuts: list[float],
    known: bool,
    out_s: float,
) -> None:
    # Without duration_s the file is only known to cover the requested range, so the end
    # may not snap past it; the start can still move (the section starts at 0).
    section = _section(1, 10.0, 40.0)
    if not known:
        del section["duration_s"]
    words = [{"word": "a", "start": 10.5, "end": 11.0}, {"word": "b", "start": 39.0, "end": 39.9}]
    _setup(media, [_highlight(1, 10.0, 40.0)], sections=[section], words=words)
    cuts.extend([1.6, 32.5])  # section time

    rc.render_clips(job, _ctx(media, conn))

    source = fake.calls[0]["edl"].source
    assert (source.in_s, source.out_s) == pytest.approx((1.6, out_s), abs=1e-3)


@pytest.mark.parametrize("detector", ["none found", "real, failing"])
def test_words_still_trim_without_cuts(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detector: str,
) -> None:
    if detector == "real, failing":  # the fake source bytes are no video: the child fails
        monkeypatch.setattr(rc, "detect_cuts", bd.detect_cuts)
    words = [{"word": "a", "start": 12.0, "end": 13.0}, {"word": "b", "start": 30.0, "end": 35.0}]
    _setup(media, [_highlight(1, 10.0, 40.0)], words=words)

    rc.render_clips(job, _ctx(media, conn))

    source = fake.calls[0]["edl"].source
    assert (source.in_s, source.out_s) == pytest.approx((3.75, 27.4))
    (row,) = _rows(conn)
    assert (row["start_s"], row["end_s"]) == pytest.approx((11.75, 35.4))
    failed = any("scene detection failed" in r.getMessage() for r in caplog.records)
    assert failed is (detector == "real, failing")


@pytest.mark.parametrize(
    ("width", "height", "detected"),
    [
        (4096, 2160, True),
        (2160, 3840, True),
        (7680, 4320, False),
        (4097, 2160, False),
        ("x", 1, False),
    ],
)
def test_oversized_section_skips_detection(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
    width: Any,
    height: Any,
    detected: bool,
) -> None:
    section = {**_section(1, 10.0, 40.0), "width": width, "height": height}
    _setup(media, [_highlight(1, 10.0, 40.0)], sections=[section])
    seen: list[Path] = []
    monkeypatch.setattr(rc, "detect_cuts", lambda path, deadline: seen.append(path) or [])

    rc.render_clips(job, _ctx(media, conn))

    assert bool(seen) is detected
    assert len(_rows(conn)) == 1  # the clip renders either way


def test_polished_edges_the_edl_rejects_fall_back_to_the_request(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    monkeypatch.setattr(rc, "_polish", lambda *a, **kw: (2.0, 2.5))  # 0.5 s: under the EDL min

    rc.render_clips(job, _ctx(media, conn))

    source = fake.calls[0]["edl"].source
    assert (source.in_s, source.out_s) == (2.0, 7.0)
    (row,) = _rows(conn)
    assert (row["start_s"], row["end_s"]) == (10.0, 15.0)
    (warning,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "clip 1" in warning and "requested range" in warning


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


def test_transient_failure_does_not_stop_later_clips(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2, 3)])
    fake.fail_on[2] = RenderError("remotion render failed with exit code 1\nstderr...")

    # Retryable: the job tries again (after other clips rendered, without using an attempt).
    with pytest.raises(Deferred, match="exit code 1"):
        rc.render_clips(job, _ctx(media, conn))
    assert [r["rank"] for r in _rows(conn)] == [1, 3]

    rc.render_clips(job, _ctx(media, conn))  # the retry only redoes clip 2
    assert [r["rank"] for r in _rows(conn)] == [1, 2, 3]
    assert [c["out_dir"].name for c in fake.calls] == ["1", "2", "3", "2"]


def test_hook_is_cleaned_and_empty_after_cleaning_means_none(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(
        media,
        [
            _highlight(1, 10.0, 15.0, hook="\u202eWow\u200b it\nworked"),
            _highlight(2, 20.0, 25.0, hook="\u200b\u2066 \u2069"),
        ],
    )
    rc.render_clips(job, _ctx(media, conn))
    hooks = [c["edl"].hook for c in fake.calls]
    assert hooks[0] is not None and hooks[0].text == "Wow it worked"
    assert hooks[1] is None


def test_transient_failure_with_all_others_invalid_stays_retryable(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0, title=5), _highlight(2, 20.0, 25.0)])
    fake.fail_on[1] = RenderError("remotion render timed out after 900s")

    with pytest.raises(RETRIED) as excinfo:
        rc.render_clips(job, _ctx(media, conn))
    assert not isinstance(excinfo.value, PermanentError)
    assert _rows(conn) == []


def test_post_text_is_cleaned(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    clip = _highlight(
        1,
        10.0,
        15.0,
        title="\u202eTi\u200btle",
        caption="Cap\ntion\u3164",
        reason="Re\x00ason",
        hashtags=["#a b", "\u200b", "#c\u2066"],
    )
    _setup(media, [clip])
    rc.render_clips(job, _ctx(media, conn))
    (row,) = _rows(conn)
    assert (row["title"], row["caption"], row["reason"]) == ("Title", "Cap tion", "Re ason")
    assert json.loads(row["hashtags"]) == ["#ab", "#c"]


def test_render_failure_is_retryable(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    fake.fail_on[1] = RenderError("remotion render timed out after 900s")

    with pytest.raises(RETRIED):
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
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0), _highlight(2, 20.0, 25.0)])
    stderr = "many\nlines of\nstderr with token=sk-ant-api03-" + "x" * 40
    fake.fail_on[1] = InvalidClipError("Remotion rejected the clip props: /hook/text: bad", stderr)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [2]
    (summary,) = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert summary.endswith(
        "1 clip(s) left out: clip 1: Remotion rejected the clip props: /hook/text: bad"
    )


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
        {"hook": 7},
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
        {"word": "\u2060", "start": 12.1, "end": 12.5},
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
    # Paid for by the clip's failure count (CLP-65), not by the job's attempts.
    assert after.attempts == 0
    assert after.last_error is not None and "remotion render failed" in after.last_error
    due = conn.execute("SELECT next_attempt_at FROM jobs WHERE id = ?", (job.id,)).fetchone()[0]
    assert due == to_iso(T0 + backoff(1))
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


def _faces(share: float, x: float = 0.3, n: int = 300) -> rf.Faces:
    """Samples every 0.2 s over the section, with a face at x in `share` of them."""
    return rf.Faces(
        1280, 720, [(i / 5, [(x, 0.02)] if i % 10 < share * 10 else []) for i in range(n)]
    )


def test_faces_found_give_a_face_tracked_clip(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    faces: FakeFaces,
    cuts: list[float],
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])  # section time [2, 32]
    faces.result = _faces(1.0)
    cuts.append(12.0)

    rc.render_clips(job, _ctx(media, conn))

    assert faces.calls == [((media / "clips/1/source.mp4").resolve(), Path("model.tflite"))]
    edl = fake.calls[0]["edl"]
    assert edl.layout == "face_track"
    assert edl.crop is not None and edl.crop[0].t_s == 0.0
    assert edl.crop[0].x_center == pytest.approx(rf.crop_path([(0.0, 0.3)], [], 1.0, 0.16)[0][1])
    # The cut is reset in clip time (section 12.0 - in_s).
    assert [k.t_s for k in edl.crop].count(pytest.approx(12.0 - edl.source.in_s)) == 2
    assert len(_rows(conn)) == 1


@pytest.mark.parametrize("share", [0.0, 0.5])
def test_few_faces_keep_blurred_fill(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    faces: FakeFaces,
    share: float,
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])
    faces.result = _faces(share)
    rc.render_clips(job, _ctx(media, conn))
    edl = fake.calls[0]["edl"]
    assert (edl.layout, edl.crop) == ("blurred_fill", None)


@pytest.mark.parametrize("failure", ["model", "detection"])
def test_face_tracking_failure_never_fails_the_clip(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    faces: FakeFaces,
    failure: str,
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])
    if failure == "model":
        faces.model = None  # download failed or checksum mismatch
    faces.result = None if failure == "detection" else _faces(1.0)
    rc.render_clips(job, _ctx(media, conn))
    assert fake.calls[0]["edl"].layout == "blurred_fill"
    assert len(faces.calls) == (failure == "detection")
    assert len(_rows(conn)) == 1


def test_crop_the_edl_rejects_keeps_blurred_fill(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    faces: FakeFaces,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])
    faces.result = _faces(1.0)
    monkeypatch.setattr(rc, "plan_reframe", lambda *a: (rf.Layout.FACE_TRACK, [(0.0, 1.5)]))
    rc.render_clips(job, _ctx(media, conn))
    assert fake.calls[0]["edl"].layout == "blurred_fill"
    assert "face track rejected" in caplog.text


def test_creator_can_opt_out_of_face_tracking(
    conn: sqlite3.Connection, media: Path, fake: FakeRender, faces: FakeFaces
) -> None:
    config = AppConfig(
        creators=(
            Creator(
                name="Static",
                region="global",
                youtube=YouTubeChannel(handle="@static"),
                overrides=CreatorOverrides(reframe="blurred_fill"),
            ),
        ),
    )
    job, _ = enqueue(conn, SourceRef("youtube", "dQw4w9WgXcQ"), T0, creator="Static")
    _setup(media, [_highlight(1, 10.0, 40.0)])
    faces.result = _faces(1.0)
    rc.render_clips(job, StageContext(media_dir=media, conn=conn, config=config))
    assert faces.calls == []
    assert fake.calls[0]["edl"].layout == "blurred_fill"


def test_oversized_section_skips_face_detection(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, faces: FakeFaces
) -> None:
    section = {**_section(1, 10.0, 40.0), "width": 7680, "height": 4320}
    _setup(media, [_highlight(1, 10.0, 40.0)], sections=[section])
    faces.result = _faces(1.0)
    rc.render_clips(job, _ctx(media, conn))
    assert faces.calls == []


def test_scene_cuts_are_detected_once_per_clip(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    faces: FakeFaces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])
    faces.result = _faces(1.0)
    seen: list[Path] = []
    monkeypatch.setattr(rc, "detect_cuts", lambda path, deadline: seen.append(path) or [])
    rc.render_clips(job, _ctx(media, conn))
    assert len(seen) == 1  # the polish and the reframe share one cut list


def test_worst_case_covers_face_tracking() -> None:
    assert rc.CLIP_WORST_CASE_S >= (
        bd.DETECT_TIMEOUT_S
        + rf.MODEL_TIMEOUT_S
        + rf.FACE_TIMEOUT_S
        + rc.DEFAULT_RENDER_TIMEOUT_S
        + 4 * rc.DEFAULT_FFMPEG_TIMEOUT_S
    )
    assert rc.CLIP_WORST_CASE_S < rc.STAGE_BUDGET_S / 2


# --- jump cuts (CLP-73) -----------------------------------------------------------------

F = 1 / 30
GAP_AT = 21
"""Word 21 starts 0.7 s late: a 1.2 s gap after word 20 (source 20.6 -> 21.8)."""


def _gap_words() -> list[dict[str, Any]]:
    return [
        {
            "word": f" w{i}",
            "start": i + 0.1 + 0.7 * (i >= GAP_AT),
            "end": i + 0.6 + 0.7 * (i >= GAP_AT),
        }
        for i in range(60)
    ]


def _mode(mode: str, **settings: Any) -> AppConfig:
    return AppConfig(settings=Settings(jump_cuts=mode, **settings))  # type: ignore[arg-type]


def _render_mode(
    conn: sqlite3.Connection,
    root: Path,
    fake: FakeRender,
    mode: str,
    video: str,
    words: list[dict[str, Any]],
    end: float = 40.0,
    **settings: Any,
) -> EDL:
    media = root / video
    media.mkdir(parents=True)
    job, _ = enqueue(conn, SourceRef("youtube", video), T0, creator="manual")
    _setup(media, [_highlight(1, 10.0, end)], words=words)
    config = _mode(mode, **settings)
    rc.render_clips(job, StageContext(media_dir=media, conn=conn, config=config))
    return fake.calls[-1]["edl"]


def test_silence_inside_the_clip_is_cut_down_to_a_breath(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    """AC: 1.2 s of silence renders shorter by the gap minus the kept breath."""
    silences.append((12.6, 13.8))  # the gap in section time (section starts at 8.0)
    off = _render_mode(conn, tmp_path, fake, "off", "aaaaaaaaaaa", _gap_words())
    cut = _render_mode(conn, tmp_path, fake, "silences", "bbbbbbbbbbb", _gap_words())

    assert off.segments is None and cut.segments is not None and len(cut.segments) == 2
    (applied,) = [c for c in cut.cuts if c.applied]
    assert applied.kind == "silence"
    assert off.duration_s - cut.duration_s == pytest.approx(1.2 - 0.14, abs=F)
    # Whole frames, and the span is the polished range on the frame grid.
    assert cut.source.in_s == pytest.approx(off.source.in_s, abs=F / 2)
    assert cut.source.out_s == pytest.approx(off.source.out_s, abs=F / 2)
    for seg in cut.segments:
        for x in (seg.in_s, seg.out_s):
            assert abs(x * 30 - round(x * 30)) < 1e-6
    # Words stay in section time: the renderer maps them onto the output timeline.
    assert cut.words == off.words
    row = max(_rows(conn), key=lambda r: r["job_id"])  # the silences run
    assert row["start_s"] == pytest.approx(cut.source.in_s + 8.0)
    assert row["end_s"] == pytest.approx(cut.source.out_s + 8.0)


def test_gap_with_game_audio_is_not_cut(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    """AC: silencedetect hears sound in the gap (game audio, laughter): no cut."""
    silences.append((30.0, 31.0))  # silence elsewhere only
    _setup(media, [_highlight(1, 10.0, 40.0)], words=_gap_words())
    rc.render_clips(job, _ctx(media, conn))  # default: silences
    edl = fake.calls[0]["edl"]
    assert edl.segments is None
    assert not [c for c in edl.cuts if c.applied]


def test_jump_cuts_off_is_the_v1_render(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    """AC: `off` gives the v1 output: no silencedetect, no segments, no cuts, and the
    composition props are those of the same clip as a v1 EDL."""
    silences.append((0.0, 1000.0))
    edl = _render_mode(conn, tmp_path, fake, "off", "aaaaaaaaaaa", _gap_words())
    assert (edl.segments, edl.cuts) == (None, ())
    assert silences.calls == []
    v1 = {**edl.to_props(), "schema_version": 1}
    del v1["segments"], v1["cuts"]
    as_v1 = parse_edl(v1)
    props, v1_props = segment_props(edl), segment_props(as_v1)
    assert {**props, "schema_version": 1} == v1_props
    assert edl.duration_in_frames == as_v1.duration_in_frames


def test_taglish_words_are_all_kept(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    """AC: "ano", "yung", "parang" are real words, never cut (even in `full`)."""
    silences.append((0.0, 1000.0))
    text = "ano yung ano ano parang game kasi parang ang hirap nung boss eh grabe " * 4
    words = [
        {"word": f" {t}", "start": 10.0 + i * 0.4, "end": 10.3 + i * 0.4}
        for i, t in enumerate(text.split())
    ]
    edl = _render_mode(conn, tmp_path, fake, "full", "aaaaaaaaaaa", words)
    assert edl.segments is None and not [c for c in edl.cuts if c.applied]
    assert [w.text for w in edl.words] == text.split()


def test_retakes_are_proposed_only(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender
) -> None:
    """AC: a retake is recorded with applied=false and nothing is cut for it."""
    take = ["the", "boss", "fight", "in", "this", "game", "was", "honestly", "brutal"]
    words = [
        {"word": f" {t}", "start": 10.0 + k * 4 + i * 0.3, "end": 10.25 + k * 4 + i * 0.3}
        for k in range(2)
        for i, t in enumerate(take)
    ]
    edl = _render_mode(conn, tmp_path, fake, "full", "aaaaaaaaaaa", words)
    retakes = [c for c in edl.cuts if c.kind in {"retake", "false_start"}]
    assert retakes and all(c.applied is False for c in retakes)
    assert edl.segments is None


def test_full_mode_cuts_stutters_even_when_silencedetect_fails(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    silences.append(None)  # detection failed: no silence cuts (fail open)
    words = _gap_words()
    words[29] = {"word": " again", "start": 29.8, "end": 29.95}  # "again- again": a stutter
    words[30] = {"word": " again", "start": 30.2, "end": 30.5}
    edl = _render_mode(conn, tmp_path, fake, "full", "aaaaaaaaaaa", words)
    assert {c.kind for c in edl.cuts if c.applied} == {"stutter"}
    assert edl.segments is not None and len(edl.segments) == 2


def test_silencedetect_failure_keeps_the_clip_whole(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    silences.append(None)
    _setup(media, [_highlight(1, 10.0, 40.0)], words=_gap_words())
    rc.render_clips(job, _ctx(media, conn))
    assert fake.calls[0]["edl"].segments is None
    assert len(_rows(conn)) == 1


def test_jump_cut_edl_the_validator_rejects_keeps_the_clip_whole(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    silences: FakeSilences,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    silences.append((0.0, 1000.0))
    bad = [(2.0, 2.4), (3.0, 3.4)]  # under the EDL's 1 s minimum
    monkeypatch.setattr(rc, "cut_clip", lambda span, cuts, min_s: (list(cuts), bad))
    _setup(media, [_highlight(1, 10.0, 40.0)], words=_gap_words())
    rc.render_clips(job, _ctx(media, conn))
    assert fake.calls[0]["edl"].segments is None
    assert "jump cuts rejected" in caplog.text


def test_face_track_keys_follow_the_output_timeline(
    conn: sqlite3.Connection,
    tmp_path: Path,
    fake: FakeRender,
    faces: FakeFaces,
    silences: FakeSilences,
) -> None:
    """CLP-58 crop keys are on the output timeline, with a jump key at the join."""
    silences.append((12.6, 13.8))
    faces.result = _faces(1.0)
    edl = _render_mode(conn, tmp_path, fake, "silences", "aaaaaaaaaaa", _gap_words())
    assert edl.layout == "face_track" and edl.segments is not None and edl.crop is not None
    first = edl.segments[0]
    join = first.out_s - first.in_s
    assert [k.t_s for k in edl.crop].count(pytest.approx(join)) >= 2
    assert edl.crop[-1].t_s <= edl.duration_s + 1e-6
    times = [k.t_s for k in edl.crop]
    assert times == sorted(times)


# --- review pass 1 -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["silences", "full"])
def test_words_with_one_start_time_still_render(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, mode: str
) -> None:
    """A Whisper glitch (two phrases starting together) once failed the whole stage."""
    words = [
        {"word": " kamote", "start": 13.0, "end": 13.2},
        {"word": " kamote", "start": 13.0, "end": 38.0},
    ]
    edl = _render_mode(conn, tmp_path, fake, mode, "aaaaaaaaaaa", words)
    assert all(c.out_s > c.in_s for c in edl.cuts)
    assert len(_rows(conn)) == 1


def test_any_jump_cut_error_keeps_the_clip_whole(
    conn: sqlite3.Connection,
    tmp_path: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise ValueError("bad cut")

    monkeypatch.setattr(rc, "plan_cuts", boom)
    edl = _render_mode(conn, tmp_path, fake, "silences", "aaaaaaaaaaa", _gap_words())
    assert (edl.segments, edl.cuts) == (None, ())
    (record,) = [r for r in caplog.records if "jump cuts rejected" in r.getMessage()]
    # A logic bug must not hide behind the fallback: the traceback is logged.
    assert record.levelname == "WARNING" and record.exc_info is not None
    assert len(_rows(conn)) == 1


def test_jump_cuts_keep_the_creator_minimum(
    conn: sqlite3.Connection, tmp_path: Path, fake: FakeRender, silences: FakeSilences
) -> None:
    """A clip near clip_min_seconds keeps enough of its pauses to stay at or above it."""
    silences.append((0.0, 1000.0))
    words = [
        {"word": f" w{k}", "start": 10.0 + 1.5 * k, "end": 10.4 + 1.5 * k} for k in range(14)
    ]  # 1.1 s pauses: about 12.5 s of cuts in a 20 s clip
    edl = _render_mode(
        conn, tmp_path, fake, "silences", "aaaaaaaaaaa", words, end=31.0,
        clip_min_seconds=15, clip_max_seconds=60,
    )  # fmt: skip
    span = edl.source.out_s - edl.source.in_s
    assert edl.segments is not None
    assert 15.0 <= edl.duration_s < span
    applied = [c for c in edl.cuts if c.applied]
    assert 0 < len(applied) < 13
    assert len(edl.segments) == len(applied) + 1


def test_records_prompt_and_playbook_version(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    versions = {"prompt_version": "select_v2", "playbook_version": "playbook_v1"}
    _setup(media, [_highlight(1, 10.0, 15.0)], versions=versions)
    rc.render_clips(job, _ctx(media, conn))
    row = _rows(conn)[0]
    assert (row["prompt_version"], row["playbook_version"]) == ("select_v2", "playbook_v1")


@pytest.mark.parametrize("bad", [{"x": 1}, ["a"], 7, "v" * 200])
def test_malformed_versions_are_not_stored(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, bad: Any
) -> None:
    _setup(
        media,
        [_highlight(1, 10.0, 15.0)],
        versions={"prompt_version": bad, "playbook_version": bad},
    )
    rc.render_clips(job, _ctx(media, conn))
    row = _rows(conn)[0]
    assert row["prompt_version"] is None and row["playbook_version"] is None


def test_sensitive_flag_is_passed_through(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0, sensitive=True), _highlight(2, 30.5, 40.5)])
    rc.render_clips(job, _ctx(media, conn))
    assert [r["sensitive"] for r in _rows(conn)] == [1, 0]


@pytest.mark.parametrize("bad", ["yes", 1, None, ["x"]])
def test_non_boolean_sensitive_is_invalid_metadata(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, bad: Any
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0, sensitive=bad), _highlight(2, 30.5, 40.5)])
    rc.render_clips(job, _ctx(media, conn))
    assert [r["rank"] for r in _rows(conn)] == [2]  # the bad clip is skipped, not stored


# --- quality gates (CLP-74) -------------------------------------------------------------


def test_rendered_clip_is_gated_on_its_video(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, gates: FakeGates
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])
    rc.render_clips(job, _ctx(media, conn))
    (call,) = gates.calls
    assert call["video"] == media / "clips/1/v1.mp4"
    assert call["edl"] == fake.calls[0]["edl"]
    row = _rows(conn)[0]
    assert (row["status"], json.loads(row["gate_reasons"])) == ("pending_review", [])


def test_failed_gate_sends_the_clip_to_needs_attention(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, gates: FakeGates
) -> None:
    gates.reasons = ["word_crosses_join"]
    _setup(media, [_highlight(1, 10.0, 40.0)])
    rc.render_clips(job, _ctx(media, conn))
    row = _rows(conn)[0]
    assert (row["status"], json.loads(row["gate_reasons"])) == (
        "needs_attention",
        ["word_crosses_join"],
    )


def test_gates_get_the_creators_language_and_threads(
    conn: sqlite3.Connection, media: Path, fake: FakeRender, gates: FakeGates
) -> None:
    config = AppConfig(
        settings=Settings(whisper_cpu_threads=2),
        creators=(
            Creator(
                name="Raffy", region="ph", language="tl", youtube=YouTubeChannel(handle="@raffy")
            ),
        ),
    )
    job, _ = enqueue(conn, SourceRef("youtube", "dQw4w9WgXcQ"), T0, creator="Raffy")
    _setup(media, [_highlight(1, 10.0, 40.0)])
    rc.render_clips(job, StageContext(media_dir=media, conn=conn, config=config))
    assert gates.calls[0]["language"] == "tl"
    assert gates.calls[0]["asr"].asr.cpu_threads == 2


def test_worst_case_covers_the_gates() -> None:
    from clipper.pipeline import quality

    assert rc.CLIP_WORST_CASE_S >= quality.GATES_TIMEOUT_S + rc.DEFAULT_RENDER_TIMEOUT_S
    assert rc.CLIP_WORST_CASE_S < rc.STAGE_BUDGET_S / 2


def test_clip_starting_on_a_scene_cut_tells_the_gates(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    gates: FakeGates,
    cuts: list[float],
) -> None:
    _setup(media, [_highlight(1, 10.0, 40.0)])  # section time [2, 32]
    rc.render_clips(job, _ctx(media, conn))
    assert gates.calls[0]["starts_on_cut"] is False
    gates.calls.clear()
    conn.execute("DELETE FROM clip_versions")
    conn.execute("DELETE FROM clips")
    cuts.append(fake.calls[0]["edl"].source.in_s)
    rc.render_clips(job, _ctx(media, conn))
    assert gates.calls[0]["starts_on_cut"] is True


# CLP-65: a clip that keeps failing is left out instead of failing the whole job.

RETRIED = (RenderError, Deferred)
"""A clip failed below the limit: retried, with an attempt (RenderError) or without one
(Deferred, when the run made progress)."""


def _render_worker(conn: sqlite3.Connection, tmp_path: Path, clock: list[datetime]) -> Worker:
    """A real worker over `tmp_path/media`; `clock[0]` is its current time."""
    return Worker(
        conn=conn,
        config=AppConfig(),
        worker_id="w",
        stages=build_stages(),
        media_root=tmp_path / "media",
        clock=lambda: clock[0],
    )


def _worker_media(conn: sqlite3.Connection, job: Job, tmp_path: Path) -> Path:
    conn.execute("UPDATE jobs SET status = 'rendering' WHERE id = ?", (job.id,))
    media = tmp_path / "media" / job.ref.key
    media.mkdir(parents=True)
    return media


def _drain(worker: Worker, conn: sqlite3.Connection, job: Job, clock: list[datetime]) -> Job:
    """Run the worker until the job is terminal, jumping the clock past every backoff."""
    for _ in range(50):
        current = get_job(conn, job.id)
        assert current is not None
        if current.status in (JobStatus.CLIPS_READY, JobStatus.FAILED):
            return current
        clock[0] += timedelta(hours=2)
        worker.run_once()
    raise AssertionError("job never finished")


def test_quarantine_limit_is_below_the_attempt_limit() -> None:
    # A rank must be left out before the job runs out of attempts on it.
    assert 1 < rc.MAX_RENDER_FAILURES < MAX_ATTEMPTS


def test_rank_failing_every_time_is_left_out_after_the_limit(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2, 3)])
    fake.fail_rank[2] = RenderError("remotion render timed out after 900s")

    for _ in range(rc.MAX_RENDER_FAILURES - 1):
        with pytest.raises(RETRIED):  # still retryable below the limit
            rc.render_clips(job, _ctx(media, conn))
    caplog.clear()
    rc.render_clips(job, _ctx(media, conn))  # the last failure leaves clip 2 out

    assert [r["rank"] for r in _rows(conn)] == [1, 3]
    renders = [c["out_dir"].name for c in fake.calls]
    assert renders.count("2") == rc.MAX_RENDER_FAILURES
    assert renders.count("1") == renders.count("3") == 1  # never rendered twice
    summary = [r.getMessage() for r in caplog.records if "left out" in r.getMessage()]
    assert len(summary) == 1
    assert "1 clip(s) left out" in summary[0]
    assert f"clip 2: render failed {rc.MAX_RENDER_FAILURES} times" in summary[0]
    assert "timed out after 900s" in summary[0]


def test_left_out_rank_is_not_rendered_again_on_a_rerun(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[2] = RenderError("ffmpeg failed with exit code 1")
    for _ in range(rc.MAX_RENDER_FAILURES - 1):
        with pytest.raises(RETRIED):
            rc.render_clips(job, _ctx(media, conn))
    rc.render_clips(job, _ctx(media, conn))
    calls = len(fake.calls)
    caplog.clear()

    rc.render_clips(job, _ctx(media, conn))  # e.g. a re-run after a restart

    assert len(fake.calls) == calls and [r["rank"] for r in _rows(conn)] == [1]
    assert any("clip 2: render failed" in r.getMessage() for r in caplog.records)


def test_missing_section_file_counts_as_a_render_failure(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    (media / "clips/2/source.mp4").unlink()  # gone for good

    for _ in range(rc.MAX_RENDER_FAILURES - 1):
        with pytest.raises(RETRIED, match="source media not found"):
            rc.render_clips(job, _ctx(media, conn))
    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [1]


def test_rank_that_recovers_below_the_limit_renders_once(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[2] = RenderError("remotion render failed with exit code 1")
    with pytest.raises(RETRIED):
        rc.render_clips(job, _ctx(media, conn))
    del fake.fail_rank[2]  # transient: fine on the retry

    rc.render_clips(job, _ctx(media, conn))
    rc.render_clips(job, _ctx(media, conn))  # done: a no-op

    assert [r["rank"] for r in _rows(conn)] == [1, 2]
    assert [c["out_dir"].name for c in fake.calls] == ["1", "2", "2"]
    assert not (media / "clips/2" / rc.FAILURES_MARKER).exists()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"failures": True, "error": "x"}),
        json.dumps({"failures": "9", "error": "x"}),
        json.dumps({"failures": 9, "error": None}),
        json.dumps([9]),
        json.dumps({"failures": 9, "error": "x"}) + " " * 4096,  # over 4 KiB
        "[" * 100_000,  # would hit the parser's recursion limit; the size cap stops it first
    ],
    ids=["not-json", "bool", "str", "no-error", "list", "too-large", "too-deep"],
)
def test_unreadable_failure_marker_counts_as_no_failures(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender, content: str
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    (media / "clips/1" / rc.FAILURES_MARKER).write_text(content)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [1]


def test_negative_failure_count_does_not_delay_leaving_out(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    (media / "clips/2" / rc.FAILURES_MARKER).write_text(
        json.dumps({"failures": -100, "error": "x"})
    )
    fake.fail_rank[2] = RenderError("remotion render failed with exit code 1")
    for _ in range(rc.MAX_RENDER_FAILURES - 1):
        with pytest.raises(RETRIED):
            rc.render_clips(job, _ctx(media, conn))

    rc.render_clips(job, _ctx(media, conn))  # counted from 0: left out now

    assert [r["rank"] for r in _rows(conn)] == [1]


def test_marker_nested_too_deep_counts_as_no_failures(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    (media / "clips/1" / rc.FAILURES_MARKER).write_text("[[[[")
    loads = json.loads

    def deep(text: str | bytes, **kw: Any) -> Any:
        if text in ("[[[[", b"[[[["):
            raise RecursionError("maximum recursion depth exceeded")
        return loads(text, **kw)

    monkeypatch.setattr(json, "loads", deep)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [1]


@pytest.mark.parametrize("make", [Path.mkdir, os.mkfifo], ids=["dir", "fifo"])
def test_failure_marker_that_is_not_a_file_counts_as_no_failures(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    make: Callable[[Path], None],
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    make(media / "clips/1" / rc.FAILURES_MARKER)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [1]


def test_symlinked_failure_marker_is_not_followed(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    planted = media / "planted.json"
    planted.write_text(json.dumps({"failures": 9, "error": "x"}))
    (media / "clips/1" / rc.FAILURES_MARKER).symlink_to(planted)

    rc.render_clips(job, _ctx(media, conn))

    assert [r["rank"] for r in _rows(conn)] == [1]


def test_failure_marker_is_not_written_through_a_symlinked_clip_dir(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])
    elsewhere = media / "elsewhere"
    (media / "clips/1").rename(elsewhere)
    (media / "clips/1").symlink_to(elsewhere, target_is_directory=True)
    fake.fail_rank[1] = RenderError("remotion render failed with exit code 1")

    with pytest.raises(RenderError):
        rc.render_clips(job, _ctx(media, conn))

    assert not (elsewhere / rc.FAILURES_MARKER).exists()
    assert any("not counting the failure" in r.getMessage() for r in caplog.records)


def test_stale_marker_on_a_rendered_clip_is_harmless(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A crash between the clip's insert and the marker's removal leaves the marker behind.
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    rc.render_clips(job, _ctx(media, conn))
    marker = media / "clips/1" / rc.FAILURES_MARKER
    marker.write_text(json.dumps({"failures": rc.MAX_RENDER_FAILURES, "error": "x"}))
    caplog.clear()

    rc.render_clips(job, _ctx(media, conn))

    assert len(fake.calls) == 2 and [r["rank"] for r in _rows(conn)] == [1, 2]
    assert not any("left out" in r.getMessage() for r in caplog.records)


def test_render_error_after_progress_defers_with_backoff(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[2] = RenderError("remotion render failed with exit code 1")
    ctx = StageContext(media_dir=media, conn=conn, config=AppConfig(), clock=lambda: T0)

    with pytest.raises(Deferred) as excinfo:
        rc.render_clips(job, ctx)

    assert excinfo.value.until == T0 + backoff(1)
    assert "exit code 1" in excinfo.value.reason
    assert [r["rank"] for r in _rows(conn)] == [1]


def test_worker_render_error_after_progress_does_not_use_an_attempt(
    conn: sqlite3.Connection, job: Job, tmp_path: Path, fake: FakeRender
) -> None:
    media = _worker_media(conn, job, tmp_path)
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[2] = RenderError("remotion render failed with exit code 1")
    clock = [T0]

    assert _render_worker(conn, tmp_path, clock).run_once()

    after = get_job(conn, job.id)
    assert after is not None and after.status is JobStatus.RENDERING
    assert after.attempts == 0
    due = conn.execute("SELECT next_attempt_at FROM jobs WHERE id = ?", (job.id,)).fetchone()[0]
    assert due == to_iso(T0 + backoff(1))


def test_gates_timeout_after_progress_defers(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])

    def check(edl: EDL, video: Path, **kw: Any) -> list[str]:
        if video.parent.name == "2":
            raise StageTimeout("stage exceeded its 100 min budget")
        return []

    monkeypatch.setattr(rc, "check_clip", check)
    ctx = StageContext(media_dir=media, conn=conn, config=AppConfig(), clock=lambda: T0)

    with pytest.raises(Deferred, match="100 min budget"):
        rc.render_clips(job, ctx)
    assert [r["rank"] for r in _rows(conn)] == [1]  # the clip already in stays in


def test_gates_timeout_without_progress_is_charged(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(1, 10.0, 15.0)])

    def check(edl: EDL, video: Path, **kw: Any) -> list[str]:
        raise StageTimeout("stage exceeded its 100 min budget")

    monkeypatch.setattr(rc, "check_clip", check)

    with pytest.raises(StageTimeout):
        rc.render_clips(job, _ctx(media, conn))
    assert _rows(conn) == []


def test_crash_mid_render_does_not_count_as_a_failure(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    # A killed worker is charged by its claim (attempts), not by the rank's count.
    _setup(media, [_highlight(1, 10.0, 15.0)])
    fake.fail_on[1] = Crash()
    with pytest.raises(Crash):
        rc.render_clips(job, _ctx(media, conn))
    assert not (media / "clips/1" / rc.FAILURES_MARKER).exists()


def test_all_ranks_left_out_fails_the_job_visibly(
    conn: sqlite3.Connection, job: Job, media: Path, fake: FakeRender
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[1] = RenderError("remotion render timed out after 900s")
    fake.fail_rank[2] = RenderError("ffmpeg failed with exit code 1")
    for _ in range(rc.MAX_RENDER_FAILURES - 1):
        with pytest.raises(RETRIED):
            rc.render_clips(job, _ctx(media, conn))

    with pytest.raises(PermanentError, match="no clip could be rendered: clip 1: render failed"):
        rc.render_clips(job, _ctx(media, conn))

    assert _rows(conn) == []
    # A requeued job gets fresh tries at these ranks.
    assert not list(media.glob(f"clips/*/{rc.FAILURES_MARKER}"))


def test_worker_takes_a_job_with_a_failing_rank_to_clips_ready(
    conn: sqlite3.Connection, job: Job, tmp_path: Path, fake: FakeRender
) -> None:
    media = _worker_media(conn, job, tmp_path)
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2, 3)])
    fake.fail_rank[2] = RenderError("remotion render timed out after 900s")
    clock = [T0]

    done = _drain(_render_worker(conn, tmp_path, clock), conn, job, clock)

    assert done.status is JobStatus.CLIPS_READY
    assert [(r["rank"], r["status"]) for r in _rows(conn)] == [
        (1, "pending_review"),
        (3, "pending_review"),
    ]


def test_worker_fails_a_job_whose_every_rank_is_left_out(
    conn: sqlite3.Connection, job: Job, tmp_path: Path, fake: FakeRender
) -> None:
    media = _worker_media(conn, job, tmp_path)
    _setup(media, [_highlight(1, 10.0, 15.0)])
    fake.fail_rank[1] = RenderError("remotion render timed out after 900s")
    clock = [T0]

    done = _drain(_render_worker(conn, tmp_path, clock), conn, job, clock)

    assert done.status is JobStatus.FAILED
    assert done.last_error is not None and "no clip could be rendered" in done.last_error
    assert _rows(conn) == []


def _budget_for_one_clip(monkeypatch: pytest.MonkeyPatch, fake: FakeRender) -> None:
    """Each run's budget fits one clip: every render uses up 20 s of a fresh deadline."""
    runs: list[list[float]] = []

    def deadline() -> Deadline:
        now = [0.0]
        runs.append(now)
        return Deadline(budget_s=rc.CLIP_WORST_CASE_S + 10, clock=lambda: now[0])

    def spend() -> None:
        runs[-1][0] += 20

    monkeypatch.setattr(rc, "Deadline", deadline)
    fake.after.append(spend)


def test_timeout_after_progress_defers_instead_of_failing(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    _budget_for_one_clip(monkeypatch, fake)
    ctx = StageContext(media_dir=media, conn=conn, config=AppConfig(), clock=lambda: T0)

    with pytest.raises(Deferred) as excinfo:
        rc.render_clips(job, ctx)

    assert excinfo.value.until == T0  # due again right away
    assert "1 clip(s) rendered" in excinfo.value.reason
    assert [r["rank"] for r in _rows(conn)] == [1]


def test_worker_timeout_after_progress_does_not_use_an_attempt(
    conn: sqlite3.Connection,
    job: Job,
    tmp_path: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = _worker_media(conn, job, tmp_path)
    ranks = range(1, MAX_ATTEMPTS + 3)  # more runs than the job has attempts
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in ranks])
    _budget_for_one_clip(monkeypatch, fake)
    clock = [T0]
    worker = _render_worker(conn, tmp_path, clock)

    assert worker.run_once()
    after = get_job(conn, job.id)
    assert after is not None and after.status is JobStatus.RENDERING
    assert after.attempts == 0
    due = conn.execute("SELECT next_attempt_at FROM jobs WHERE id = ?", (job.id,)).fetchone()[0]
    assert due == to_iso(T0)  # due again right away

    done = _drain(worker, conn, job, clock)
    assert done.status is JobStatus.CLIPS_READY
    assert [r["rank"] for r in _rows(conn)] == list(ranks)


def test_worker_timeout_without_progress_still_uses_an_attempt(
    conn: sqlite3.Connection,
    job: Job,
    tmp_path: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = _worker_media(conn, job, tmp_path)
    _setup(media, [_highlight(1, 10.0, 15.0)])
    monkeypatch.setattr(rc, "Deadline", lambda: Deadline(budget_s=rc.CLIP_WORST_CASE_S - 1))
    clock = [T0]
    worker = _render_worker(conn, tmp_path, clock)

    assert worker.run_once()
    after = get_job(conn, job.id)
    assert after is not None and after.status is JobStatus.RENDERING
    assert after.attempts == 1
    assert after.last_error is not None and "StageTimeout" in after.last_error

    done = _drain(worker, conn, job, clock)  # never any progress: it cannot loop forever
    assert done.status is JobStatus.FAILED
    assert fake.calls == []


def test_timeout_after_a_counted_failure_defers(
    conn: sqlite3.Connection,
    job: Job,
    media: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in (1, 2)])
    fake.fail_rank[1] = RenderError("remotion render failed with exit code 1")
    now = [0.0]
    monkeypatch.setattr(
        rc, "Deadline", lambda: Deadline(budget_s=rc.CLIP_WORST_CASE_S + 10, clock=lambda: now[0])
    )

    def slow(*args: Any, **kw: Any) -> RenderResult:
        now[0] += 20
        return fake(*args, **kw)

    monkeypatch.setattr(rc, "render_clip", slow)

    # The failure was counted (bounded per clip), so no attempt is used.
    with pytest.raises(Deferred, match="1 failure\\(s\\) counted"):
        rc.render_clips(job, _ctx(media, conn))
    assert _rows(conn) == []


def test_failure_that_cannot_be_counted_uses_an_attempt(
    conn: sqlite3.Connection, job: Job, tmp_path: Path, fake: FakeRender
) -> None:
    media = _worker_media(conn, job, tmp_path)
    _setup(media, [_highlight(1, 10.0, 15.0)])
    elsewhere = media / "elsewhere"
    (media / "clips/1").rename(elsewhere)
    (media / "clips/1").symlink_to(elsewhere, target_is_directory=True)
    fake.fail_rank[1] = RenderError("remotion render failed with exit code 1")
    clock = [T0]

    done = _drain(_render_worker(conn, tmp_path, clock), conn, job, clock)

    assert done.status is JobStatus.FAILED  # bounded by the job's attempts instead
    assert len(fake.calls) == MAX_ATTEMPTS


@pytest.mark.parametrize("bad", [MAX_ATTEMPTS + 1, 3 * MAX_ATTEMPTS])
def test_worker_failing_ranks_in_front_of_good_ones_still_reach_clips_ready(
    conn: sqlite3.Connection,
    job: Job,
    tmp_path: Path,
    fake: FakeRender,
    monkeypatch: pytest.MonkeyPatch,
    bad: int,
) -> None:
    # Like Remotion timing out: every render (good or bad) takes 20 s of a budget that
    # fits three per run, and the bad ranks come first in sections.json.
    media = _worker_media(conn, job, tmp_path)
    ranks = range(1, bad + 3)
    _setup(media, [_highlight(r, 10.0 * r, 10.0 * r + 5) for r in ranks])
    for rank in range(1, bad + 1):
        fake.fail_rank[rank] = RenderError("remotion render timed out after 900s")
    runs: list[list[float]] = []

    def deadline() -> Deadline:
        now = [0.0]
        runs.append(now)
        return Deadline(budget_s=rc.CLIP_WORST_CASE_S + 41, clock=lambda: now[0])

    def slow(*args: Any, **kw: Any) -> RenderResult:
        runs[-1][0] += 20
        return fake(*args, **kw)

    monkeypatch.setattr(rc, "Deadline", deadline)
    monkeypatch.setattr(rc, "render_clip", slow)
    clock = [T0]

    done = _drain(_render_worker(conn, tmp_path, clock), conn, job, clock)

    assert done.status is JobStatus.CLIPS_READY
    assert [r["rank"] for r in _rows(conn)] == [bad + 1, bad + 2]
