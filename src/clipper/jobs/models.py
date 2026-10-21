"""Job states, pipeline order, and the Job row model."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from clipper.urls import SourceRef

Clock = Callable[[], datetime]
_ISO = "%Y-%m-%dT%H:%M:%SZ"


class JobStatus(StrEnum):
    DETECTED = "detected"
    WAITING_VOD = "waiting_vod"
    FETCHING_AUDIO = "fetching_audio"
    TRANSCRIBING = "transcribing"
    SELECTING = "selecting"
    FETCHING_CLIPS = "fetching_clips"
    RENDERING = "rendering"
    CLIPS_READY = "clips_ready"
    FAILED = "failed"
    SKIPPED = "skipped"


PIPELINE: tuple[JobStatus, ...] = (
    JobStatus.DETECTED,
    JobStatus.WAITING_VOD,
    JobStatus.FETCHING_AUDIO,
    JobStatus.TRANSCRIBING,
    JobStatus.SELECTING,
    JobStatus.FETCHING_CLIPS,
    JobStatus.RENDERING,
    JobStatus.CLIPS_READY,
)
TERMINAL = frozenset({JobStatus.CLIPS_READY, JobStatus.FAILED, JobStatus.SKIPPED})


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """UTC, second resolution; the same text format as the schema's defaults, so it sorts."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; pass an aware (UTC) datetime")
    return moment.astimezone(UTC).strftime(_ISO)


def from_iso(text: str | None) -> datetime | None:
    """Parse `to_iso` output (or any ISO-8601 UTC stamp); None stays None."""
    if not text:
        return None
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Job:
    id: int
    ref: SourceRef
    creator: str
    source_url: str
    status: JobStatus
    is_stream: bool
    attempts: int
    title: str | None = None
    duration_seconds: int | None = None
    last_error: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            ref=SourceRef(row["platform"], row["item_id"]),
            creator=row["creator"],
            source_url=row["source_url"],
            status=JobStatus(row["status"]),
            is_stream=bool(row["is_stream"]),
            attempts=row["attempts"],
            title=row["title"],
            duration_seconds=row["duration_seconds"],
            last_error=row["last_error"],
            created_at=from_iso(row["created_at"]),
        )

    @property
    def next_status(self) -> JobStatus:
        """Pipeline successor. Only stream jobs wait for a VOD; an enqueued URL is already one."""
        if self.status in TERMINAL:
            raise ValueError(f"job {self.id} is terminal ({self.status})")
        if self.status is JobStatus.DETECTED and not self.is_stream:
            return JobStatus.FETCHING_AUDIO
        return PIPELINE[PIPELINE.index(self.status) + 1]
