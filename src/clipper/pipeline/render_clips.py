"""RENDERING stage (CLP-63): turn each fetched section into a `pending_review` clip.

Input: `sections.json` (CLP-16), `highlights.json` (CLP-15) and `transcript.json` (CLP-14)
in the job's media dir. For each section the EDL cuts the requested range out of the padded
`clips/<rank>/source.mp4` (`[pad_start_s, pad_start_s + length]`), with the highlight's hook
and the transcript words that overlap the clip, shifted into the section's timeline.
`clipper.render.render_clip` writes `clips/<rank>/v1.{mp4,jpg,json}` and
`clips_repo.insert_pending_clip` records it with the highlight's title, caption, hashtags,
score and reason. Before the EDL is final, dead air is trimmed and the clip edges are
snapped to scene cuts (CLP-68, `clipper.pipeline.boundaries`); a failed or skipped scene
detection (sections over MAX_DETECT_PIXELS) leaves only the trim. Then, unless the
creator's `jump_cuts` is off, silences (and in `full` stutters and fillers) inside the
clip are cut and the EDL gets v2 segments plus the cut records (CLP-73,
`clipper.pipeline.jumpcuts`); a failed silencedetect makes no silence cuts. Then, unless
the creator's `reframe` is blurred_fill, faces are detected on the section and a clip with
faces in most frames gets a face-tracked 9:16 crop that jumps at the same scene cuts
(CLP-58, `clipper.pipeline.reframe`), on the output timeline (a jump at each join too);
any failure keeps the blurred fill.

There is no artifact file: the `clips` row (UNIQUE (job_id, rank)) marks a clip done, so a
rerun after a crash renders only the clips without a row. The row's start_s/end_s are the
clip's position in the source video (the review page links there), not in the section; a
re-render from `clips/<rank>/source.mp4` must convert back by subtracting
`requested_start_s - pad_start_s` (sections.json).

A transient render failure or timeout (`RenderError`) does not stop the other clips; the
first one is re-raised at the end so the job retries just the failed ranks. A clip with
bad input (invalid EDL or metadata, `InvalidClipError` from the renderer) is left out and
the others still render; the skipped ranks are logged once. If no clip of the job ends up
rendered, the job fails. A clip is only started if the stage budget still covers its worst
case (CLIP_WORST_CASE_S), so a run never outlives the claim lease; else `StageTimeout`
(retryable) and the next claim resumes.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

from clipper.clips_repo import ClipMeta, insert_pending_clip
from clipper.config import JumpCuts
from clipper.edl import EDL, FPS, MAX_CLIP_SECONDS, frames, parse_edl
from clipper.jobs.models import Job
from clipper.jobs.stages import PermanentError, StageContext, StageDef
from clipper.pipeline.boundaries import DETECT_TIMEOUT_S, detect_cuts, polish
from clipper.pipeline.common import (
    STAGE_BUDGET_S,
    Deadline,
    StageTimeout,
    read_json,
    settings_for,
    strip_control,
)
from clipper.pipeline.jumpcuts import (
    SILENCE_TIMEOUT_S,
    crop_on_output,
    cut_clip,
    detect_silences,
    plan_cuts,
)
from clipper.pipeline.reframe import (
    FACE_TIMEOUT_S,
    MODEL_TIMEOUT_S,
    detect_faces,
    ensure_model,
    plan_reframe,
)
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
)
"""Scene detection, silencedetect, the face model fetch, face detection, Remotion and the
four ffmpeg steps of `render_clip`, each at its timeout."""
assert CLIP_WORST_CASE_S < STAGE_BUDGET_S / 2, "a stage run must fit at least two clips"
MAX_REASON_CHARS = 300
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
    prompt_version: str | None,
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
    if (
        any(t is not None and not isinstance(t, str) for t in texts)
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
        prompt_version=prompt_version,
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


def _rejected(edl: EDL, what: str, fallback: str, exc: ValueError) -> EDL:
    """`edl` unchanged, with a warning that the EDL with `what` was invalid."""
    reason = redact(strip_control(str(exc).splitlines()[0]))[:MAX_REASON_CHARS]
    logger.warning("clip source %s: %s rejected, %s: %s", edl.source.path, what, fallback, reason)
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
        return _rejected(edl, "jump cuts", "no cuts", exc)


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
    invalid: list[str] = []
    retry: RenderError | None = None
    for section in sections:
        try:
            rank, edl, start, end, meta = _plan(
                section, highlights, transcript, selection.get("prompt_version")
            )
            if rank in done:
                continue
            if deadline.remaining() < CLIP_WORST_CASE_S:
                raise StageTimeout(f"stage budget left too small for clip {rank}; resuming later")
            cuts = _cuts(edl, section, ctx.media_dir, deadline)
            bounds = _polish(edl, start, section, transcript, limits, cuts)
            try:
                rank, edl, start, end, meta = _plan(
                    section, highlights, transcript, selection.get("prompt_version"), bounds
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
        except PermanentError as exc:
            invalid.append(redact(strip_control(str(exc)))[:MAX_REASON_CHARS])
            continue
        except InvalidClipError as exc:
            reason = redact(strip_control(str(exc).splitlines()[0]))
            invalid.append(f"clip {rank}: {reason}"[:MAX_REASON_CHARS])
            continue
        except RenderError as exc:
            # Possibly transient: render the remaining clips, then retry the job for this one.
            reason = redact(str(exc).splitlines()[0])
            logger.warning("job %s: clip %s failed, will retry: %s", job.id, rank, reason)
            retry = retry or exc
            continue
        insert_pending_clip(
            ctx.conn,
            job_id=job.id,
            rank=rank,
            edl=edl,
            result=result,
            meta=meta,
            start_s=start,  # position in the source video, not in the padded section file
            end_s=end,
        )
        done.add(rank)
    if retry is not None:
        raise retry
    if not done:
        raise PermanentError("no clip could be rendered: " + "; ".join(invalid))
    if invalid:
        summary = "; ".join(invalid)  # each reason redacted before it was cut
        logger.warning("job %s: %d clip(s) left out: %s", job.id, len(invalid), summary)


STAGE = StageDef(render_clips)
