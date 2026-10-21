"""Stage `fetching_audio` (CLP-13): download audio only and normalize it for transcription.

Steps: probe metadata (no download) -> filters (live/upcoming, min duration) -> download
`-f bestaudio` (never a video stream) -> ffmpeg to 16 kHz mono FLAC -> ebur128 loudness
peaks -> write `meta.json` and `signals.json` -> rename the FLAC into place LAST.

`audio_16k.flac` is the declared artifact, so a crash anywhere before the final rename
leaves the stage re-runnable. All file names are fixed constants inside the job's media
dir (derived by the worker from the validated `SourceRef`), and yt-dlp only ever sees
`canonical_url(job.ref)`.

Artifacts (shared contract with later stages):
- meta.json: {"title": str, "duration_s": float, "uploader": str,
  "upload_date": "YYYY-MM-DD" | null, "is_live": bool, "webpage_url": canonical url}
  (`is_live` is true when the source was a livestream; live/upcoming sources are not
  fetched, see below).
- signals.json: {"heatmap": [{"start_s", "end_s", "value"}],
  "loudness_peaks": [{"t_s", "lufs"}]} (peaks in time order).

Live / upcoming / still-processing sources raise `SourceNotReady`, a `Deferred` (+30 min,
no attempt used); once the job is older than 48 h that becomes a `PermanentError`. The
stream path reaches this stage only after `waiting_vod`. Sources longer than
`max_duration_seconds` are skipped, and yt-dlp is capped with `--max-filesize`.

The whole run shares one `Deadline` (100 min, inside the 2 h lease): every yt-dlp/ffmpeg
call's timeout is shrunk to what is left, and running out is a retryable `StageTimeout`.

Optional env: CLIPPER_YTDLP_COOKIES (path to a cookies.txt), CLIPPER_YTDLP_PROXY.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from clipper.config import Settings
from clipper.jobs.models import Job
from clipper.jobs.stages import Deferred, PermanentError, SkipJob, StageContext, StageDef
from clipper.pipeline.common import (
    PROTOCOL_WHITELIST,
    Deadline,
    MediaError,
    default_runner,
    ffprobe_duration,
    run_media,
    settings_for,
    write_json_atomic,
)
from clipper.pipeline.ytdlp import Runner, YtDlp
from clipper.urls import canonical_url

__all__ = ["STAGE", "MediaError", "SourceNotReady", "fetch_audio"]  # MediaError re-exported

AUDIO_NAME = "audio_16k.flac"
META_NAME = "meta.json"
SIGNALS_NAME = "signals.json"
SOURCE_STEM = ".src"
INFO_TMP_NAME = ".info.tmp.json"
SAMPLE_RATE = 16_000
CONVERT_TIMEOUT_S = 30 * 60.0
LOUDNESS_TIMEOUT_S = 20 * 60.0
PROBE_TIMEOUT_S = 60.0
PEAK_COUNT = 20
PEAK_MIN_SPACING_S = 30.0
SILENCE_LUFS = -70.0
NOT_READY_RETRY = timedelta(minutes=30)
NOT_READY_MAX_AGE = timedelta(hours=48)
NOT_READY_LIVE_STATUSES = frozenset({"is_live", "is_upcoming", "post_live"})
WAS_LIVE_STATUSES = frozenset({"is_live", "was_live", "post_live"})

_EBUR128_LINE = re.compile(r"\bt:\s*(?P<t>[0-9.]+)\s.*?\bM:\s*(?P<m>-?inf|nan|-?[0-9.]+)")


class SourceNotReady(Deferred):
    """The source is live, upcoming, or still being processed; try again later."""


def _not_ready(job: Job, now: datetime, live_status: object) -> Exception:
    reason = f"source is not a finished video yet (live_status={live_status})"
    if job.created_at is not None and now - job.created_at > NOT_READY_MAX_AGE:
        hours = NOT_READY_MAX_AGE.total_seconds() / 3600
        return PermanentError(f"{reason} after {hours:.0f} h; giving up")
    return SourceNotReady(now + NOT_READY_RETRY, reason)


def fetch_audio(
    job: Job,
    ctx: StageContext,
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    media_dir = ctx.media_dir
    url = canonical_url(job.ref)
    streaming = runner is None  # the real runner can stream ffmpeg's log line by line
    runner = runner if runner is not None else default_runner()
    deadline = Deadline()
    ytdlp = YtDlp.from_env(runner, env, cap_timeout=deadline.timeout)
    settings = settings_for(job, ctx.config)

    info = ytdlp.probe(url)
    live_status = info.get("live_status")
    if live_status in NOT_READY_LIVE_STATUSES or info.get("is_live") is True:
        raise _not_ready(job, ctx.clock(), live_status)
    duration = _positive_float(info.get("duration"))
    if duration is not None:
        _check_duration(duration, settings)

    info_tmp = media_dir / INFO_TMP_NAME
    flac_tmp = media_dir / f".{AUDIO_NAME}.tmp"
    _remove_stale_sources(media_dir)
    try:
        write_json_atomic(info_tmp, info, indent=None)
        source = ytdlp.download_audio(url, media_dir, SOURCE_STEM, info_json=info_tmp)
        _to_flac(runner, source, flac_tmp, deadline)
        if duration is None:
            timeout = deadline.timeout(PROBE_TIMEOUT_S)
            duration = ffprobe_duration(flac_tmp, runner=runner, timeout_s=timeout)
            _check_duration(duration, settings)
        peaks = loudness_peaks(_momentary_loudness(runner, flac_tmp, deadline, streaming))

        meta = {
            "title": str(info.get("title") or ""),
            "duration_s": duration,
            "uploader": str(info.get("uploader") or info.get("channel") or ""),
            "upload_date": _iso_date(info.get("upload_date")),
            "is_live": live_status in WAS_LIVE_STATUSES or bool(info.get("was_live")),
            "webpage_url": url,
        }
        signals = {"heatmap": _heatmap(info.get("heatmap")), "loudness_peaks": peaks}
        write_json_atomic(media_dir / META_NAME, meta)
        write_json_atomic(media_dir / SIGNALS_NAME, signals)
        source.unlink(missing_ok=True)  # before the artifact: never two copies on disk
        os.replace(flac_tmp, media_dir / AUDIO_NAME)  # the declared artifact: last
    finally:
        info_tmp.unlink(missing_ok=True)  # holds signed format URLs; never keep it
        flac_tmp.unlink(missing_ok=True)


STAGE = StageDef(fetch_audio, AUDIO_NAME)


def _check_duration(duration: float, settings: Settings) -> None:
    minimum = settings.min_duration_seconds
    if duration < minimum:
        raise SkipJob(f"too short: {duration:.0f}s < min_duration_seconds {minimum}s")
    maximum = settings.max_duration_seconds
    if duration > maximum:
        raise SkipJob(f"too long: {duration:.0f}s > max_duration_seconds {maximum}s")


def _positive_float(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _iso_date(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return None


def _heatmap(raw: Any) -> list[dict[str, float]]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start, end, value = (_finite(item.get(k)) for k in ("start_time", "end_time", "value"))
        if start is not None and end is not None and value is not None:
            out.append({"start_s": start, "end_s": end, "value": value})
    return out


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _remove_stale_sources(media_dir: Path) -> None:
    """Drop completed downloads from an earlier attempt; keep `.part` files so yt-dlp
    resumes a long download instead of starting over."""
    for path in media_dir.glob(f"{SOURCE_STEM}.*"):
        if path.is_file() and path.suffix not in {".part", ".ytdl"}:
            path.unlink()


def _to_flac(runner: Runner, source: Path, dest: Path, deadline: Deadline) -> None:
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *PROTOCOL_WHITELIST, "-i", str(source.resolve()),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "flac", "-f", "flac",
        str(dest.resolve()),
    ]  # fmt: skip
    run_media(args, deadline.timeout(CONVERT_TIMEOUT_S), "audio conversion", runner=runner)


def _momentary_loudness(
    runner: Runner, path: Path, deadline: Deadline, streaming: bool
) -> list[tuple[float, float]]:
    """ebur128 logs one line per 100 ms (hours of audio = many MB), so the real runner
    parses lines as they arrive instead of keeping the log; injected runners return it."""
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
        *PROTOCOL_WHITELIST, "-i", str(path.resolve()),
        "-af", "ebur128=framelog=info", "-f", "null", "-",
    ]  # fmt: skip
    timeout = deadline.timeout(LOUDNESS_TIMEOUT_S)
    if not streaming:
        return parse_ebur128(run_media(args, timeout, "loudness", runner=runner).stderr)
    samples: list[tuple[float, float]] = []
    run_media(
        args,
        timeout,
        "loudness",
        runner=runner,
        on_stderr_line=lambda line: samples.extend(parse_ebur128(line)),
    )
    return samples


def parse_ebur128(stderr: str) -> list[tuple[float, float]]:
    """(t_s, momentary LUFS) per ebur128 frame log line; silent/-inf frames dropped."""
    samples = []
    for match in _EBUR128_LINE.finditer(stderr):
        lufs = float(match["m"])
        if math.isfinite(lufs) and lufs > SILENCE_LUFS:
            samples.append((float(match["t"]), lufs))
    return samples


def loudness_peaks(
    samples: Sequence[tuple[float, float]],
    count: int = PEAK_COUNT,
    min_spacing_s: float = PEAK_MIN_SPACING_S,
) -> list[dict[str, float]]:
    """Loudest `count` moments at least `min_spacing_s` apart, returned in time order."""
    chosen: list[tuple[float, float]] = []
    for t, lufs in sorted(samples, key=lambda s: (-s[1], s[0])):
        if len(chosen) >= count:
            break
        if all(abs(t - other) >= min_spacing_s for other, _ in chosen):
            chosen.append((t, lufs))
    return [{"t_s": round(t, 3), "lufs": lufs} for t, lufs in sorted(chosen)]
