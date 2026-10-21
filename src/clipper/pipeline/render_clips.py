"""RENDERING stage (CLP-63): turn each fetched section into a `pending_review` clip.

Input: `sections.json` (CLP-16), `highlights.json` (CLP-15) and `transcript.json` (CLP-14)
in the job's media dir. For each section the EDL cuts the requested range out of the padded
`clips/<rank>/source.mp4` (`[pad_start_s, pad_start_s + length]`), with the highlight's hook
and the transcript words that overlap the clip, shifted into the section's timeline.
`clipper.render.render_clip` writes `clips/<rank>/v1.{mp4,jpg,json}` and
`clips_repo.insert_pending_clip` records it with the highlight's title, caption, hashtags,
score and reason.

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
from typing import Any

from clipper.clips_repo import ClipMeta, insert_pending_clip
from clipper.edl import EDL, parse_edl
from clipper.jobs.models import Job
from clipper.jobs.stages import PermanentError, StageContext, StageDef
from clipper.pipeline.common import (
    STAGE_BUDGET_S,
    Deadline,
    StageTimeout,
    read_json,
    strip_control,
)
from clipper.redact import redact
from clipper.render import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    DEFAULT_RENDER_TIMEOUT_S,
    InvalidClipError,
    RenderError,
    render_clip,
)

logger = logging.getLogger(__name__)

MAX_WORD_CHARS = 64
"""EDL limit for one caption word."""
CLIP_WORST_CASE_S = DEFAULT_RENDER_TIMEOUT_S + 4 * DEFAULT_FFMPEG_TIMEOUT_S
"""Remotion plus the four ffmpeg steps of `render_clip`, each at its timeout."""
assert CLIP_WORST_CASE_S < STAGE_BUDGET_S / 2, "a stage run must fit at least two clips"
MAX_REASON_CHARS = 300


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
    section: Any, highlights: dict[Any, Any], transcript: Any, prompt_version: str | None
) -> tuple[int, EDL, float, float, ClipMeta]:
    """(rank, EDL, source start, source end, meta) for one section; PermanentError if invalid."""
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
        edl = parse_edl(
            {
                "source": {
                    "path": section["path"],
                    "in_s": pad,
                    "out_s": round(pad + end - start, 3),
                },
                # An empty hook (also after cleaning) means no hook card.
                "hook": {"text": hook} if (hook := strip_control(clip.get("hook") or "")) else None,
                "words": _words(transcript, start - pad, start, end),
            }
        )
    # Malformed artifact entries, or an EDL pydantic rejects (ValidationError is a ValueError).
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"clip {rank}: invalid EDL: {exc}") from None
    meta = ClipMeta(
        title=clip.get("title"),
        caption=clip.get("caption"),
        hashtags=tuple(hashtags),
        score=score,
        reason=clip.get("reason"),
        prompt_version=prompt_version,
    )
    return rank, edl, start, end, meta


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
            result = render_clip(
                edl,
                ctx.media_dir,
                ctx.media_dir / "clips" / str(rank),
                basename="v1",  # a new clip is version 1
                timeout_s=deadline.timeout(DEFAULT_RENDER_TIMEOUT_S),
                ffmpeg_timeout_s=deadline.timeout(DEFAULT_FFMPEG_TIMEOUT_S),
            )
        except PermanentError as exc:
            invalid.append(strip_control(str(exc))[:MAX_REASON_CHARS])
            continue
        except InvalidClipError as exc:
            invalid.append(f"clip {rank}: {str(exc).splitlines()[0][:MAX_REASON_CHARS]}")
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
        summary = redact("; ".join(invalid))
        logger.warning("job %s: %d clip(s) left out: %s", job.id, len(invalid), summary)


STAGE = StageDef(render_clips)
