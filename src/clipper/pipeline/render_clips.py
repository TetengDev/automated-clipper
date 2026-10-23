"""RENDERING stage (CLP-63): turn each fetched section into a `pending_review` clip.

Input: `sections.json` (CLP-16), `highlights.json` (CLP-15) and `transcript.json` (CLP-14)
in the job's media dir. For each section the EDL cuts the requested range out of the padded
`clips/<rank>/source.mp4` (`[pad_start_s, pad_start_s + length]`), with the highlight's hook
and the transcript words that overlap the clip, shifted into the section's timeline.
`clipper.render.render_clip` writes `clips/<rank>/v1.{mp4,jpg,json}` and
`clips_repo.insert_pending_clip` records it with the highlight's title, caption, hashtags,
score, reason, and the prompt and playbook versions it was selected with. Before the EDL
is final, dead air is trimmed and the clip edges are snapped to scene cuts (CLP-68,
`clipper.pipeline.boundaries`); a failed or skipped scene detection (sections over
MAX_DETECT_PIXELS) leaves only the trim. Then, unless the
creator's `jump_cuts` is off, silences (and in `full` stutters and fillers) inside the
clip are cut and the EDL gets v2 segments plus the cut records (CLP-73,
`clipper.pipeline.jumpcuts`); a failed silencedetect makes no silence cuts. Then, unless
the creator's `reframe` is blurred_fill, faces are detected on the section and a clip with
faces in most frames gets a face-tracked 9:16 crop that jumps at the same scene cuts
(CLP-58, `clipper.pipeline.reframe`), on the output timeline (a jump at each join too);
any failure keeps the blurred fill. The rendered video then runs the quality gates
(CLP-74, `clipper.pipeline.quality`): a failed gate stores the clip as `needs_attention`
with its reasons; a gate that cannot run is noted but never holds the clip back.

There is no artifact file: the `clips` row (UNIQUE (job_id, rank)) marks a clip done, so a
rerun after a crash renders only the clips without a row. The row's start_s/end_s are the
clip's position in the source video (the review page links there), not in the section; a
re-render from `clips/<rank>/source.mp4` must convert back by subtracting
`requested_start_s - pad_start_s` (sections.json).

A transient render failure or timeout (`RenderError`) does not stop the other clips; the
job retries just the failed ranks. Each failure is counted in
`clips/<rank>/render_failures.json` (CLP-65); at MAX_RENDER_FAILURES the clip is left out
like an invalid one, so one clip that always fails (a Remotion timeout, a corrupt or
missing section file) cannot use up the job's attempts. Clips that never failed are tried
first, then those closest to the limit, so failing clips at the front cannot spend every
run's budget before a good clip gets its turn. A clip with bad input (invalid EDL or
metadata, `InvalidClipError` from the renderer) is left out and the others still render;
the skipped ranks are logged once. If no clip of the job ends up rendered, the job fails
(and the failure counts are dropped, so a requeue tries again). A requeue of a job that
failed otherwise keeps the counts: delete `clips/<rank>/render_failures.json` to give a
left-out clip fresh tries.

A clip is only started if the stage budget still covers its worst case
(CLIP_WORST_CASE_S), so a run never outlives the claim lease; else `StageTimeout` and the
next claim resumes. A run uses up an attempt (StageTimeout, or the first RenderError
re-raised at the end) only when it made no progress: no clip rendered and no failure
counted (e.g. nothing fit the budget, or the marker could not be written). After progress
the stage is `Deferred`, so clip failures are paid for by their counts, not by the job's
attempts: to now after a timeout, after the queue's backoff for the highest failure count
after a render failure. That cannot loop: each clip is rendered once and counts at most
MAX_RENDER_FAILURES failures. Crashes still use up attempts (each claim is one).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from clipper.clips_repo import ClipMeta, insert_pending_clip
from clipper.config import JumpCuts
from clipper.edl import EDL, FPS, MAX_CLIP_SECONDS, frames, parse_edl
from clipper.jobs.models import Job
from clipper.jobs.queue import MAX_ATTEMPTS, backoff
from clipper.jobs.stages import Deferred, PermanentError, StageContext, StageDef
from clipper.pipeline.boundaries import DETECT_TIMEOUT_S, detect_cuts, polish
from clipper.pipeline.common import (
    STAGE_BUDGET_S,
    Deadline,
    StageTimeout,
    read_json,
    settings_for,
    strip_control,
    write_json_atomic,
)
from clipper.pipeline.jumpcuts import (
    SILENCE_TIMEOUT_S,
    crop_on_output,
    cut_clip,
    detect_silences,
    plan_cuts,
)
from clipper.pipeline.quality import GATES_TIMEOUT_S, check_clip, default_asr
from clipper.pipeline.reframe import (
    FACE_TIMEOUT_S,
    MODEL_TIMEOUT_S,
    detect_faces,
    ensure_model,
    plan_reframe,
)
from clipper.pipeline.transcribe import creator_language
from clipper.redact import redact
from clipper.render import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    DEFAULT_RENDER_TIMEOUT_S,
    InvalidClipError,
    RenderError,
    render_clip,
    resolve_source,
)

logger = logging.getLogger(__name__)

MAX_WORD_CHARS = 64
"""EDL limit for one caption word."""
CLIP_WORST_CASE_S = (
    DETECT_TIMEOUT_S
    + SILENCE_TIMEOUT_S
    + MODEL_TIMEOUT_S
    + FACE_TIMEOUT_S
    + DEFAULT_RENDER_TIMEOUT_S
    + 4 * DEFAULT_FFMPEG_TIMEOUT_S
    + GATES_TIMEOUT_S
)
"""Scene detection, silencedetect, the face model fetch, face detection, Remotion, the
four ffmpeg steps of `render_clip` and the quality gates, each at its timeout."""
assert CLIP_WORST_CASE_S < STAGE_BUDGET_S / 2, "a stage run must fit at least two clips"
MAX_REASON_CHARS = 300
MAX_RENDER_FAILURES = 3
"""Render failures of one clip before it is left out: two retries for a transient error,
and fewer than the job's attempts, so the job never runs out of them on one clip."""
assert 1 < MAX_RENDER_FAILURES < MAX_ATTEMPTS
FAILURES_MARKER = "render_failures.json"
"""In `clips/<rank>/`: {"failures": count, "error": last reason}; removed once rendered."""
MAX_MARKER_BYTES = 4096
"""A larger failure marker is not ours: it counts as no failures."""
_VERSION = re.compile(r"^[\w.-]{1,64}$")


def _version(value: Any) -> str | None:
    """A version label from highlights.json; anything else (stale or hand-edited) is dropped."""
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


MAX_DETECT_PIXELS = 4096 * 2160
"""Larger sections are not scene-detected (decoder cost and attack surface)."""


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _clips(data: Any, name: str) -> list[Any]:
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list):
        raise PermanentError(f"{name} has no clips list")
    return clips


def _words(transcript: Any, offset: float, start: float, end: float) -> list[dict[str, Any]]:
    """Transcript words overlapping [start, end], in the section's timeline (minus offset)."""
    words = []
    for w in transcript.get("words") or ():
        if w["end"] <= start or w["start"] >= end:
            continue
        text = strip_control(str(w["word"]))[:MAX_WORD_CHARS]
        if text:
            begin = round(max(w["start"] - offset, 0.0), 3)
            words.append(
                {"text": text, "start_s": begin, "end_s": max(begin, round(w["end"] - offset, 3))}
            )
    return words


def _plan(
    section: Any,
    highlights: dict[Any, Any],
    transcript: Any,
    selection: dict[str, Any],
    bounds: tuple[float, float] | None = None,
) -> tuple[int, EDL, float, float, ClipMeta]:
    """(rank, EDL, source start, source end, meta) for one section; PermanentError if invalid.
    `bounds` overrides the cut inside the section file (seconds, from `_polish`)."""
    rank = section.get("rank") if isinstance(section, dict) else None
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise PermanentError(f"invalid clip rank: {rank!r}")
    clip = highlights.get(rank)
    if not isinstance(clip, dict):
        raise PermanentError(f"clip {rank}: no highlight")
    texts = [clip.get(k) for k in ("title", "caption", "reason", "hook")]
    score, hashtags = clip.get("score"), clip.get("hashtags", [])
    sensitive = clip.get("sensitive", False)  # absent in v1 highlights
    if (
        not isinstance(sensitive, bool)
        or any(t is not None and not isinstance(t, str) for t in texts)
        or not (score is None or _number(score))
        or not isinstance(hashtags, list)
        or not all(isinstance(t, str) for t in hashtags)
    ):
        raise PermanentError(f"clip {rank}: invalid metadata")
    start, end = section.get("requested_start_s"), section.get("requested_end_s")
    if not (_number(start) and _number(end) and 0 <= start < end):
        raise PermanentError(f"clip {rank}: invalid range")
    try:
        pad = section["pad_start_s"]
        offset = start - pad  # source time = section time + offset
        in_s, out_s = bounds or (pad, round(pad + end - start, 3))
        if bounds:
            start, end = round(in_s + offset, 3), round(out_s + offset, 3)
        edl = parse_edl(
            {
                "source": {"path": section["path"], "in_s": in_s, "out_s": out_s},
                # An empty hook (also after cleaning) means no hook card.
                "hook": {"text": hook} if (hook := strip_control(clip.get("hook") or "")) else None,
                "words": _words(transcript, offset, start, end),
            }
        )
    # Malformed artifact entries, or an EDL pydantic rejects (ValidationError is a ValueError).
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"clip {rank}: invalid EDL: {exc}") from None
    # Cleaned again here: highlights.json may be stale or hand-edited.
    title, caption, reason = (None if t is None else strip_control(t) for t in texts[:3])
    tags = (strip_control(h).replace(" ", "") for h in hashtags)
    meta = ClipMeta(
        title=title,
        caption=caption,
        hashtags=tuple(t for t in tags if t),
        score=score,
        reason=reason,
        prompt_version=_version(selection.get("prompt_version")),
        playbook_version=_version(selection.get("playbook_version")),
        sensitive=sensitive,
    )
    return rank, edl, start, end, meta


def _detectable(section: dict[str, Any]) -> bool:
    width, height = section.get("width"), section.get("height")
    return _number(width) and _number(height) and 0 < width * height <= MAX_DETECT_PIXELS


def _cuts(edl: EDL, section: dict[str, Any], media_dir: Path, deadline: Deadline) -> list[float]:
    if _detectable(section):
        return detect_cuts(resolve_source(edl, media_dir), deadline)
    width, height = section.get("width"), section.get("height")
    logger.info("clip source %s: %sx%s, detection skipped", edl.source.path, width, height)
    return []


def _polish(
    edl: EDL,
    start: float,
    section: dict[str, Any],
    transcript: Any,
    limits: tuple[float, float],
    cuts: list[float],
) -> tuple[float, float]:
    """Clip edges in the section file after the dead-air trim and the scene-cut snap."""
    src = edl.source
    offset = start - src.in_s
    # Without a valid duration_s the file is only known to cover the requested range.
    size = section.get("duration_s")
    span = (0.0, size if _number(size) and size >= src.out_s else src.out_s)
    words = [
        (w["start_s"], w["end_s"]) for w in _words(transcript, offset, offset, offset + span[1])
    ]
    a, b = polish(src.in_s, src.out_s, cuts, words, *limits, section=span)
    # Floor, so an end just before a cut never rounds up onto the next shot's first frame.
    return round(a, 3), math.floor(round(b * 1000, 6)) / 1000


def _rejected(edl: EDL, what: str, fallback: str, exc: ValueError, *, trace: bool = False) -> EDL:
    """`edl` unchanged, with a warning that the EDL with `what` was invalid (with the
    traceback if `trace`, so a logic bug cannot hide behind the fallback)."""
    reason = redact(strip_control(str(exc).splitlines()[0]))[:MAX_REASON_CHARS]
    logger.warning(
        "clip source %s: %s rejected, %s: %s", edl.source.path, what, fallback, reason,
        exc_info=trace,
    )  # fmt: skip
    return edl


def _jump_cut(edl: EDL, mode: JumpCuts, min_s: float, media_dir: Path, deadline: Deadline) -> EDL:
    """The EDL with the jump cuts of `mode` (v2 segments and cut records), never shorter
    than `min_s` through them; as is when nothing is cut or anything is invalid."""
    silences = detect_silences(resolve_source(edl, media_dir), deadline) or []
    try:
        cuts = plan_cuts(edl.words, silences, mode)
        if not cuts:
            return edl
        cuts, segments = cut_clip((edl.source.in_s, edl.source.out_s), cuts, min_s)
        props = {**edl.to_props(), "cuts": [c.model_dump(mode="json") for c in cuts]}
        if len(segments) > 1:
            source = {**props["source"], "in_s": segments[0][0], "out_s": segments[-1][1]}
            props["source"] = source
            props["segments"] = [{"in_s": a, "out_s": b} for a, b in segments]
        return parse_edl(props)
    except ValueError as exc:  # pydantic's ValidationError too
        return _rejected(edl, "jump cuts", "no cuts", exc, trace=True)


def _reframe(edl: EDL, cuts: list[float], media_dir: Path, deadline: Deadline) -> EDL:
    """The EDL with a face-tracked crop when faces are found; else (or on any failure) as is."""
    model = ensure_model(timeout_s=deadline.timeout(MODEL_TIMEOUT_S))
    faces = detect_faces(resolve_source(edl, media_dir), model, deadline) if model else None
    if not faces:
        return edl
    layout, crop = plan_reframe(faces, cuts, edl.source.in_s, edl.source.out_s)
    if crop is None:
        return edl
    if edl.segments is not None:  # keys from the span's first frame, as plan_reframe has them
        crop = crop_on_output(crop, edl.spans, frames(edl.source.in_s) / FPS)
    keys = [{"t_s": t, "x_center": x} for t, x in crop]
    try:
        return parse_edl({**edl.to_props(), "layout": layout.value, "crop": keys})
    except ValueError as exc:  # pydantic's ValidationError
        return _rejected(edl, "face track", "blurred fill", exc)


def _done(conn: sqlite3.Connection, job_id: int) -> set[int]:
    return {row[0] for row in conn.execute("SELECT rank FROM clips WHERE job_id = ?", (job_id,))}


def _marker(media_dir: Path, rank: int) -> Path:
    return media_dir / "clips" / str(rank) / FAILURES_MARKER


def _failures(marker: Path) -> tuple[int, str]:
    """(count, last reason) of the clip's earlier render failures; none if the marker is
    missing, not a regular file (a symlink is never followed), over MAX_MARKER_BYTES or
    malformed."""
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return 0, ""
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return 0, ""
            raw = fh.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            return 0, ""
        data = json.loads(raw)
        count, error = data["failures"], data["error"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 0, ""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0, ""
    if not isinstance(error, str):
        return 0, ""
    # Cleaned again: the file may be hand-edited.
    return count, redact(strip_control(error[:MAX_REASON_CHARS]))


def _prior_failures(section: Any, media_dir: Path) -> int:
    rank = section.get("rank") if isinstance(section, dict) else None
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        return 0  # _plan reports it as invalid
    return _failures(_marker(media_dir, rank))[0]


def _try_order(failures: int) -> tuple[bool, int]:
    """Clips never failed first (a run that still has a good clip renders it before its
    budget is spent on failing ones), then those closest to being left out."""
    return failures > 0, -failures


def _record_failure(marker: Path, failures: int, reason: str) -> bool:
    """Write the clip's failure count; False (not counted) if its directory or `clips/` is
    a symlink: a planted link must not redirect the write."""
    clip_dir = marker.parent
    if clip_dir.is_symlink() or clip_dir.parent.is_symlink():
        logger.warning("%s is a symlink; not counting the failure", clip_dir)
        return False
    clip_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(marker, {"failures": failures, "error": reason})
    return True


def _left_out(rank: int, failures: int, error: str) -> str:
    return f"clip {rank}: render failed {failures} times: {error}"[:MAX_REASON_CHARS]


def render_clips(job: Job, ctx: StageContext) -> None:
    deadline = Deadline()
    sections = _clips(read_json(ctx.media_dir / "sections.json"), "sections.json")
    selection = read_json(ctx.media_dir / "highlights.json")
    highlights = {
        c.get("rank"): c for c in _clips(selection, "highlights.json") if isinstance(c, dict)
    }
    transcript = read_json(ctx.media_dir / "transcript.json")
    done = _done(ctx.conn, job.id)
    settings = settings_for(job, ctx.config)
    limits = (settings.clip_min_seconds, min(settings.clip_max_seconds, MAX_CLIP_SECONDS))
    language = creator_language(job, ctx)
    asr = default_asr(cpu_threads=settings.whisper_cpu_threads)
    invalid: list[str] = []
    left_out: list[Path] = []  # failure markers of the clips left out after repeated failures
    retry: RenderError | None = None
    # Progress this run: new clips, and failures counted in a marker (at most
    # MAX_RENDER_FAILURES per clip). Both are bounded, so runs that make progress are too.
    rendered = counted = worst = 0  # worst: the highest failure count recorded this run
    order = sorted(sections, key=lambda s: _try_order(_prior_failures(s, ctx.media_dir)))
    for section in order:
        try:
            rank, edl, start, end, meta = _plan(section, highlights, transcript, selection)
            if rank in done:
                continue
            marker = _marker(ctx.media_dir, rank)
            failures, error = _failures(marker)
            if failures >= MAX_RENDER_FAILURES:
                invalid.append(_left_out(rank, failures, error))
                left_out.append(marker)
                continue
            if deadline.remaining() < CLIP_WORST_CASE_S:
                raise StageTimeout(f"stage budget left too small for clip {rank}; resuming later")
            cuts = _cuts(edl, section, ctx.media_dir, deadline)
            bounds = _polish(edl, start, section, transcript, limits, cuts)
            try:
                rank, edl, start, end, meta = _plan(
                    section, highlights, transcript, selection, bounds
                )
            except PermanentError as exc:  # e.g. rounding nudged it past an EDL limit
                reason = redact(strip_control(str(exc)))[:MAX_REASON_CHARS]
                logger.warning("%s; rendering the requested range", reason)
            if settings.jump_cuts != "off":
                shift = start - edl.source.in_s  # source time = section time + shift
                edl = _jump_cut(edl, settings.jump_cuts, limits[0], ctx.media_dir, deadline)
                start = round(edl.source.in_s + shift, 3)
                end = round(edl.source.out_s + shift, 3)
            if settings.reframe == "auto" and _detectable(section):
                edl = _reframe(edl, cuts, ctx.media_dir, deadline)
            result = render_clip(
                edl,
                ctx.media_dir,
                ctx.media_dir / "clips" / str(rank),
                basename="v1",  # a new clip is version 1
                timeout_s=deadline.timeout(DEFAULT_RENDER_TIMEOUT_S),
                ffmpeg_timeout_s=deadline.timeout(DEFAULT_FFMPEG_TIMEOUT_S),
            )
            reasons = check_clip(
                edl,
                result.video_path,
                language=language,
                asr=asr,
                deadline=deadline,
                starts_on_cut=any(abs(c - edl.source.in_s) <= 1 / FPS for c in cuts),
            )
        except PermanentError as exc:
            invalid.append(redact(strip_control(str(exc)))[:MAX_REASON_CHARS])
            continue
        except InvalidClipError as exc:
            reason = redact(strip_control(str(exc).splitlines()[0]))
            invalid.append(f"clip {rank}: {reason}"[:MAX_REASON_CHARS])
            continue
        except RenderError as exc:
            # Possibly transient: render the remaining clips, then retry the job for this
            # one; unless it failed too often, then it is left out like an invalid clip.
            reason = redact(strip_control(str(exc).splitlines()[0]))[:MAX_REASON_CHARS]
            failures += 1
            if _record_failure(marker, failures, reason):
                counted += 1
                worst = max(worst, failures)
            if failures >= MAX_RENDER_FAILURES:
                logger.warning(
                    "job %s: clip %s failed %d times, leaving it out: %s",
                    job.id, rank, failures, reason,
                )  # fmt: skip
                invalid.append(_left_out(rank, failures, reason))
                left_out.append(marker)
                continue
            logger.warning("job %s: clip %s failed, will retry: %s", job.id, rank, reason)
            retry = retry or exc
            continue
        except StageTimeout as exc:  # out of budget before or inside a clip (the gates too)
            if not (rendered or counted):
                raise  # no progress: this run uses up an attempt
            # Resume right away, without using up an attempt.
            note = f"{rendered} clip(s) rendered, {counted} failure(s) counted"
            raise Deferred(ctx.clock(), f"{exc} ({note})") from None
        if reasons:
            logger.info("job %s: clip %s quality gates: %s", job.id, rank, ", ".join(reasons))
        insert_pending_clip(
            ctx.conn,
            job_id=job.id,
            rank=rank,
            edl=edl,
            result=result,
            meta=replace(meta, gate_reasons=tuple(reasons)),
            start_s=start,  # position in the source video, not in the padded section file
            end_s=end,
        )
        done.add(rank)
        rendered += 1
        # A marker left on a done clip (crash before this, or not removable) is ignored.
        with contextlib.suppress(OSError):
            marker.unlink(missing_ok=True)
    if retry is not None:
        if not (rendered or counted):
            raise retry  # no progress (the failure could not be counted): uses up an attempt
        # Retry with the usual backoff for the clip's failure count, without an attempt.
        reason = redact(strip_control(str(retry).splitlines()[0]))[:MAX_REASON_CHARS]
        note = f"{rendered} clip(s) rendered, {counted} failure(s) counted"
        raise Deferred(ctx.clock() + backoff(max(worst, 1)), f"{reason} ({note})")
    if not done:
        for path in left_out:  # a requeued job tries these clips afresh
            path.unlink(missing_ok=True)
        raise PermanentError("no clip could be rendered: " + "; ".join(invalid))
    if invalid:
        summary = "; ".join(invalid)  # each reason redacted before it was cut
        logger.warning("job %s: %d clip(s) left out: %s", job.id, len(invalid), summary)


STAGE = StageDef(render_clips)
