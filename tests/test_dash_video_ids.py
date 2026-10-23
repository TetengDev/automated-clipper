"""CLP-84: a YouTube id may start with "-"; it must never reach an argv as an option.

The id may appear in a subprocess argument only inside the canonical URL (after "--") or
inside a path whose component is the `<platform>_<id>` media dir name. Media dirs are
relative here (cwd = tmp dir) to prove that relative paths are safe too. Every runner is a
fake that records argv; no real binaries run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from clipper import render
from clipper.config import AppConfig
from clipper.jobs import Job, JobStatus, StageContext
from clipper.pipeline import boundaries, common, jumpcuts, quality, reframe, transcribe
from clipper.pipeline.common import Deadline
from clipper.pipeline.fetch_audio import fetch_audio
from clipper.pipeline.fetch_clips import fetch_clips
from clipper.pipeline.ytdlp import RunResult, YtDlp
from clipper.urls import SourceRef, canonical_url
from test_render import EDL_DATA
from test_render import FakeRunner as RenderRunner

DASH_IDS = ["-4mo8fGTST0", "-HgaXKz0rtg", "--exec_cmd0", "-----------"]

EBUR128 = "[Parsed_ebur128_0 @ 0x1] t: 10.0 TARGET:-23 LUFS    M: -20.0 S: -21.0  I: -22.0 LUFS"


@pytest.fixture(params=DASH_IDS)
def ref(request: pytest.FixtureRequest) -> SourceRef:
    return SourceRef("youtube", request.param)


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: SourceRef) -> Path:
    """The job's media dir, relative to the cwd, as with `--media-dir .`."""
    monkeypatch.chdir(tmp_path)
    path = Path(ref.key)
    path.mkdir()
    return path


def assert_id_is_never_an_option(argvs: Sequence[Sequence[str]], ref: SourceRef) -> None:
    assert argvs, "nothing was run"
    url = canonical_url(ref)
    for argv in argvs:
        for i, arg in enumerate(argv):
            assert not arg.startswith(ref.item_id), (arg, argv)
            if ref.item_id not in arg:
                continue
            if arg == url:
                assert "--" in argv[:i], argv  # option parsing ended before the URL
            else:
                assert f"youtube_{ref.item_id}" in arg, (arg, argv)


def job(ref: SourceRef, status: JobStatus) -> Job:
    return Job(
        id=1,
        ref=ref,
        creator="MrBeast",
        source_url=canonical_url(ref),
        status=status,
        is_stream=False,
        attempts=1,
    )


class Recorder:
    """yt-dlp, ffmpeg and ffprobe stand-ins that produce what the stages expect."""

    def __init__(self, ref: SourceRef) -> None:
        self.ref = ref
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout_s: float, **_: Any) -> RunResult:
        argv = list(args)
        self.calls.append(argv)
        if "yt_dlp" in argv:
            if "--dump-single-json" in argv:
                info = {"id": self.ref.item_id, "title": "t", "duration": 900}
                return RunResult(0, json.dumps({**info, "entries": []}), "")
            out = Path(argv[argv.index("-o") + 1].replace("%(ext)s", "mp4"))
            out.write_bytes(b"media")
            return RunResult(0, "", "")
        if "clipper.pipeline.local_asr" in argv:
            words = [{"word": "hi", "start": 1.0, "end": 1.5}]
            body = {"language": "en", "words": words, "segments": [], "dropped": {}}
            body["segments"] = [{"start": 1.0, "end": 1.5, "text": "hi"}]
            return RunResult(0, json.dumps(body), "")
        if argv[0] == "ffprobe":
            probe = {
                "format": {"duration": "900"},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
            }
            return RunResult(0, json.dumps(probe) if "json" in argv else "900\n", "")
        if "null" in argv:
            return RunResult(0, "", EBUR128)
        Path(argv[-1]).write_bytes(b"out")
        return RunResult(0, "", "")


# --- yt-dlp ---------------------------------------------------------------------------


def test_ytdlp_probe_listing_and_downloads(ref: SourceRef, media: Path) -> None:
    rec = Recorder(ref)
    ytdlp = YtDlp(runner=rec)
    url = canonical_url(ref)

    ytdlp.probe(url)
    ytdlp.listing(url, "0")
    ytdlp.download_audio(url, media, "source")
    ytdlp.download_section(url, media, "section", (1.0, 2.0), format="b")

    assert_id_is_never_an_option(rec.calls, ref)
    assert all(argv[-2:] == ["--", url] for argv in rec.calls)


# --- stages ---------------------------------------------------------------------------


@pytest.fixture
def ctx(media: Path) -> Iterator[StageContext]:
    with closing(sqlite3.connect(":memory:")) as conn:
        yield StageContext(media, conn, AppConfig())


def test_fetch_audio_stage(ref: SourceRef, media: Path, ctx: StageContext) -> None:
    rec = Recorder(ref)

    fetch_audio(job(ref, JobStatus.FETCHING_AUDIO), ctx, runner=rec, env={})

    kinds = {("yt-dlp" if "yt_dlp" in c else c[0]) for c in rec.calls}
    assert kinds == {"yt-dlp", "ffmpeg"}
    assert_id_is_never_an_option(rec.calls, ref)
    assert (media / "audio_16k.flac").is_file()


def test_fetch_clips_stage(ref: SourceRef, media: Path, ctx: StageContext) -> None:
    clip = {"rank": 1, "start_s": 10.0, "end_s": 40.0, "hook": "h", "title": "t"}
    clip |= {"caption": "c", "hashtags": ["#x"], "score": 90, "reason": "r"}
    (media / "highlights.json").write_text(json.dumps({"clips": [clip]}))
    (media / "meta.json").write_text(json.dumps({"title": "x", "duration_s": 900}))
    rec = Recorder(ref)

    fetch_clips(job(ref, JobStatus.FETCHING_CLIPS), ctx, runner=rec, env={})

    kinds = {("yt-dlp" if "yt_dlp" in c else c[0]) for c in rec.calls}
    assert kinds == {"yt-dlp", "ffprobe"}
    assert_id_is_never_an_option(rec.calls, ref)
    assert (media / "clips" / "1" / "source.mp4").is_file()


def test_render_invocation(ref: SourceRef, media: Path, tmp_path: Path) -> None:
    (media / "clips" / "1").mkdir(parents=True)
    (media / "clips" / "1" / "source.mp4").write_bytes(b"src")
    render_dir = tmp_path / "render"
    (render_dir / "src").mkdir(parents=True)
    (render_dir / "src" / "index.ts").write_text("// entry")
    cli = render_dir / render.REMOTION_CLI
    cli.parent.mkdir(parents=True)
    cli.write_text("// cli")
    edl = {**EDL_DATA, "source": {**EDL_DATA["source"], "path": "clips/1/source.mp4"}}
    runner = RenderRunner()

    render.render_clip(
        edl, media, media / "clips" / "1", runner=runner, render_dir=render_dir, basename="v1"
    )

    argvs = [call["argv"] for call in runner.calls]
    assert [RenderRunner.step(a) for a in argvs] == [
        "precut",
        "remotion",
        "measure",
        "finalize",
        "thumb",
    ]
    assert_id_is_never_an_option(argvs, ref)


# --- the other media children ----------------------------------------------------------


def test_scene_face_and_silence_detection(ref: SourceRef, media: Path) -> None:
    section = media / "clips" / "1" / "source.mp4"
    section.parent.mkdir(parents=True)
    section.write_bytes(b"video")
    calls: list[list[str]] = []

    def runner(args: Sequence[str], timeout_s: float, **_: Any) -> RunResult:
        calls.append(list(args))
        return RunResult(1, "", "no")

    deadline = Deadline()
    boundaries.detect_cuts(section, deadline, runner=runner)
    reframe.detect_faces(section, media / "model.tflite", deadline, runner=runner)
    jumpcuts.detect_silences(section, deadline, runner=runner)

    assert len(calls) == 3
    assert_id_is_never_an_option(calls, ref)


def test_quality_gate_and_transcribe_cuts(
    ref: SourceRef, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(ref)
    monkeypatch.setattr(common, "default_runner", lambda: rec)
    video = media / "clips" / "1" / "v1.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    audio = media / "audio_16k.flac"
    audio.write_bytes(b"flac")

    with pytest.raises(common.MediaError):  # the fake log has no ebur128 summary
        quality.measure_loudness(video, 10)
    chunk = transcribe.Chunk(index=0, offset=0.0, duration=30.0)
    transcribe.cut_chunk(audio, chunk, media)

    assert len(rec.calls) == 2
    assert_id_is_never_an_option(rec.calls, ref)


def test_ffprobe_duration(ref: SourceRef, media: Path) -> None:
    audio = media / "audio_16k.flac"
    audio.write_bytes(b"flac")
    rec = Recorder(ref)

    assert common.ffprobe_duration(audio, runner=rec) == 900.0

    assert_id_is_never_an_option(rec.calls, ref)


@pytest.fixture
def local_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: SourceRef
) -> tuple[transcribe.FasterWhisperTranscriber, Recorder]:
    """The local whisper backend with its model check skipped; every child (ffmpeg,
    ffprobe, the ASR child) goes to one recorder."""
    rec = Recorder(ref)
    monkeypatch.setattr(common, "default_runner", lambda: rec)
    monkeypatch.setattr(transcribe, "default_runner", lambda: rec)
    monkeypatch.setattr(
        transcribe.FasterWhisperTranscriber,
        "ensure_model",
        lambda self, timeout_s=0: self.snapshot_dir,
    )
    return transcribe.FasterWhisperTranscriber(models_dir=tmp_path / "models"), rec


def test_quality_join_check_audio_and_asr_child(
    ref: SourceRef, media: Path, local_asr: tuple[transcribe.FasterWhisperTranscriber, Recorder]
) -> None:
    asr, rec = local_asr
    video = media / "clips" / "1" / "v1.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")

    heard = quality._hear(video, None, asr, Deadline())

    assert heard == [("hi", 1.0, 1.5)]
    assert [c[0] for c in rec.calls] == ["ffmpeg", rec.calls[1][0]]  # extract, then ASR
    assert "clipper.pipeline.local_asr" in rec.calls[1]
    assert_id_is_never_an_option(rec.calls, ref)


def test_transcribe_stage(
    ref: SourceRef,
    media: Path,
    tmp_path: Path,
    local_asr: tuple[transcribe.FasterWhisperTranscriber, Recorder],
) -> None:
    from datetime import UTC, datetime

    from clipper.db import connect, migrate
    from clipper.jobs import enqueue

    asr, rec = local_asr
    (media / "audio_16k.flac").write_bytes(b"flac")
    with closing(connect(tmp_path / "state.db")) as conn:
        migrate(conn)
        queued, _ = enqueue(conn, ref, datetime(2026, 1, 1, tzinfo=UTC), creator="MrBeast")
        ctx = StageContext(media, conn, AppConfig())
        transcribe.transcribe(queued, ctx, transcriber=asr)

    kinds = [("asr" if "clipper.pipeline.local_asr" in c else c[0]) for c in rec.calls]
    assert kinds[0] == "ffprobe" and {"ffmpeg", "asr"} <= set(kinds)
    assert_id_is_never_an_option(rec.calls, ref)
    assert (media / "transcript.json").is_file()


def test_select_stage_claude_cli(
    ref: SourceRef, media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipper.db import connect, migrate
    from clipper.jobs import enqueue
    from clipper.pipeline import select
    from test_select import T0, make_ctx, write_transcript
    from test_select_cli import FakeCli, cli_json

    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    write_transcript(media)
    meta = {"title": "t", "duration_s": 600, "uploader": "MrBeast", "is_live": False}
    (media / "meta.json").write_text(json.dumps(meta | {"webpage_url": canonical_url(ref)}))
    fake = FakeCli(cli_json())
    with closing(connect(tmp_path / "state.db")) as conn:
        migrate(conn)
        queued, _ = enqueue(conn, ref, T0, creator="MrBeast")
        select.select_highlights(queued, make_ctx(conn, media, selector="claude_cli"), runner=fake)

    (call,) = fake.calls
    assert not any(ref.item_id in arg for arg in call["args"])
    assert_id_is_never_an_option([[*call["args"], "--", canonical_url(ref)]], ref)
    cwd = Path(call["cwd"])
    assert cwd.is_absolute() and ref.item_id not in str(cwd)


def test_export_rclone_argv(ref: SourceRef, tmp_path: Path) -> None:
    """The real export path: package staged, then rclone, for a clip of a dash-id job."""
    from review_support import make_env
    from test_export import approved_clip
    from test_export import run as export

    env = make_env(tmp_path)
    try:
        clip_id = approved_clip(
            env, creator="MrBeast", item_id=ref.item_id, video=f"{ref.key}/clips/1/v1.mp4"
        )
        result = export(env, clip_id, rclone_remote="gdrive:clips")
    finally:
        env.conn.close()

    assert result.status == "exported"
    (args,) = env.runner.calls
    assert args[:3] == ["rclone", "copy", "--"]
    assert Path(args[3]).is_absolute()
    assert not any(ref.item_id in arg for arg in args)  # staged by clip, never by id
    assert_id_is_never_an_option(env.runner.calls, ref)
    assert all(ref.item_id not in p.name for p in result.files)


def test_review_source_link_is_the_canonical_url(ref: SourceRef) -> None:
    from clipper.clips import source_url_at

    assert source_url_at("youtube", ref.item_id, 75) == f"{canonical_url(ref)}&t=75s"


# --- invariant guard ---------------------------------------------------------------------

SRC = Path(__file__).resolve().parents[1] / "src" / "clipper"
ITEM_ID_ALLOWLIST: dict[str, int] = {}
"""`relative path -> number of item_id uses` reviewed as safe (logging, SQL,
canonical_url). Empty: the pipeline and the renderer only use `ref.key` and
`canonical_url(ref)`."""


def test_pipeline_and_render_never_touch_item_id() -> None:
    """A new `.item_id` in an argv/path-building module must go through review (CLP-84)."""
    import ast

    files = [*sorted((SRC / "pipeline").rglob("*.py")), SRC / "render.py"]
    uses: dict[str, int] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = sum(
            (isinstance(n, ast.Attribute) and n.attr == "item_id")
            or (isinstance(n, ast.Name) and n.id == "item_id")
            or (isinstance(n, ast.keyword) and n.arg == "item_id")
            for n in ast.walk(tree)
        )
        if count:
            uses[str(path.relative_to(SRC))] = count
    assert len(files) > 10
    assert uses == ITEM_ID_ALLOWLIST


@pytest.mark.parametrize(
    "argv",
    [
        ["ffmpeg", "-i", "{id}.flac"],  # a bare relative file name is read as an option
        ["ffmpeg", "-i", "x", "{id}"],
        ["yt-dlp", "{url}"],  # URL without "--" before it
        ["node", "cli.js", "--props=/tmp/{id}/props.json"],  # id outside the media dir
    ],
)
def test_the_check_catches_unsafe_argv(argv: list[str]) -> None:
    ref = SourceRef("youtube", DASH_IDS[0])
    unsafe = [a.format(id=ref.item_id, url=canonical_url(ref)) for a in argv]
    with pytest.raises(AssertionError):
        assert_id_is_never_an_option([unsafe], ref)
