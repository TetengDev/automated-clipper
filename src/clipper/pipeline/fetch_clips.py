"""FETCHING_CLIPS stage (CLP-16): download only the selected sections of the source video.

Input: `highlights.json` (CLP-15) and `meta.json` in the job's media dir. For each clip,
yt-dlp downloads just `[start - PAD_S, end + PAD_S]` (clamped to the video) at <=1080p into
`clips/<rank>/source.mp4`. Clips already on disk that pass an ffprobe check are kept, so a
retried job resumes. `sections.json` is written last and marks the stage done:

    {"clips": [{"rank", "path", "requested_start_s", "requested_end_s",
                "pad_start_s", "duration_s", "width", "height"}]}

`pad_start_s` is where the requested clip starts inside the file (seconds); CLP-17 seeks
there when rendering.

yt-dlp runs through the shared `clipper.pipeline.ytdlp.YtDlp` wrapper (same error mapping,
cookies/proxy env and redaction as `fetching_audio`). Each kept clip has a sidecar
`clips/<rank>/.section.json` ({"start", "end", "url", "format"}): an existing source.mp4 is only
reused if its sidecar matches the section wanted now (highlights can change between runs).
All calls share one 100 min `Deadline`; running out is a retryable `StageTimeout`.
"Requested format is not available" (a YouTube player change) defers the stage like in
`fetching_audio` (`NoUsableFormat`, +30 min), failing for good only after 48 h of it.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clipper.jobs.models import Job
from clipper.jobs.stages import PermanentError, SkipJob, StageContext, StageDef
from clipper.pipeline.common import (
    FFPROBE_TIMEOUT_S,
    PROTOCOL_WHITELIST,
    Deadline,
    default_runner,
    deferring_no_format,
    read_json,
    write_json_atomic,
)
from clipper.pipeline.ytdlp import CLIP_FORMAT, CommandTimeout, Runner, YtDlp, YtDlpError
from clipper.urls import canonical_url

PAD_S = 2.0
"""Padding downloaded before and after each clip (CLP-16: 2 s)."""
FORMAT = CLIP_FORMAT
"""Shared with the probe (monitor and fetch_audio), so a video that gets this far has one."""
MIN_TIMEOUT_S = 600.0
TIMEOUT_PER_SECOND = 20.0
"""Per-clip yt-dlp timeout: generous, but well inside the 2 h claim lease."""
TOLERANCE_S = 0.5
TMP_STEM = "source.tmp"
SIDECAR = ".section.json"
NO_FORMAT_MARKER = ".clips_no_format_since"
"""When this stage first hit a format error (a player breakage): see `deferring_no_format`."""


@dataclass(frozen=True)
class _Clip:
    rank: int
    start: float
    end: float


@dataclass(frozen=True)
class _Probe:
    duration: float
    width: int
    height: int


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _video_duration(media_dir: Path, job: Job) -> float | None:
    meta = read_json(media_dir / "meta.json", required=False)
    if isinstance(meta, dict):
        duration = _number(meta.get("duration_s"))
        if duration is not None and duration > 0:
            return duration
    return float(job.duration_seconds) if job.duration_seconds else None


def _load_clips(media_dir: Path, duration: float | None) -> list[_Clip]:
    data = read_json(media_dir / "highlights.json")
    raw = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise PermanentError("highlights.json has no clips list")
    clips: list[_Clip] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PermanentError("highlights.json clip is not an object")
        rank = item.get("rank")
        start, end = _number(item.get("start_s")), _number(item.get("end_s"))
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise PermanentError(f"invalid clip rank: {rank!r}")
        if start is None or end is None or start < 0 or end <= start:
            raise PermanentError(f"clip {rank}: invalid range")
        if duration is not None and end > duration + TOLERANCE_S:
            raise PermanentError(
                f"clip {rank}: ends after the video ({end:.1f}s > {duration:.1f}s)"
            )
        clips.append(_Clip(rank, start, end))
    ranks = [c.rank for c in clips]
    if len(set(ranks)) != len(ranks):
        raise PermanentError("highlights.json has duplicate clip ranks")
    return sorted(clips, key=lambda c: c.rank)


def _section(clip: _Clip, duration: float | None) -> tuple[float, float]:
    start = max(0.0, clip.start - PAD_S)
    end = clip.end + PAD_S
    if duration is not None:
        end = min(duration, end)
    return round(start, 3), round(end, 3)


def _probe(path: Path, runner: Runner, deadline: Deadline) -> _Probe | None:
    """Duration and video size, or None if the file is not a playable video."""
    args = [
        "ffprobe", *PROTOCOL_WHITELIST, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height", "-of", "json", "--", str(path),
    ]  # fmt: skip
    try:
        result = runner(args, deadline.timeout(FFPROBE_TIMEOUT_S))
    except CommandTimeout:
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        video = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, KeyError, TypeError, StopIteration):
        return None
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        return None
    return _Probe(duration, width, height)


def _sidecar(url: str, section: tuple[float, float]) -> dict[str, object]:
    return {"start": section[0], "end": section[1], "url": url, "format": FORMAT}


def _reusable(
    dest: Path, want: dict[str, object], runner: Runner, deadline: Deadline
) -> _Probe | None:
    """The existing clip, if it was downloaded for exactly this section and still plays."""
    sidecar = dest.parent / SIDECAR
    if not dest.is_file() or not sidecar.is_file():
        return None
    try:
        if json.loads(sidecar.read_text(encoding="utf-8")) != want:
            return None
    except (OSError, ValueError):
        return None
    return _probe(dest, runner, deadline)


def _download(
    ytdlp: YtDlp,
    runner: Runner,
    deadline: Deadline,
    job: Job,
    clip: _Clip,
    section: tuple[float, float],
    dest: Path,
) -> _Probe:
    clip_dir = dest.parent
    clip_dir.mkdir(parents=True, exist_ok=True)
    for stale in clip_dir.glob(f"{TMP_STEM}*"):
        stale.unlink()
    start, end = section
    timeout = max(MIN_TIMEOUT_S, (end - start) * TIMEOUT_PER_SECOND)
    try:
        tmp = ytdlp.download_section(
            canonical_url(job.ref), clip_dir, TMP_STEM, section, format=FORMAT, timeout_s=timeout
        )
        probe = _probe(tmp, runner, deadline)
        if probe is None:
            raise YtDlpError(f"clip {clip.rank}: downloaded file is not a valid video")
        # The file must cover the whole requested clip (padding may be clamped).
        if probe.duration < (end - start) - TOLERANCE_S:
            raise YtDlpError(
                f"clip {clip.rank}: download is shorter than requested "
                f"({probe.duration:.1f}s < {end - start:.1f}s)"
            )
        (clip_dir / SIDECAR).unlink(missing_ok=True)
        dest.with_name(f"{dest.name}.cuts.json").unlink(missing_ok=True)  # CLP-68 scene cuts
        os.replace(tmp, dest)
        write_json_atomic(clip_dir / SIDECAR, _sidecar(canonical_url(job.ref), section))
        return probe
    finally:
        for leftover in clip_dir.glob(f"{TMP_STEM}*"):
            leftover.unlink()


def fetch_clips(
    job: Job,
    ctx: StageContext,
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    runner = runner if runner is not None else default_runner()
    deadline = Deadline()
    ytdlp = YtDlp.from_env(runner, env, cap_timeout=deadline.timeout)
    url = canonical_url(job.ref)
    duration = _video_duration(ctx.media_dir, job)
    clips = _load_clips(ctx.media_dir, duration)
    if not clips:
        raise SkipJob("no highlights to fetch")
    entries: list[dict[str, object]] = []
    for clip in clips:
        section = _section(clip, duration)
        rel = f"clips/{clip.rank}/source.mp4"
        dest = ctx.media_dir / rel
        probe = _reusable(dest, _sidecar(url, section), runner, deadline)
        if probe is None:
            dest.unlink(missing_ok=True)
            with deferring_no_format(ctx.media_dir / NO_FORMAT_MARKER, ctx.clock):
                probe = _download(ytdlp, runner, deadline, job, clip, section, dest)
        entries.append(
            {
                "rank": clip.rank,
                "path": rel,
                "requested_start_s": clip.start,
                "requested_end_s": clip.end,
                "pad_start_s": round(clip.start - section[0], 3),
                "duration_s": round(probe.duration, 3),
                "width": probe.width,
                "height": probe.height,
            }
        )
    (ctx.media_dir / NO_FORMAT_MARKER).unlink(missing_ok=True)
    write_json_atomic(ctx.media_dir / "sections.json", {"clips": entries})


STAGE = StageDef(fetch_clips, "sections.json")
