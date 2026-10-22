"""Persist rendered clips: `clips` + `clip_versions` rows (schema: migrations/0001_initial.sql).

A freshly rendered clip is version 1 with status `pending_review`; a re-render (reviewer
edit / EDL adjustment) adds version n+1 and puts the clip back into `pending_review`.

Each version must point at its own files: render with a per-version basename, e.g.
``render_clip(edl, media_root, clip_dir, basename=f"v{version}")``, so rendering v2 never
overwrites the video/thumbnail an earlier clip_versions row (and its reviews) refer to.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from clipper.db import transaction
from clipper.edl import EDL
from clipper.render import RenderResult


class ClipStatus(StrEnum):
    PENDING_REVIEW = "pending_review"


@dataclass(frozen=True)
class ClipVersionRef:
    clip_id: int
    version: int


@dataclass(frozen=True)
class ClipMeta:
    """Copy/selection metadata stored alongside a rendered version."""

    title: str | None = None
    caption: str | None = None
    hashtags: Sequence[str] = ()
    score: float | None = None
    reason: str | None = None
    prompt_version: str | None = None
    playbook_version: str | None = None
    note: str | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_version(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    version: int,
    edl: EDL,
    result: RenderResult,
    meta: ClipMeta,
    now: str,
    start_s: float | None = None,
    end_s: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO clip_versions (clip_id, version, start_s, end_s, title, caption, hashtags,"
        " score, reason, captions_enabled, note, video_path, thumb_path, prompt_version,"
        " playbook_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            version,
            edl.source.in_s if start_s is None else start_s,
            edl.source.out_s if end_s is None else end_s,
            meta.title,
            meta.caption,
            json.dumps(list(meta.hashtags)),
            meta.score,
            meta.reason,
            int(edl.captions_enabled),
            meta.note,
            str(result.video_path),
            str(result.thumbnail_path),
            meta.prompt_version,
            meta.playbook_version,
            now,
        ),
    )


def insert_pending_clip(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    rank: int,
    edl: EDL,
    result: RenderResult,
    meta: ClipMeta | None = None,
    now: str | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> ClipVersionRef:
    """Insert a new clip (version 1, `pending_review`). Raises sqlite3.IntegrityError if
    (job_id, rank) already exists or job_id is unknown.

    `start_s`/`end_s` (default: the EDL's source range) are stored as the clip's position
    in the original video, for when the EDL cuts a pre-fetched section of it."""
    stamp = now or _now()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO clips (job_id, rank, status, current_version, created_at, updated_at)"
            " VALUES (?, ?, ?, 1, ?, ?)",
            (job_id, rank, ClipStatus.PENDING_REVIEW.value, stamp, stamp),
        )
        clip_id = cursor.lastrowid
        assert clip_id is not None
        _insert_version(
            conn,
            clip_id=clip_id,
            version=1,
            edl=edl,
            result=result,
            meta=meta or ClipMeta(),
            now=stamp,
            start_s=start_s,
            end_s=end_s,
        )
    return ClipVersionRef(clip_id=clip_id, version=1)


def add_clip_version(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    edl: EDL,
    result: RenderResult,
    meta: ClipMeta | None = None,
    now: str | None = None,
) -> ClipVersionRef:
    """Record a re-render as version n+1 and return the clip to `pending_review`.

    `result` must come from a render with a version-specific basename (see module doc);
    earlier version rows keep their own video/thumbnail paths.
    """
    stamp = now or _now()
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM clip_versions WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        exists = conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if exists is None:
            raise KeyError(f"clip {clip_id} not found")
        version = int(row[0]) + 1
        _insert_version(
            conn,
            clip_id=clip_id,
            version=version,
            edl=edl,
            result=result,
            meta=meta or ClipMeta(),
            now=stamp,
        )
        conn.execute(
            "UPDATE clips SET current_version = ?, status = ?, updated_at = ? WHERE id = ?",
            (version, ClipStatus.PENDING_REVIEW.value, stamp, clip_id),
        )
    return ClipVersionRef(clip_id=clip_id, version=version)
