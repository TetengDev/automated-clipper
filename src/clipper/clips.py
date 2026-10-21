"""Clip review data access: queue queries, clip detail, state transitions, audit rows.

State transitions are validated here (statuses are plain TEXT in the schema). Every
transition runs inside BEGIN IMMEDIATE and is conditional on the current status, so a
double-submitted action changes state (and writes its audit row) at most once.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from clipper.db import transaction
from clipper.jobs.models import to_iso
from clipper.urls import InvalidSourceURL, SourceRef, canonical_url

PAGE_SIZE = 24


class ClipStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    EXPORTED = "exported"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    RE_RENDERING = "re_rendering"
    ARCHIVED = "archived"


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EXPORT = "export"
    EXPORT_FAILED = "export_failed"


REJECT_REASONS: tuple[str, ...] = (
    "boring",
    "bad_cut",
    "bad_captions",
    "duplicate",
    "unsafe",
    "other",
)

# Allowed moves for the review actions handled here (edit/regenerate come later).
TRANSITIONS: dict[ClipStatus, frozenset[ClipStatus]] = {
    ClipStatus.PENDING_REVIEW: frozenset({ClipStatus.APPROVED, ClipStatus.REJECTED}),
    ClipStatus.APPROVED: frozenset({ClipStatus.EXPORTED}),
}

Sort = Literal["newest", "score"]


class InvalidTransition(Exception):
    """The clip is not in a state that allows the requested action."""

    def __init__(self, clip_id: int, current: str, target: ClipStatus) -> None:
        super().__init__(f"clip {clip_id}: cannot move from {current} to {target}")
        self.current = current


def can_transition(current: str, target: ClipStatus) -> bool:
    try:
        return target in TRANSITIONS.get(ClipStatus(current), frozenset())
    except ValueError:
        return False


@dataclass(frozen=True)
class QueueFilter:
    status: ClipStatus | None = ClipStatus.PENDING_REVIEW  # None: every status
    creator: str | None = None
    min_score: float | None = None
    sort: Sort = "newest"


@dataclass(frozen=True)
class ClipCard:
    id: int
    status: str
    version: int
    title: str | None
    creator: str
    score: float | None
    duration_s: float
    source_title: str | None
    has_thumb: bool
    created_at: str


@dataclass(frozen=True)
class ClipVersion:
    version: int
    start_s: float
    end_s: float
    title: str | None
    caption: str | None
    hashtags: tuple[str, ...]
    score: float | None
    reason: str | None
    video_path: str | None
    thumb_path: str | None
    prompt_version: str | None
    note: str | None
    created_at: str

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ClipVersion:
        return cls(
            version=row["version"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            title=row["title"],
            caption=row["caption"],
            hashtags=parse_hashtags(row["hashtags"]),
            score=row["score"],
            reason=row["reason"],
            video_path=row["video_path"],
            thumb_path=row["thumb_path"],
            prompt_version=row["prompt_version"],
            note=row["note"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class ClipDetail:
    id: int
    status: str
    current_version: int
    created_at: str
    creator: str
    platform: str
    item_id: str
    source_title: str | None
    current: ClipVersion
    versions: tuple[ClipVersion, ...]

    @property
    def source_url(self) -> str | None:
        try:
            return source_url_at(self.platform, self.item_id, self.current.start_s)
        except InvalidSourceURL:
            return None


def parse_hashtags(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    tags = (str(t).strip().lstrip("#") for t in value)
    return tuple(f"#{t}" for t in tags if t)


def resolve_media_path(media_root: Path, stored: str | None) -> Path | None:
    """The real file for a DB-stored media path, or None unless it lies inside `media_root`.

    Relative paths are taken against `media_root`; symlinks are resolved before the check,
    so neither `..` nor a link can escape the root.
    """
    if not stored or "\x00" in stored:
        return None
    root = media_root.resolve()
    candidate = Path(stored)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_relative_to(root) or not real.is_file():
        return None
    return real


def source_url_at(platform: str, item_id: str, start_s: float) -> str:
    """Canonical source URL (rebuilt from the validated id) that opens at `start_s`."""
    url = canonical_url(SourceRef(platform, item_id))  # type: ignore[arg-type]
    seconds = max(0, int(start_s))
    if platform == "youtube":
        return f"{url}&t={seconds}s"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{url}?t={hours}h{minutes}m{secs}s"


_CARD_SELECT = """
SELECT c.id, c.status, c.current_version, c.created_at, j.creator, j.title AS source_title,
       v.title, v.score, v.start_s, v.end_s, v.thumb_path
FROM clips c
JOIN jobs j ON j.id = c.job_id
JOIN clip_versions v ON v.clip_id = c.id AND v.version = c.current_version
"""


def _where(flt: QueueFilter, *, with_status: bool = True) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if with_status and flt.status is not None:
        clauses.append("c.status = ?")
        params.append(str(flt.status))
    if flt.creator:
        clauses.append("j.creator = ?")
        params.append(flt.creator)
    if flt.min_score is not None:
        clauses.append("v.score >= ?")
        params.append(flt.min_score)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_queue(
    conn: sqlite3.Connection, flt: QueueFilter, *, page: int = 1, page_size: int = PAGE_SIZE
) -> list[ClipCard]:
    where, params = _where(flt)
    order = (
        "v.score IS NULL, v.score DESC, c.created_at DESC, c.id DESC"
        if flt.sort == "score"
        else "c.created_at DESC, c.id DESC"
    )
    sql = f"{_CARD_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?"  # noqa: S608 - fixed fragments
    rows = conn.execute(sql, [*params, page_size, (max(page, 1) - 1) * page_size]).fetchall()
    return [
        ClipCard(
            id=r["id"],
            status=r["status"],
            version=r["current_version"],
            title=r["title"],
            creator=r["creator"],
            score=r["score"],
            duration_s=r["end_s"] - r["start_s"],
            source_title=r["source_title"],
            has_thumb=bool(r["thumb_path"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def count_queue(conn: sqlite3.Connection, flt: QueueFilter) -> int:
    where, params = _where(flt)
    sql = f"SELECT COUNT(*) FROM ({_CARD_SELECT}{where})"  # noqa: S608 - fixed fragments
    return int(conn.execute(sql, params).fetchone()[0])


def status_counts(conn: sqlite3.Connection, flt: QueueFilter) -> dict[str, int]:
    """Clip counts per status under the creator/score filters (ignores the status filter)."""
    where, params = _where(flt, with_status=False)
    sql = f"SELECT status, COUNT(*) AS n FROM ({_CARD_SELECT}{where}) GROUP BY status"  # noqa: S608
    return {r["status"]: r["n"] for r in conn.execute(sql, params)}


def list_creators(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT j.creator FROM clips c JOIN jobs j ON j.id = c.job_id ORDER BY 1"
    )
    return [r[0] for r in rows]


def get_clip(conn: sqlite3.Connection, clip_id: int) -> ClipDetail | None:
    row = conn.execute(
        "SELECT c.id, c.status, c.current_version, c.created_at, j.creator, j.platform,"
        " j.item_id, j.title AS source_title"
        " FROM clips c JOIN jobs j ON j.id = c.job_id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
    if row is None:
        return None
    versions = tuple(
        ClipVersion.from_row(v)
        for v in conn.execute(
            "SELECT * FROM clip_versions WHERE clip_id = ? ORDER BY version DESC", (clip_id,)
        )
    )
    current = next((v for v in versions if v.version == row["current_version"]), None)
    if current is None:
        return None
    return ClipDetail(
        id=row["id"],
        status=row["status"],
        current_version=row["current_version"],
        created_at=row["created_at"],
        creator=row["creator"],
        platform=row["platform"],
        item_id=row["item_id"],
        source_title=row["source_title"],
        current=current,
        versions=versions,
    )


def get_version(conn: sqlite3.Connection, clip_id: int, version: int) -> ClipVersion | None:
    row = conn.execute(
        "SELECT * FROM clip_versions WHERE clip_id = ? AND version = ?", (clip_id, version)
    ).fetchone()
    return ClipVersion.from_row(row) if row else None


def neighbour_pending(conn: sqlite3.Connection, clip_id: int, *, older: bool) -> int | None:
    """The adjacent pending clip in queue order (newest first): `older` is next (j)."""
    row = conn.execute("SELECT created_at FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if row is None:
        return None
    if older:
        sql = (
            "SELECT id FROM clips WHERE status = ? AND (created_at < ? OR"
            " (created_at = ? AND id < ?)) ORDER BY created_at DESC, id DESC LIMIT 1"
        )
    else:
        sql = (
            "SELECT id FROM clips WHERE status = ? AND (created_at > ? OR"
            " (created_at = ? AND id > ?)) ORDER BY created_at ASC, id ASC LIMIT 1"
        )
    created = row["created_at"]
    found = conn.execute(
        sql, (str(ClipStatus.PENDING_REVIEW), created, created, clip_id)
    ).fetchone()
    return found["id"] if found else None


def next_pending_after(conn: sqlite3.Connection, clip_id: int) -> int | None:
    """Where to go after acting on `clip_id`: the next older pending clip, else the newest."""
    nxt = neighbour_pending(conn, clip_id, older=True)
    if nxt is not None:
        return nxt
    row = conn.execute(
        "SELECT id FROM clips WHERE status = ? AND id != ? ORDER BY created_at DESC, id DESC"
        " LIMIT 1",
        (str(ClipStatus.PENDING_REVIEW), clip_id),
    ).fetchone()
    return row["id"] if row else None


def record_review(
    conn: sqlite3.Connection,
    clip_id: int,
    version: int,
    action: ReviewAction,
    now: datetime,
    *,
    reason_tag: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO clip_reviews (clip_id, version, action, reason_tag, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            version,
            str(action),
            reason_tag,
            json.dumps(payload, sort_keys=True) if payload is not None else None,
            to_iso(now),
        ),
    )


def reviews_for(conn: sqlite3.Connection, clip_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clip_reviews WHERE clip_id = ? ORDER BY created_at DESC, id DESC",
        (clip_id,),
    ).fetchall()


@dataclass(frozen=True)
class TransitionResult:
    changed: bool  # False: a repeat of an action already applied (double submit)
    status: str
    version: int


def _transition(
    conn: sqlite3.Connection,
    clip_id: int,
    target: ClipStatus,
    action: ReviewAction,
    now: datetime,
    *,
    expected_version: int | None,
    reason_tag: str | None = None,
    repeat_ok: frozenset[ClipStatus] = frozenset(),
) -> TransitionResult:
    with transaction(conn):
        row = conn.execute(
            "SELECT status, current_version FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if row is None:
            raise LookupError(clip_id)
        status, version = row["status"], row["current_version"]
        if expected_version is not None and expected_version != version:
            raise InvalidTransition(clip_id, f"{status} (version {version})", target)
        if status in repeat_ok:
            return TransitionResult(changed=False, status=status, version=version)
        if not can_transition(status, target):
            raise InvalidTransition(clip_id, status, target)
        conn.execute(
            "UPDATE clips SET status = ?, updated_at = ? WHERE id = ?",
            (str(target), to_iso(now), clip_id),
        )
        record_review(conn, clip_id, version, action, now, reason_tag=reason_tag)
    return TransitionResult(changed=True, status=str(target), version=version)


def approve(
    conn: sqlite3.Connection, clip_id: int, now: datetime, *, expected_version: int | None = None
) -> TransitionResult:
    return _transition(
        conn,
        clip_id,
        ClipStatus.APPROVED,
        ReviewAction.APPROVE,
        now,
        expected_version=expected_version,
        repeat_ok=frozenset({ClipStatus.APPROVED, ClipStatus.EXPORTED}),
    )


def reject(
    conn: sqlite3.Connection,
    clip_id: int,
    reason_tag: str,
    now: datetime,
    *,
    expected_version: int | None = None,
) -> TransitionResult:
    if reason_tag not in REJECT_REASONS:
        raise ValueError(f"invalid reject reason: {reason_tag!r}")
    return _transition(
        conn,
        clip_id,
        ClipStatus.REJECTED,
        ReviewAction.REJECT,
        now,
        expected_version=expected_version,
        reason_tag=reason_tag,
        repeat_ok=frozenset({ClipStatus.REJECTED}),
    )
