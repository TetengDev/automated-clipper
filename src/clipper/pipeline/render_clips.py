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
clip's position in the source video (the review page links there), not in the section.

A render failure or timeout (`RenderError`) is retried with the job. A clip whose EDL is
invalid is logged and left out; the others still render. If no clip of the job ends up
rendered, the job fails.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from clipper.clips_repo import ClipMeta, insert_pending_clip
from clipper.edl import EDL, parse_edl
from clipper.jobs.models import Job
from clipper.jobs.stages import PermanentError, StageContext, StageDef
from clipper.pipeline.common import Deadline, read_json
from clipper.render import DEFAULT_RENDER_TIMEOUT_S, render_clip

logger = logging.getLogger(__name__)

MAX_WORD_CHARS = 64
"""EDL limit for one caption word."""


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
        text = str(w["word"]).strip()[:MAX_WORD_CHARS]
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
    try:
        start, end = section["requested_start_s"], section["requested_end_s"]
        pad = section["pad_start_s"]
        edl = parse_edl(
            {
                "source": {
                    "path": section["path"],
                    "in_s": pad,
                    "out_s": round(pad + end - start, 3),
                },
                "hook": {"text": clip["hook"]} if clip.get("hook") else None,
                "words": _words(transcript, start - pad, start, end),
            }
        )
        meta = ClipMeta(
            title=clip.get("title"),
            caption=clip.get("caption"),
            hashtags=tuple(clip.get("hashtags") or ()),
            score=clip.get("score"),
            reason=clip.get("reason"),
            prompt_version=prompt_version,
        )
    # Malformed artifact entries, or an EDL pydantic rejects (ValidationError is a ValueError).
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"clip {rank}: invalid EDL: {exc}") from None
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
    for section in sections:
        try:
            rank, edl, start, end, meta = _plan(
                section, highlights, transcript, selection.get("prompt_version")
            )
        except PermanentError as exc:
            logger.warning("job %s: clip left out: %s", job.id, exc)
            invalid.append(str(exc))
            continue
        if rank in done:
            continue
        result = render_clip(
            edl,
            ctx.media_dir,
            ctx.media_dir / "clips" / str(rank),
            basename="v1",  # a new clip is version 1
            timeout_s=deadline.timeout(DEFAULT_RENDER_TIMEOUT_S),
        )
        # Store the clip's position in the source video, not in the padded section file.
        source = edl.source.model_copy(update={"in_s": start, "out_s": end})
        insert_pending_clip(
            ctx.conn,
            job_id=job.id,
            rank=rank,
            edl=edl.model_copy(update={"source": source}),
            result=result,
            meta=meta,
        )
        done.add(rank)
    if not done:
        raise PermanentError("no clip could be rendered: " + "; ".join(invalid))


STAGE = StageDef(render_clips)
