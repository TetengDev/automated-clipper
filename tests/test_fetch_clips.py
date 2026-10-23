"""fetch_clips stage (CLP-16): download only the selected sections, padded and resumable.

The yt-dlp runner is faked; it "downloads" by rendering a real small mp4 with ffmpeg so the
stage's ffprobe checks run for real. No network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clipper.jobs.models import Job, JobStatus
from clipper.jobs.stages import Deferred, PermanentError, SkipJob, StageContext
from clipper.pipeline import fetch_clips as fc
from clipper.pipeline.common import (
    NO_FORMAT_MAX_AGE,
    NO_FORMAT_RETRY,
    Deadline,
    NoUsableFormat,
    StageTimeout,
)
from clipper.pipeline.ytdlp import (
    CommandTimeout,
    DownloadBlocked,
    RunResult,
    YtDlpError,
    run_command,
)
from clipper.urls import SourceRef

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

VIDEO_ID = "dQw4w9WgXcQ"
CANONICAL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _clip(rank: int, start: float, end: float) -> dict[str, Any]:
    return {
        "rank": rank,
        "start_s": start,
        "end_s": end,
        "hook": "h",
        "title": "t",
        "caption": "c",
        "hashtags": ["#x"],
        "score": 90,
        "reason": "r",
    }


def _setup(media: Path, clips: list[dict[str, Any]], duration: float | None = 60.0) -> None:
    media.mkdir(parents=True, exist_ok=True)
    (media / "highlights.json").write_text(
        json.dumps({"model": "m", "prompt_version": "1", "clips": clips})
    )
    meta: dict[str, Any] = {"title": "x"}
    if duration is not None:
        meta["duration_s"] = duration
    (media / "meta.json").write_text(json.dumps(meta))


def _job(source_url: str = "https://youtu.be/dQw4w9WgXcQ?si=tracking") -> Job:
    return Job(
        id=1,
        ref=SourceRef("youtube", VIDEO_ID),
        creator="someone",
        source_url=source_url,
        status=JobStatus.FETCHING_CLIPS,
        is_stream=False,
        attempts=0,
    )


def _ctx(media: Path) -> StageContext:
    return StageContext(media_dir=media, conn=None, config=None)  # type: ignore[arg-type]


def _make_mp4(path: Path, seconds: float) -> None:
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=10:duration={seconds:.3f}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _section(argv: Sequence[str]) -> tuple[float, float]:
    spec = argv[argv.index("--download-sections") + 1]
    assert spec.startswith("*")
    start, end = spec[1:].split("-")
    return float(start), float(end)


class FakeYtdlp:
    """Records calls; writes a real mp4 of the requested section's length."""

    def __init__(
        self, *, returncode: int = 0, stderr: str = "", fail_ranks: frozenset[int] = frozenset()
    ) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.fail_ranks = fail_ranks

    def __call__(self, args: Sequence[str], timeout_s: float) -> RunResult:
        argv = list(args)
        if "yt_dlp" not in argv:  # ffprobe checks run for real
            return run_command(argv, timeout_s)
        self.calls.append(argv)
        out = Path(argv[argv.index("-o") + 1].replace("%(ext)s", "mp4"))
        rank = int(out.parent.name)
        if self.returncode or rank in self.fail_ranks:
            return RunResult(self.returncode or 1, "", self.stderr)
        start, end = _section(argv)
        _make_mp4(out, end - start)
        return RunResult(0, "", "")


def _sections(media: Path) -> dict[str, Any]:
    return json.loads((media / "sections.json").read_text())


def test_stage_is_declared_with_sections_artifact() -> None:
    assert fc.STAGE.run is fc.fetch_clips
    assert fc.STAGE.artifact == "sections.json"


def test_argv_has_padded_sections_and_dash_dash_before_canonical_url(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 10.0, 13.0)])
    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)

    (argv,) = fake.calls
    assert argv[:3] == [sys.executable, "-m", "yt_dlp"]
    assert _section(argv) == (10.0 - fc.PAD_S, 13.0 + fc.PAD_S)
    assert "--force-keyframes-at-cuts" in argv
    assert argv[argv.index("-f") + 1] == "bv*[height<=1080]+ba/b[height<=1080]"
    assert argv[argv.index("--merge-output-format") + 1] == "mp4"
    # URL comes last, after "--", and is the canonical one (never the raw input).
    assert argv[-2:] == ["--", CANONICAL]
    assert not any("si=tracking" in a for a in argv)


def test_padding_is_clamped_to_video_bounds(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 0.5, 3.0), _clip(2, 57.0, 59.5)], duration=60.0)
    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)

    assert _section(fake.calls[0]) == (0.0, 3.0 + fc.PAD_S)
    assert _section(fake.calls[1]) == (57.0 - fc.PAD_S, 60.0)
    by_rank = {c["rank"]: c for c in _sections(tmp_path)["clips"]}
    assert by_rank[1]["pad_start_s"] == pytest.approx(0.5)
    assert by_rank[2]["pad_start_s"] == pytest.approx(fc.PAD_S)


def test_sections_json_schema_and_alignment(tmp_path: Path) -> None:
    clips = [_clip(2, 20.0, 23.0), _clip(1, 5.0, 8.5)]
    _setup(tmp_path, clips)
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())

    data = _sections(tmp_path)
    assert [c["rank"] for c in data["clips"]] == [1, 2]
    for entry in data["clips"]:
        assert set(entry) == {
            "rank",
            "path",
            "requested_start_s",
            "requested_end_s",
            "pad_start_s",
            "duration_s",
            "width",
            "height",
        }
        assert entry["path"] == f"clips/{entry['rank']}/source.mp4"
        assert (tmp_path / entry["path"]).is_file()
        assert (entry["width"], entry["height"]) == (320, 180)
        length = entry["requested_end_s"] - entry["requested_start_s"]
        # AC: segment covers requested range plus padding within +/-0.5 s.
        assert entry["pad_start_s"] == pytest.approx(fc.PAD_S, abs=0.5)
        assert entry["duration_s"] == pytest.approx(length + 2 * fc.PAD_S, abs=0.5)
        # The clip (pad_start_s .. pad_start_s + length) lies inside the file.
        assert entry["pad_start_s"] + length <= entry["duration_s"] + 0.5
    assert data["clips"][0]["requested_start_s"] == 5.0
    assert data["clips"][0]["requested_end_s"] == 8.5
    # No temp files left behind.
    leftovers = [p.name for p in tmp_path.rglob("*") if p.is_file() and ".tmp" in p.name]
    assert leftovers == []


def test_resume_skips_existing_valid_clips(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0), _clip(2, 20.0, 23.0)])
    first = FakeYtdlp(fail_ranks=frozenset({2}))
    with pytest.raises(RuntimeError):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=first)
    assert (tmp_path / "clips/1/source.mp4").is_file()
    assert not (tmp_path / "sections.json").exists()

    second = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=second)
    assert len(second.calls) == 1
    assert "clips/2/" in second.calls[0][second.calls[0].index("-o") + 1]
    assert [c["rank"] for c in _sections(tmp_path)["clips"]] == [1, 2]


def test_corrupt_existing_clip_is_redownloaded(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    bad = tmp_path / "clips/1/source.mp4"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a video")

    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)
    assert len(fake.calls) == 1
    assert _sections(tmp_path)["clips"][0]["duration_s"] > 3.0


def test_failure_leaves_no_sections_json(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    with pytest.raises(RuntimeError):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp(returncode=1, stderr="boom"))
    assert not (tmp_path / "sections.json").exists()
    assert not (tmp_path / "clips/1/source.mp4").exists()


def test_missing_output_is_an_error(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])

    def silent(args: Sequence[str], timeout_s: float) -> RunResult:
        if "yt_dlp" not in args:
            return run_command(args, timeout_s)
        return RunResult(0, "", "")

    with pytest.raises(YtDlpError, match="expected one downloaded file, found 0"):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=silent)
    assert not (tmp_path / "sections.json").exists()


def test_short_download_is_rejected(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 15.0)])

    def short(args: Sequence[str], timeout_s: float) -> RunResult:
        if "yt_dlp" not in args:
            return run_command(args, timeout_s)
        out = Path(args[list(args).index("-o") + 1].replace("%(ext)s", "mp4"))
        _make_mp4(out, 2.0)
        return RunResult(0, "", "")

    with pytest.raises(RuntimeError, match="shorter"):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=short)
    assert not (tmp_path / "clips/1/source.mp4").exists()


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable. This video has been removed",
    ],
)
def test_private_or_removed_is_permanent(tmp_path: Path, stderr: str) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    with pytest.raises(PermanentError):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp(returncode=1, stderr=stderr))


def test_bot_check_is_retryable(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    stderr = "ERROR: [youtube] x: Sign in to confirm you're not a bot"
    with pytest.raises(DownloadBlocked, match="bot check"):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp(returncode=1, stderr=stderr))


def test_stderr_is_redacted_in_errors(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    stderr = "ERROR: fetch https://x.googlevideo.com/v?sig=SECRET123&a=1 failed"
    with pytest.raises(YtDlpError) as exc:
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp(returncode=1, stderr=stderr))
    assert "SECRET123" not in str(exc.value)


def test_timeout_is_retryable(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    seen: list[float] = []

    def hang(args: Sequence[str], timeout_s: float) -> RunResult:
        seen.append(timeout_s)
        raise CommandTimeout("python timed out after 600s")

    with pytest.raises(YtDlpError, match="timed out") as exc:
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=hang)
    assert not isinstance(exc.value, PermanentError)
    assert seen == [fc.MIN_TIMEOUT_S]


def test_cookies_and_proxy_from_env_are_passed(tmp_path: Path) -> None:
    _setup(tmp_path / "media", [_clip(1, 5.0, 8.0)])
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    fake = FakeYtdlp()
    env = {"CLIPPER_YTDLP_COOKIES": str(cookies), "CLIPPER_YTDLP_PROXY": "socks5://p:1"}
    fc.fetch_clips(_job(), _ctx(tmp_path / "media"), runner=fake, env=env)
    (argv,) = fake.calls
    assert argv[argv.index("--cookies") + 1] == str(cookies)
    assert argv[argv.index("--proxy") + 1] == "socks5://p:1"


def test_no_clips_skips_job(tmp_path: Path) -> None:
    _setup(tmp_path, [])
    with pytest.raises(SkipJob):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())


def test_missing_highlights_is_permanent(tmp_path: Path) -> None:
    # Same rule as every stage: a missing input artifact will not reappear on retry.
    with pytest.raises(PermanentError, match="highlights.json"):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())


@pytest.mark.parametrize(
    "clips",
    [
        [_clip(1, 8.0, 5.0)],
        [_clip(1, -1.0, 5.0)],
        [_clip(0, 1.0, 5.0)],
        [{**_clip(1, 1.0, 5.0), "rank": "../x"}],
        [_clip(1, 1.0, 5.0), _clip(1, 6.0, 9.0)],
        [{**_clip(1, 1.0, 5.0), "start_s": float("nan")}],
        [_clip(1, 70.0, 75.0)],
    ],
)
def test_invalid_highlights_are_permanent(tmp_path: Path, clips: list[dict[str, Any]]) -> None:
    _setup(tmp_path, clips, duration=60.0)
    with pytest.raises(PermanentError):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())


def test_malformed_highlights_json_is_permanent(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 1.0, 5.0)])
    (tmp_path / "highlights.json").write_text("{not json")
    with pytest.raises(PermanentError):
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())


def test_unknown_duration_pads_end_without_clamp(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)], duration=None)
    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)
    assert _section(fake.calls[0]) == (5.0 - fc.PAD_S, 8.0 + fc.PAD_S)


# --- review hardening ------------------------------------------------------------------------


def test_existing_clip_for_a_different_section_is_redownloaded(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())
    sidecar = json.loads((tmp_path / "clips/1/.section.json").read_text())
    assert sidecar == {"start": 3.0, "end": 10.0, "url": CANONICAL, "format": fc.FORMAT}

    cuts = tmp_path / "clips/1/source.mp4.cuts.json"  # scene cuts of the old file
    cuts.write_text("[]")

    # Highlights changed (e.g. selection re-run): clip 1 now means another moment.
    (tmp_path / "sections.json").unlink()
    _setup(tmp_path, [_clip(1, 30.0, 34.0)])
    again = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=again)
    assert len(again.calls) == 1
    assert _section(again.calls[0]) == (28.0, 36.0)
    assert _sections(tmp_path)["clips"][0]["duration_s"] == pytest.approx(8.0, abs=0.5)
    assert not cuts.exists()


def test_playable_clip_without_sidecar_is_redownloaded(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    dest = tmp_path / "clips/1/source.mp4"
    dest.parent.mkdir(parents=True)
    _make_mp4(dest, 7.0)
    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)
    assert len(fake.calls) == 1


def test_ffprobe_goes_through_the_injected_runner(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    fake = FakeYtdlp()
    probes: list[list[str]] = []

    def runner(args: Sequence[str], timeout_s: float) -> RunResult:
        if args[0] == "ffprobe":
            probes.append(list(args))
            assert args[1:3] == ["-protocol_whitelist", "file,pipe"]
        return fake(args, timeout_s)

    fc.fetch_clips(_job(), _ctx(tmp_path), runner=runner)
    assert len(probes) == 1


def test_section_download_is_size_capped(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    fake = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=fake)
    (argv,) = fake.calls
    assert argv[argv.index("--max-filesize") + 1] == "2G"


def test_stage_budget_exhausted_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    monkeypatch.setattr(fc, "Deadline", lambda: Deadline(budget_s=0.5))
    with pytest.raises(StageTimeout) as exc:
        fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())
    assert not isinstance(exc.value, PermanentError)
    assert not (tmp_path / "sections.json").exists()


def test_clip_downloaded_with_another_format_is_redownloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=FakeYtdlp())
    (tmp_path / "sections.json").unlink()
    monkeypatch.setattr(fc, "FORMAT", "bv*[height<=720]+ba/b[height<=720]")
    again = FakeYtdlp()
    fc.fetch_clips(_job(), _ctx(tmp_path), runner=again)
    assert len(again.calls) == 1
    assert again.calls[0][again.calls[0].index("-f") + 1] == "bv*[height<=720]+ba/b[height<=720]"


# --- format errors: a YouTube player breakage (CLP-83) ---------------------------------------

FORMAT_ERROR = "ERROR: [youtube] x: Requested format is not available. Use --list-formats"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _fetch_at(media: Path, when: datetime, runner: FakeYtdlp) -> None:
    ctx = StageContext(media_dir=media, conn=None, config=None, clock=lambda: when)  # type: ignore[arg-type]
    fc.fetch_clips(_job(), ctx, runner=runner)


def test_format_error_defers_the_clip_download(tmp_path: Path) -> None:
    # After paid transcription and selection, a player breakage must not fail the job.
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])

    with pytest.raises(Deferred) as exc:
        _fetch_at(tmp_path, T0, FakeYtdlp(returncode=1, stderr=FORMAT_ERROR))

    assert isinstance(exc.value, NoUsableFormat)
    assert exc.value.until == T0 + NO_FORMAT_RETRY
    assert (tmp_path / fc.NO_FORMAT_MARKER).exists()
    assert not (tmp_path / "sections.json").exists()


def test_format_error_past_the_age_cap_fails_the_clips_for_good(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    broken = FakeYtdlp(returncode=1, stderr=FORMAT_ERROR)
    with pytest.raises(NoUsableFormat):
        _fetch_at(tmp_path, T0, broken)
    with pytest.raises(NoUsableFormat):
        _fetch_at(tmp_path, T0 + NO_FORMAT_MAX_AGE - timedelta(hours=1), broken)

    with pytest.raises(PermanentError, match="requested format not available.*giving up"):
        _fetch_at(tmp_path, T0 + NO_FORMAT_MAX_AGE + timedelta(minutes=10), broken)


def test_format_marker_cleared_once_the_clips_download(tmp_path: Path) -> None:
    _setup(tmp_path, [_clip(1, 5.0, 8.0)])
    with pytest.raises(NoUsableFormat):
        _fetch_at(tmp_path, T0, FakeYtdlp(returncode=1, stderr=FORMAT_ERROR))

    _fetch_at(tmp_path, T0 + timedelta(hours=1), FakeYtdlp())

    assert not (tmp_path / fc.NO_FORMAT_MARKER).exists()
    assert (tmp_path / "sections.json").exists()
