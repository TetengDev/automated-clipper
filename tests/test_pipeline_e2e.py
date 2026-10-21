"""End-to-end: a job walks detected -> fetching_audio -> transcribing -> selecting ->
fetching_clips -> rendering through a real `Worker` with `build_stages()`.

Only the outside world is faked, at its boundary:
- yt-dlp: `clipper.pipeline.ytdlp.run_command` is replaced by a runner that answers yt-dlp
  calls by rendering real small media with ffmpeg (lavfi); ffmpeg/ffprobe calls run for real.
- Groq: HTTP mocked with respx. Anthropic: HTTP mocked with an httpx2 MockTransport.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx

from clipper.config import AppConfig, Settings
from clipper.db import connect, migrate
from clipper.jobs import Job, JobStatus, Worker, enqueue, get_job
from clipper.jobs import worker as worker_mod
from clipper.pipeline import build_stages, select, ytdlp
from clipper.pipeline import transcribe as tr
from clipper.urls import SourceRef, canonical_url

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
YT = SourceRef("youtube", "dQw4w9WgXcQ")
VIDEO_S = 60
REAL_RUN = ytdlp.run_command
CLIP_KEYS = {"rank", "start_s", "end_s", "hook", "title", "caption", "hashtags", "score", "reason"}
SECTION_KEYS = {
    "rank",
    "path",
    "requested_start_s",
    "requested_end_s",
    "pad_start_s",
    "duration_s",
    "width",
    "height",
}


class Crash(BaseException):
    """Stands in for the worker process being killed."""


def _ffmpeg(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def _out(args: Sequence[str], ext: str) -> Path:
    return Path(args[list(args).index("-o") + 1].replace("%(ext)s", ext))


class FakeYtdlpWorld:
    """yt-dlp answered locally; every other command runs for real."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.crash_on_section: int | None = None

    def kind(self, args: Sequence[str]) -> str:
        if "--dump-single-json" in args:
            return "probe"
        return "section" if "--download-sections" in args else "audio"

    def of(self, kind: str) -> list[list[str]]:
        return [c for c in self.calls if self.kind(c) == kind]

    def __call__(self, args: Sequence[str], timeout_s: float, **kw: Any) -> ytdlp.RunResult:
        if "yt_dlp" not in args:
            return REAL_RUN(args, timeout_s, **kw)
        args = list(args)
        assert args[-2:] == ["--", canonical_url(YT)]
        self.calls.append(args)
        kind = self.kind(args)
        if kind == "probe":
            info = {
                "id": YT.item_id,
                "title": "E2E video",
                "duration": VIDEO_S,
                "uploader": "Someone",
                "upload_date": "20261020",
                "live_status": "not_live",
                "heatmap": [{"start_time": 10.0, "end_time": 20.0, "value": 1.0}],
            }
            return ytdlp.RunResult(0, json.dumps(info), "")
        if kind == "audio":
            _ffmpeg(
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={VIDEO_S}",
                "-c:a", "aac", str(_out(args, "m4a")),
            )  # fmt: skip
            return ytdlp.RunResult(0, "", "")
        if self.crash_on_section == len(self.of("section")):
            self.crash_on_section = None
            raise Crash
        spec = args[args.index("--download-sections") + 1]
        start, end = (float(x) for x in spec.lstrip("*").split("-"))
        seconds = f"{end - start:.3f}"
        _ffmpeg(
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(_out(args, "mp4")),
        )  # fmt: skip
        return ytdlp.RunResult(0, "", "")


def _groq_body() -> dict[str, Any]:
    words = [{"word": f" w{i}", "start": i + 0.1, "end": i + 0.6} for i in range(VIDEO_S - 1)]
    segments = [
        {"start": s + 0.1, "end": s + 9.6, "text": " ".join(f"w{i}" for i in range(s, s + 10))}
        for s in range(0, VIDEO_S, 10)
    ]
    return {"language": "English", "duration": VIDEO_S, "segments": segments, "words": words}


def _clip(start: float, end: float, score: int) -> dict[str, Any]:
    return {
        "start_s": start,
        "end_s": end,
        "hook": f"Hook {start}",
        "title": f"Title {start}",
        "caption": "Caption",
        "hashtags": ["#clip"],
        "score": score,
        "reason": "Reason",
    }


class FakeAnthropic:
    def __init__(self) -> None:
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        text = json.dumps({"clips": [_clip(5.1, 30.6, 80), _clip(35.1, 58.6, 70)]})
        body = {
            "id": "msg_e2e",
            "type": "message",
            "role": "assistant",
            "model": select.MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        return httpx2.Response(200, json=body)

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key="sk-ant-test-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeYtdlpWorld, FakeAnthropic, Any]]:
    yt = FakeYtdlpWorld()
    claude = FakeAnthropic()
    monkeypatch.setattr(ytdlp, "run_command", yt)
    monkeypatch.setattr(select, "_make_client", claude.client)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_e2e_key_1234567890")
    with respx.mock(assert_all_called=False) as mock:
        groq = mock.post(tr.GROQ_URL).mock(return_value=httpx.Response(200, json=_groq_body()))
        yield yt, claude, groq


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


def _worker(conn: sqlite3.Connection, media_root: Path) -> Worker:
    config = AppConfig(settings=Settings(min_duration_seconds=10))
    return Worker(
        conn=conn,
        config=config,
        worker_id="e2e",
        stages=build_stages(),
        media_root=media_root,
        clock=lambda: T0,
    )


def _drain(worker: Worker, limit: int = 20) -> list[JobStatus]:
    seen: list[JobStatus] = []
    for _ in range(limit):
        if not worker.run_once():
            return seen
        job = get_job(worker.conn, 1)
        assert job is not None
        seen.append(job.status)
    raise AssertionError("worker never went idle")


def _job(conn: sqlite3.Connection) -> Job:
    job = get_job(conn, 1)
    assert job is not None
    return job


def _assert_artifacts(media: Path) -> None:
    probe = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(media / "audio_16k.flac"),
        ],  # fmt: skip
        check=True,
        capture_output=True,
        text=True,
    )
    (stream,) = json.loads(probe.stdout)["streams"]
    assert (stream["codec_name"], stream["sample_rate"], stream["channels"]) == ("flac", "16000", 1)

    meta = json.loads((media / "meta.json").read_text())
    assert meta == {
        "title": "E2E video",
        "duration_s": VIDEO_S,
        "uploader": "Someone",
        "upload_date": "2026-10-20",
        "is_live": False,
        "webpage_url": canonical_url(YT),
    }
    signals = json.loads((media / "signals.json").read_text())
    assert set(signals) == {"heatmap", "loudness_peaks"}
    assert signals["heatmap"] == [{"start_s": 10.0, "end_s": 20.0, "value": 1.0}]
    assert signals["loudness_peaks"] and set(signals["loudness_peaks"][0]) == {"t_s", "lufs"}

    transcript = json.loads((media / "transcript.json").read_text())
    assert set(transcript) == {"language", "duration_s", "model", "segments", "words"}
    assert transcript["language"] == "en" and transcript["model"] == tr.GROQ_MODEL
    assert transcript["duration_s"] == pytest.approx(VIDEO_S, abs=0.5)
    assert len(transcript["words"]) == VIDEO_S - 1

    highlights = json.loads((media / "highlights.json").read_text())
    assert (highlights["model"], highlights["prompt_version"]) == (
        select.MODEL,
        select.PROMPT_VERSION,
    )
    clips = highlights["clips"]
    assert [c["rank"] for c in clips] == [1, 2]
    assert all(set(c) == CLIP_KEYS for c in clips)

    sections = json.loads((media / "sections.json").read_text())["clips"]
    assert [s["rank"] for s in sections] == [1, 2]
    for section, clip in zip(sections, clips, strict=True):
        assert set(section) == SECTION_KEYS
        assert (section["requested_start_s"], section["requested_end_s"]) == (
            clip["start_s"],
            clip["end_s"],
        )
        assert (section["width"], section["height"]) == (320, 180)
        assert (media / section["path"]).is_file()
        length = clip["end_s"] - clip["start_s"]
        assert section["pad_start_s"] + length <= section["duration_s"] + 0.5
    assert not [p for p in media.rglob("*") if ".tmp" in p.name or p.name.startswith(".src")]


def _spend(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    rows = conn.execute("SELECT kind, provider, job_id, usd FROM spend ORDER BY id").fetchall()
    assert all(row["usd"] > 0 for row in rows)
    return [(row["kind"], row["provider"], row["job_id"]) for row in rows]


def test_job_walks_the_pipeline_to_rendering(
    conn: sqlite3.Connection, tmp_path: Path, world: tuple[FakeYtdlpWorld, FakeAnthropic, Any]
) -> None:
    yt, claude, groq = world
    enqueue(conn, YT, T0, creator="manual")
    worker = _worker(conn, tmp_path / "media")

    statuses = _drain(worker)

    assert statuses == [
        JobStatus.FETCHING_AUDIO,
        JobStatus.TRANSCRIBING,
        JobStatus.SELECTING,
        JobStatus.FETCHING_CLIPS,
        JobStatus.RENDERING,
    ]
    job = _job(conn)
    assert job.status is JobStatus.RENDERING and job.last_error is None
    _assert_artifacts(tmp_path / "media" / YT.key)
    assert _spend(conn) == [("transcribe", "groq", 1), ("llm", "anthropic", 1)]
    assert (len(yt.of("probe")), len(yt.of("audio")), len(yt.of("section"))) == (1, 1, 2)
    assert (groq.call_count, claude.requests) == (1, 1)


def test_crashes_resume_without_redoing_completed_work(
    conn: sqlite3.Connection,
    tmp_path: Path,
    world: tuple[FakeYtdlpWorld, FakeAnthropic, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt, claude, groq = world
    enqueue(conn, YT, T0, creator="manual")
    worker = _worker(conn, tmp_path / "media")
    media = tmp_path / "media" / YT.key

    # 1) Killed after transcript.json is written but before the status update.
    real_advance = worker_mod.advance

    def crash_after_transcribing(conn: sqlite3.Connection, job: Job, *a: Any, **kw: Any) -> Any:
        if job.status is JobStatus.TRANSCRIBING:
            monkeypatch.setattr(worker_mod, "advance", real_advance)
            raise Crash
        return real_advance(conn, job, *a, **kw)

    monkeypatch.setattr(worker_mod, "advance", crash_after_transcribing)
    with pytest.raises(Crash):
        _drain(worker)
    assert _job(conn).status is JobStatus.TRANSCRIBING
    assert (media / "transcript.json").is_file()

    # 2) Killed in the middle of fetching_clips, while downloading the second section.
    yt.crash_on_section = 2
    with pytest.raises(Crash):
        _drain(worker)
    assert _job(conn).status is JobStatus.FETCHING_CLIPS
    assert (media / "clips/1/source.mp4").is_file()
    assert not (media / "sections.json").exists()

    _drain(worker)

    assert _job(conn).status is JobStatus.RENDERING
    _assert_artifacts(media)
    # Transcription ran once although its stage was re-entered: the artifact was reused.
    assert groq.call_count == 1
    assert claude.requests == 1
    assert (len(yt.of("probe")), len(yt.of("audio"))) == (1, 1)
    # Clip 1 survived the crash; only clip 2 was downloaded again.
    targets = [c[c.index("-o") + 1] for c in yt.of("section")]
    assert ["/clips/1/" in t for t in targets] == [True, False, False]
    assert _spend(conn) == [("transcribe", "groq", 1), ("llm", "anthropic", 1)]
