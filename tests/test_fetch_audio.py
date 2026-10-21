import dataclasses
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from clipper.config import AppConfig, Creator, CreatorOverrides, Settings, YouTubeChannel
from clipper.db import connect, migrate
from clipper.jobs import (
    Deferred,
    Job,
    JobStatus,
    PermanentError,
    SkipJob,
    StageContext,
    StageDef,
    Worker,
    enqueue,
)
from clipper.pipeline import fetch_audio as fa
from clipper.pipeline.common import Deadline, StageTimeout
from clipper.pipeline.fetch_audio import (
    AUDIO_NAME,
    STAGE,
    MediaError,
    SourceNotReady,
    fetch_audio,
    loudness_peaks,
    parse_ebur128,
)
from clipper.pipeline.ytdlp import (
    CommandTimeout,
    DownloadBlocked,
    RunResult,
    Unavailable,
    YtDlpError,
    run_command,
)
from clipper.urls import SourceRef, canonical_url

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
YT = SourceRef("youtube", "dQw4w9WgXcQ")
URL = canonical_url(YT)
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

EBUR128 = "\n".join(
    [
        "[Parsed_ebur128_0 @ 0x1] t: 0.1  TARGET:-23 LUFS    M: -inf S: -inf  I: -70.0 LUFS",
        "[Parsed_ebur128_0 @ 0x1] t: 0.5  TARGET:-23 LUFS    M: -120.7 S: -120.7  I: -70.0 LUFS",
        "[Parsed_ebur128_0 @ 0x1] t: 10.0 TARGET:-23 LUFS    M: -20.0 S: -21.0  I: -22.0 LUFS",
        "[Parsed_ebur128_0 @ 0x1] t: 12.0 TARGET:-23 LUFS    M: -10.5 S: -21.0  I: -22.0 LUFS",
        "[Parsed_ebur128_0 @ 0x1] t: 90.0 TARGET:-23 LUFS    M: -15.0 S: -21.0  I: -22.0 LUFS",
        "  Integrated loudness:",
        "    I:         -21.8 LUFS",
    ]
)


def base_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": YT.item_id,
        "title": "A video",
        "duration": 600,
        "uploader": "Someone",
        "upload_date": "20260102",
        "live_status": "not_live",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=raw",
        "heatmap": [
            {"start_time": 0.0, "end_time": 6.0, "value": 1.0},
            {"start_time": 6.0, "end_time": 12.0, "value": 0.25},
            {"start_time": "bad", "end_time": 1, "value": 1},
        ],
        "formats": [{"url": "https://rr1.googlevideo.com/videoplayback?sig=SECRET"}],
    }
    info.update(overrides)
    return info


class FakeRunner:
    """Answers yt-dlp probe/download and ffmpeg/ffprobe calls without any processes."""

    def __init__(self, info: dict[str, Any] | None = None) -> None:
        self.info = info if info is not None else base_info()
        self.calls: list[list[str]] = []
        self.responses: dict[str, RunResult | BaseException] = {}
        self.download_ext = "webm"
        self.media_delegate: Callable[[Sequence[str], float], RunResult] | None = None
        self.source_file: Path | None = None

    @staticmethod
    def kind(args: Sequence[str]) -> str:
        if "yt_dlp" in args:
            return "probe" if "--dump-single-json" in args else "download"
        if args[0] == "ffprobe":
            return "ffprobe"
        return "loudness" if "ebur128=framelog=info" in args else "convert"

    def __call__(self, args: Sequence[str], timeout_s: float) -> RunResult:
        args = list(args)
        self.calls.append(args)
        kind = self.kind(args)
        response = self.responses.get(kind)
        if isinstance(response, BaseException):
            raise response
        if response is not None:
            return response
        if kind == "probe":
            return RunResult(0, json.dumps(self.info), "")
        if kind == "download":
            info_path = Path(args[args.index("--load-info-json") + 1])
            assert json.loads(info_path.read_text())["id"] == YT.item_id
            template = args[args.index("-o") + 1]
            out = Path(template.replace("%(ext)s", self.download_ext))
            if self.source_file is not None:
                shutil.copyfile(self.source_file, out)
            else:
                out.write_bytes(b"fake audio")
            return RunResult(0, "", "")
        if self.media_delegate is not None:
            return self.media_delegate(args, timeout_s)
        if kind == "convert":
            Path(args[-1]).write_bytes(b"fLaC fake")
            return RunResult(0, "", "")
        if kind == "ffprobe":
            return RunResult(0, "42.5\n", "")
        return RunResult(0, "", EBUR128)

    def of(self, kind: str) -> list[list[str]]:
        return [c for c in self.calls if self.kind(c) == kind]


def make_job(creator: str = "manual") -> Job:
    return Job(
        id=1,
        ref=YT,
        creator=creator,
        source_url=URL,
        status=JobStatus.FETCHING_AUDIO,
        is_stream=False,
        attempts=1,
    )


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    path = tmp_path / "media" / YT.key
    path.mkdir(parents=True)
    return path


@pytest.fixture
def ctx(media_dir: Path) -> Iterator[StageContext]:
    with closing(sqlite3.connect(":memory:")) as conn:
        yield StageContext(media_dir, conn, AppConfig())


def run(job: Job, ctx: StageContext, runner: FakeRunner, env: dict[str, str] | None = None):
    fetch_audio(job, ctx, runner=runner, env=env or {})


def leftovers(media_dir: Path) -> list[str]:
    return sorted(p.name for p in media_dir.iterdir())


def test_stage_def() -> None:
    assert STAGE.artifact == "audio_16k.flac"
    assert STAGE.run is fetch_audio


def test_happy_path_writes_artifacts(ctx: StageContext, media_dir: Path) -> None:
    runner = FakeRunner()
    run(make_job(), ctx, runner)

    assert leftovers(media_dir) == ["audio_16k.flac", "meta.json", "signals.json"]
    meta = json.loads((media_dir / "meta.json").read_text())
    assert meta == {
        "title": "A video",
        "duration_s": 600.0,
        "uploader": "Someone",
        "upload_date": "2026-01-02",
        "is_live": False,
        "webpage_url": URL,
    }
    signals = json.loads((media_dir / "signals.json").read_text())
    assert signals["heatmap"] == [
        {"start_s": 0.0, "end_s": 6.0, "value": 1.0},
        {"start_s": 6.0, "end_s": 12.0, "value": 0.25},
    ]
    # -inf / near-silent frames dropped; 10.0 is within 30 s of the louder 12.0.
    assert signals["loudness_peaks"] == [
        {"t_s": 12.0, "lufs": -10.5},
        {"t_s": 90.0, "lufs": -15.0},
    ]
    assert not any("SECRET" in p.read_text(errors="ignore") for p in media_dir.iterdir())


def test_ytdlp_only_sees_canonical_url_after_double_dash(
    ctx: StageContext, media_dir: Path
) -> None:
    runner = FakeRunner()
    job = dataclasses.replace(make_job(), source_url="https://youtu.be/dQw4w9WgXcQ?x=--exec")
    run(job, ctx, runner)
    for args in runner.of("probe") + runner.of("download"):
        assert args[-2:] == ["--", URL]
        assert not any("youtu.be" in a for a in args)


def test_audio_only_download_and_16k_mono_conversion(ctx: StageContext) -> None:
    runner = FakeRunner()
    run(make_job(), ctx, runner)
    (download,) = runner.of("download")
    assert download[download.index("-f") + 1] == "bestaudio"
    (convert,) = runner.of("convert")
    for flag, value in (("-ac", "1"), ("-ar", "16000"), ("-c:a", "flac")):
        assert convert[convert.index(flag) + 1] == value
    assert "-vn" in convert


def test_missing_heatmap_and_metadata_fields(ctx: StageContext, media_dir: Path) -> None:
    info = base_info(upload_date=None, uploader=None, channel="Chan", title=None)
    del info["heatmap"]
    run(make_job(), ctx, FakeRunner(info))
    meta = json.loads((media_dir / "meta.json").read_text())
    assert meta["upload_date"] is None
    assert meta["uploader"] == "Chan"
    assert meta["title"] == ""
    assert json.loads((media_dir / "signals.json").read_text())["heatmap"] == []


def test_was_live_vod_is_fetched_and_flagged(ctx: StageContext, media_dir: Path) -> None:
    run(make_job(), ctx, FakeRunner(base_info(live_status="was_live")))
    assert json.loads((media_dir / "meta.json").read_text())["is_live"] is True


@pytest.mark.parametrize("live_status", ["is_live", "is_upcoming", "post_live"])
def test_live_or_upcoming_is_retried_later(
    ctx: StageContext, media_dir: Path, live_status: str
) -> None:
    runner = FakeRunner(base_info(live_status=live_status))
    with pytest.raises(SourceNotReady, match=live_status):
        run(make_job(), ctx, runner)
    assert runner.of("download") == []
    assert leftovers(media_dir) == [fa.NOT_READY_MARKER]


def test_short_video_is_skipped_before_download(ctx: StageContext, media_dir: Path) -> None:
    runner = FakeRunner(base_info(duration=120))
    with pytest.raises(SkipJob, match=r"too short: 120s < min_duration_seconds 300s"):
        run(make_job(), ctx, runner)
    assert runner.of("download") == []
    assert leftovers(media_dir) == []


def test_min_duration_uses_creator_overrides(media_dir: Path) -> None:
    config = AppConfig(
        settings=Settings(min_duration_seconds=60),
        creators=(
            Creator(
                name="Long",
                region="global",
                youtube=YouTubeChannel(handle="@LongForm"),
                overrides=CreatorOverrides(min_duration_seconds=900),
            ),
        ),
    )
    with closing(sqlite3.connect(":memory:")) as conn:
        ctx = StageContext(media_dir, conn, config)
        with pytest.raises(SkipJob, match="900s"):
            run(make_job("long"), ctx, FakeRunner())  # name match is case-insensitive
        run(make_job("unknown creator"), ctx, FakeRunner())  # global 60 s applies
    assert (media_dir / AUDIO_NAME).is_file()


def test_unknown_duration_is_measured_then_filtered(ctx: StageContext, media_dir: Path) -> None:
    runner = FakeRunner(base_info(duration=None))
    with pytest.raises(SkipJob, match="too short: 42s"):  # ffprobe says 42.5 s
        run(make_job(), ctx, runner)
    assert runner.of("ffprobe")
    assert not (media_dir / AUDIO_NAME).exists()


def test_unknown_duration_measured_into_meta(media_dir: Path) -> None:
    config = AppConfig(settings=Settings(min_duration_seconds=0))
    with closing(sqlite3.connect(":memory:")) as conn:
        run(make_job(), StageContext(media_dir, conn, config), FakeRunner(base_info(duration=0)))
    assert json.loads((media_dir / "meta.json").read_text())["duration_s"] == 42.5


def test_bad_ffprobe_output(ctx: StageContext) -> None:
    runner = FakeRunner(base_info(duration=None))
    runner.responses["ffprobe"] = RunResult(0, "N/A\n", "")
    with pytest.raises(MediaError, match="no duration"):
        run(make_job(), ctx, runner)


@pytest.mark.parametrize(
    ("stage", "response", "error"),
    [
        ("probe", RunResult(1, "", "ERROR: Sign in to confirm you're not a bot"), DownloadBlocked),
        ("probe", RunResult(1, "", "ERROR: Private video"), Unavailable),
        ("download", RunResult(1, "", "ERROR: Sign in to confirm you're not a bot"),
         DownloadBlocked),
        ("download", RunResult(1, "", "ERROR: connection reset"), YtDlpError),
        ("convert", RunResult(1, "", "Invalid data found when processing input"), MediaError),
        ("convert", CommandTimeout("ffmpeg timed out after 1800s"), MediaError),
        ("loudness", RunResult(1, "", "boom"), MediaError),
    ],
)  # fmt: skip
def test_failures_leave_no_artifact(
    ctx: StageContext, media_dir: Path, stage: str, response: Any, error: type[Exception]
) -> None:
    runner = FakeRunner()
    runner.responses[stage] = response
    with pytest.raises(error):
        run(make_job(), ctx, runner)
    assert not (media_dir / AUDIO_NAME).exists()
    assert not any(name.endswith(".tmp") or "info" in name for name in leftovers(media_dir))


def test_crash_before_final_rename_leaves_stage_rerunnable(
    ctx: StageContext, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = fa.write_json_atomic

    def crash_on_signals(path: Path, payload: Any, **kw: Any) -> None:
        if path.name == "signals.json":
            raise OSError("disk full")
        real_write(path, payload, **kw)

    monkeypatch.setattr(fa, "write_json_atomic", crash_on_signals)
    with pytest.raises(OSError, match="disk full"):
        run(make_job(), ctx, FakeRunner())
    assert not (media_dir / AUDIO_NAME).exists()
    assert not (media_dir / f".{AUDIO_NAME}.tmp").exists()

    monkeypatch.setattr(fa, "write_json_atomic", real_write)
    run(make_job(), ctx, FakeRunner())  # the retry completes and replaces leftovers
    assert leftovers(media_dir) == ["audio_16k.flac", "meta.json", "signals.json"]


def test_stale_completed_download_removed_but_partial_kept(
    ctx: StageContext, media_dir: Path
) -> None:
    (media_dir / ".src.m4a").write_bytes(b"stale")
    (media_dir / ".src.webm.part").write_bytes(b"partial")
    runner = FakeRunner()
    runner.responses["convert"] = RunResult(1, "", "fail")
    with pytest.raises(MediaError):
        run(make_job(), ctx, runner)
    assert sorted(leftovers(media_dir)) == [".src.webm", ".src.webm.part"]


def test_cookies_and_proxy_from_env(ctx: StageContext, tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    runner = FakeRunner()
    env = {"CLIPPER_YTDLP_COOKIES": str(cookies), "CLIPPER_YTDLP_PROXY": "socks5://p:1080"}
    run(make_job(), ctx, runner, env)
    for args in runner.of("probe") + runner.of("download"):
        assert args[args.index("--cookies") + 1] == str(cookies)
        assert args[args.index("--proxy") + 1] == "socks5://p:1080"


def test_missing_cookies_file(ctx: StageContext, tmp_path: Path) -> None:
    env = {"CLIPPER_YTDLP_COOKIES": str(tmp_path / "nope.txt")}
    with pytest.raises(YtDlpError, match="missing file") as info:
        run(make_job(), ctx, FakeRunner(), env)
    assert "nope.txt" not in str(info.value)


def test_parse_ebur128_and_peaks() -> None:
    assert parse_ebur128(EBUR128) == [(10.0, -20.0), (12.0, -10.5), (90.0, -15.0)]
    samples = [(float(t), -30.0 + (t % 7)) for t in range(0, 300)]
    peaks = loudness_peaks(samples, count=5, min_spacing_s=30)
    assert len(peaks) == 5
    times = [p["t_s"] for p in peaks]
    assert times == sorted(times)
    assert all(b - a >= 30 for a, b in zip(times, times[1:], strict=False))
    assert loudness_peaks([]) == []


# --- worker integration: outcome recorded on the job -------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "clipper.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


def _worker(db: Path, tmp_path: Path, runner: FakeRunner) -> Worker:
    stage = StageDef(partial(fetch_audio, runner=runner, env={}), AUDIO_NAME)
    return Worker(
        conn=connect(db),
        config=AppConfig(),
        worker_id="w1",
        stages={
            JobStatus.DETECTED: StageDef(lambda job, ctx: None),
            JobStatus.FETCHING_AUDIO: stage,
        },
        media_root=tmp_path / "media",
        clock=lambda: T0,
    )


def _row(db: Path, job_id: int) -> sqlite3.Row:
    with closing(connect(db)) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_download_blocked_recorded_on_job(db: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.responses["probe"] = RunResult(
        1, "", "ERROR: [youtube] x: Sign in to confirm you're not a bot"
    )
    worker = _worker(db, tmp_path, runner)
    with closing(worker.conn):
        job, _ = enqueue(worker.conn, YT, T0, creator="manual")
        assert worker.run_once()  # detected -> fetching_audio
        assert worker.run_once()  # fetch fails, retry scheduled
    row = _row(db, job.id)
    assert row["status"] == JobStatus.FETCHING_AUDIO.value
    assert row["last_error"].startswith("DownloadBlocked: blocked by platform")
    assert row["next_attempt_at"] is not None


def test_skip_and_permanent_outcomes_via_worker(db: Path, tmp_path: Path) -> None:
    runner = FakeRunner(base_info(duration=10))
    worker = _worker(db, tmp_path, runner)
    with closing(worker.conn):
        job, _ = enqueue(worker.conn, YT, T0, creator="manual")
        worker.run_once()
        worker.run_once()
        other, _ = enqueue(worker.conn, SourceRef("youtube", "aaaaaaaaaaa"), T0, creator="m")
        runner.info = base_info()
        runner.responses["probe"] = RunResult(1, "", "ERROR: Private video")
        worker.run_once()
        worker.run_once()
    assert _row(db, job.id)["status"] == JobStatus.SKIPPED.value
    assert "too short" in _row(db, job.id)["skip_reason"]
    assert _row(db, other.id)["status"] == JobStatus.FAILED.value
    assert issubclass(Unavailable, PermanentError)


def test_worker_success_advances(db: Path, tmp_path: Path) -> None:
    worker = _worker(db, tmp_path, FakeRunner())
    with closing(worker.conn):
        job, _ = enqueue(worker.conn, YT, T0, creator="manual")
        worker.run_once()
        worker.run_once()
    assert _row(db, job.id)["status"] == JobStatus.TRANSCRIBING.value
    assert (tmp_path / "media" / YT.key / AUDIO_NAME).is_file()


# --- real ffmpeg: conversion + ebur128 parsing on a generated tone ----------------------


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_tone(ctx: StageContext, media_dir: Path, tmp_path: Path) -> None:
    source = tmp_path / "tone.m4a"
    subprocess.run(  # noqa: S603
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=44100",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=5",
            "-shortest", "-ac", "2", "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(source),
        ],
        check=True,
    )  # fmt: skip
    runner = FakeRunner(base_info(duration=5.0, heatmap=None))
    runner.source_file = source
    runner.download_ext = "m4a"
    runner.media_delegate = run_command
    config = AppConfig(settings=Settings(min_duration_seconds=0))
    run(make_job(), StageContext(media_dir, ctx.conn, config), runner)

    audio = media_dir / AUDIO_NAME
    probe = subprocess.run(  # noqa: S603
        [
            FFPROBE, "-v", "error", "-show_entries",
            "stream=codec_name,codec_type,sample_rate,channels:format=duration",
            "-of", "json", str(audio),
        ],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    data = json.loads(probe.stdout)
    (stream,) = data["streams"]  # audio only, no video stream
    assert stream["codec_type"] == "audio"
    assert stream["codec_name"] == "flac"
    assert stream["sample_rate"] == "16000"
    assert stream["channels"] == 1
    assert abs(float(data["format"]["duration"]) - 5.0) <= 1.0

    signals = json.loads((media_dir / "signals.json").read_text())
    assert signals["heatmap"] == []
    peaks = signals["loudness_peaks"]
    assert len(peaks) == 1  # 5 s tone, 30 s min spacing
    assert 0 < peaks[0]["t_s"] <= 5.5
    assert -30 < peaks[0]["lufs"] < 0
    assert leftovers(media_dir) == ["audio_16k.flac", "meta.json", "signals.json"]


# --- review hardening ------------------------------------------------------------------------


def test_not_ready_defers_thirty_minutes(media_dir: Path) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        ctx = StageContext(media_dir, conn, AppConfig(), clock=lambda: T0)
        with pytest.raises(Deferred) as exc:
            run(make_job(), ctx, FakeRunner(base_info(live_status="is_upcoming")))
    assert isinstance(exc.value, SourceNotReady)
    assert exc.value.until == T0 + fa.NOT_READY_RETRY


def _probe_at(media_dir: Path, when: datetime, job: Job, info: dict[str, Any]) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        run(job, StageContext(media_dir, conn, AppConfig(), clock=lambda: when), FakeRunner(info))


def test_not_ready_for_more_than_48h_in_this_stage_is_permanent(media_dir: Path) -> None:
    live = base_info(live_status="is_live")
    with pytest.raises(SourceNotReady):
        _probe_at(media_dir, T0, make_job(), live)
    with pytest.raises(SourceNotReady):
        _probe_at(media_dir, T0 + timedelta(hours=47), make_job(), live)
    with pytest.raises(PermanentError, match="giving up"):
        _probe_at(media_dir, T0 + timedelta(hours=49), make_job(), live)


def test_not_ready_age_ignores_time_spent_before_this_stage(media_dir: Path) -> None:
    # A stream that sat in waiting_vod for days is deferred, not failed, on its first probe.
    old_job = dataclasses.replace(make_job(), created_at=T0 - timedelta(days=5))
    with pytest.raises(SourceNotReady):
        _probe_at(media_dir, T0, old_job, base_info(live_status="post_live"))


def test_not_ready_marker_cleared_once_the_source_is_ready(media_dir: Path) -> None:
    with pytest.raises(SourceNotReady):
        _probe_at(media_dir, T0, make_job(), base_info(live_status="is_live"))
    _probe_at(media_dir, T0 + timedelta(hours=1), make_job(), base_info())
    assert not (media_dir / fa.NOT_READY_MARKER).exists()


def test_too_long_video_is_skipped_before_download(ctx: StageContext, media_dir: Path) -> None:
    runner = FakeRunner(base_info(duration=57_601))
    with pytest.raises(SkipJob, match=r"too long: 57601s > max_duration_seconds 57600s"):
        run(make_job(), ctx, runner)
    assert runner.of("download") == []


def test_max_duration_uses_creator_overrides(media_dir: Path) -> None:
    config = AppConfig(
        creators=(
            Creator(
                name="Short",
                region="global",
                youtube=YouTubeChannel(handle="@Short"),
                overrides=CreatorOverrides(max_duration_seconds=3600),
            ),
        ),
    )
    with closing(sqlite3.connect(":memory:")) as conn:
        ctx = StageContext(media_dir, conn, config)
        with pytest.raises(SkipJob, match="too long"):
            run(make_job("Short"), ctx, FakeRunner(base_info(duration=3601)))


def test_download_is_size_capped_and_ffmpeg_inputs_are_file_only(ctx: StageContext) -> None:
    runner = FakeRunner()
    run(make_job(), ctx, runner)
    (download,) = runner.of("download")
    assert download[download.index("--max-filesize") + 1] == "4G"
    for args in runner.of("convert") + runner.of("loudness"):
        i = args.index("-protocol_whitelist")
        assert args[i + 1] == "file,pipe" and i < args.index("-i")


def test_stage_budget_shrinks_timeouts(ctx: StageContext, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    runner = FakeRunner()
    real_call = runner.__call__

    def recording(args: Sequence[str], timeout_s: float) -> RunResult:
        seen.append(timeout_s)
        return real_call(args, timeout_s)

    monkeypatch.setattr(fa, "Deadline", lambda: Deadline(budget_s=90.0))
    fetch_audio(make_job(), ctx, runner=recording, env={})
    assert seen and all(0 < t <= 90.0 for t in seen)


def test_stage_budget_exhausted_is_retryable(
    ctx: StageContext, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fa, "Deadline", lambda: Deadline(budget_s=0.5))
    with pytest.raises(StageTimeout) as exc:
        run(make_job(), ctx, FakeRunner())
    assert not isinstance(exc.value, PermanentError)
    assert not (media_dir / AUDIO_NAME).exists()


def test_media_error_tail_is_redacted(ctx: StageContext) -> None:
    runner = FakeRunner()
    runner.responses["convert"] = RunResult(
        1, "", "Error opening http://u:SECRET@h/x?sig=SECRET2 Authorization: Bearer SECRET3"
    )
    with pytest.raises(MediaError) as exc:
        run(make_job(), ctx, runner)
    assert "SECRET" not in str(exc.value)


def test_source_is_removed_before_the_artifact_appears(
    ctx: StageContext, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = fa.os.replace
    seen: list[list[str]] = []

    def spy(src: Path, dst: Path) -> None:
        if Path(dst).name == AUDIO_NAME:
            seen.append(sorted(p.name for p in media_dir.glob(".src.*")))
        real_replace(src, dst)

    monkeypatch.setattr(fa.os, "replace", spy)
    run(make_job(), ctx, FakeRunner())
    assert seen == [[]]
