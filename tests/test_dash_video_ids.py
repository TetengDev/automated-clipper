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


def test_review_media_and_export_paths_need_no_argv(ref: SourceRef) -> None:
    """Export runs rclone on a staging dir named after the clip (never the id); the
    review app serves files by clip id and links the canonical URL."""
    from clipper.clips import source_url_at
    from clipper.export import rclone_args

    args = rclone_args("remote:", Path("/abs/stage/creator_title-1_v1.x"), "2026-10-23")
    assert args[:3] == ["rclone", "copy", "--"]
    assert source_url_at("youtube", ref.item_id, 75) == f"{canonical_url(ref)}&t=75s"


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
