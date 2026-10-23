"""YouTube monitor (CLP-24): poll each creator's channel, enqueue new uploads as jobs.

A synchronous loop like the worker's: polling is blocking HTTP and yt-dlp subprocesses,
and SQLite is used synchronously everywhere, so asyncio would only add a second
concurrency style. The sleep between cycles is `stop.wait()`, so SIGTERM/SIGINT end it
at once; a cycle in progress stops after the current creator's poll.

Every cycle writes one `runs` row (component `monitor:youtube`): jobs enqueued
(items_new), Data API quota units spent, and errors as JSON. One creator failing is
logged and recorded there; the others are still polled.

Per creator:
- First poll (no seen_items for the creator yet): `baseline` marks every listed item as
  seen without any metadata call; `emit` handles items published within `lookback_days`
  like new ones and marks the older ones seen. On every poll, items published before the
  cutoff (the first poll's time, minus lookback_days for emit) are marked seen, so an
  interrupted first poll never lets old items through later. Only the first poll trusts
  the listing's (possibly rounded) dates; later ones use each item's metadata date.
- Otherwise only unseen IDs get metadata. Then: live, upcoming, or a finished stream
  whose VOD is still processing -> `waiting_vod`, not enqueued (the S3 live-VOD work,
  CLP-26, rechecks these); shorter than min_duration_seconds (Shorts) -> `skipped_short`;
  private/removed -> `unavailable`; anything else is enqueued.
- An item's seen_items row and its job are written in one transaction, so it is enqueued
  exactly once, across crashes and restarts too. A failed metadata lookup leaves the item
  unseen; the next poll retries it.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from clipper.config import AppConfig, Creator, Settings
from clipper.db import transaction
from clipper.jobs.models import Clock, from_iso, to_iso, utcnow
from clipper.jobs.queue import insert_job
from clipper.redact import redact
from clipper.sources.youtube import NOT_FINISHED, Metadata, Upload, VideoMeta
from clipper.urls import SourceRef

logger = logging.getLogger(__name__)

COMPONENT = "monitor:youtube"
PLATFORM = "youtube"
JITTER = 0.1
"""Each sleep is the poll interval +- 10 %, so creators are not polled in lockstep."""
METADATA_RETRY_BASE = timedelta(minutes=10)
DAY = timedelta(days=1)
MAX_METADATA_ATTEMPTS = 6
"""A video whose metadata keeps failing is retried after 10, 20, 40, 80 and 160 minutes,
then recorded as `metadata_failed` (about 5 h of failures). Platform blocks (bot check)
are not counted: they say nothing about the video."""


class FirstRun(StrEnum):
    BASELINE = "baseline"
    EMIT = "emit"


class SeenStatus(StrEnum):
    BASELINE = "baseline"
    ENQUEUED = "enqueued"
    SKIPPED_SHORT = "skipped_short"
    WAITING_VOD = "waiting_vod"
    UNAVAILABLE = "unavailable"
    METADATA_FAILED = "metadata_failed"


class Source(Protocol):
    def recent_uploads(self, channel_id: str) -> list[Upload]: ...

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool]) -> Metadata: ...

    @property
    def quota_units(self) -> int: ...


@dataclass
class CreatorPoll:
    creator: str
    first_run: bool = False
    new: int = 0
    """Items not seen before (enqueued or not)."""
    enqueued: list[tuple[int, str, str | None]] = field(default_factory=list)
    """(job id, video id, title) per job created."""
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Failure:
    creator: str
    attempts: int
    retry_at: datetime


@dataclass
class CycleReport:
    polls: list[CreatorPoll] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    """Creators with a YouTube handle but no channel_id yet."""
    errors: list[dict[str, str]] = field(default_factory=list)
    quota_units: int = 0

    @property
    def items_new(self) -> int:
        return sum(len(p.enqueued) for p in self.polls)


@dataclass
class YouTubeMonitor:
    conn: sqlite3.Connection
    config: AppConfig
    source: Source
    clock: Clock = utcnow
    first_run_mode: FirstRun | None = None
    """Overrides `settings.first_run_mode` (CLI --first-run)."""
    stop: threading.Event = field(default_factory=threading.Event)
    rng: random.Random = field(default_factory=random.Random)
    failures: dict[str, _Failure] = field(default_factory=dict)
    """Videos whose metadata lookup failed, by id. Kept in memory: a restart only resets
    the backoff, it cannot enqueue anything twice."""

    def run_forever(self, creators: Sequence[Creator]) -> None:
        """Poll every interval (with jitter) until `stop` is set. A failing cycle (e.g. the
        DB is locked while writing the runs row) is logged; the next one runs as usual."""
        interval_s = self.config.settings.poll_interval_youtube_minutes * 60
        logger.info("monitor started: %d creator(s), every ~%d s", len(creators), interval_s)
        while not self.stop.is_set():
            try:
                self.run_cycle(creators)
            except Exception:
                logger.exception("monitor cycle failed; next cycle as scheduled")
            self.stop.wait(interval_s * self.rng.uniform(1 - JITTER, 1 + JITTER))
        logger.info("monitor stopped")

    def run_cycle(self, creators: Sequence[Creator]) -> CycleReport:
        report = CycleReport()
        quota_before = self.source.quota_units
        with transaction(self.conn):
            run_id = self.conn.execute(
                "INSERT INTO runs (component, started_at) VALUES (?, ?)",
                (COMPONENT, to_iso(self.clock())),
            ).lastrowid
        for creator in creators:
            if self.stop.is_set():
                logger.info("stop requested; ending the cycle before %s", creator.name)
                break
            if creator.youtube is None:
                continue
            if creator.youtube.channel_id is None:
                logger.warning("%s: no YouTube channel_id; run `clipper resolve`", creator.name)
                report.unresolved.append(creator.name)
                continue
            poll = CreatorPoll(creator.name)
            report.polls.append(poll)
            try:
                self._poll(creator, creator.youtube.channel_id, poll)
            except Exception as exc:
                error = redact(f"{type(exc).__name__}: {exc}")
                logger.error("%s: poll failed: %s", creator.name, error)
                poll.errors.append({"creator": creator.name, "error": error})
            report.errors.extend(poll.errors)
        report.quota_units = self.source.quota_units - quota_before
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, items_new = ?, quota_units = ?, errors = ?"
                " WHERE id = ?",
                (
                    to_iso(self.clock()),
                    report.items_new,
                    report.quota_units,
                    json.dumps(report.errors) if report.errors else None,
                    run_id,
                ),
            )
        return report

    def _poll(self, creator: Creator, channel_id: str, poll: CreatorPoll) -> None:
        settings = self.config.effective_settings(creator.name)
        mode = self.first_run_mode or FirstRun(settings.first_run_mode)
        now = self.clock()
        uploads = self.source.recent_uploads(channel_id)
        history_start = self._history_start(creator.name)
        poll.first_run = history_start is None
        # The cutoff hangs off the creator's first poll, not this one, so a first poll that
        # was interrupted (SIGTERM, crash, error) cannot let old items through later:
        # baseline -> anything published before the first poll; emit -> before the first
        # poll minus lookback_days.
        start = history_start or now
        cutoff = (
            start if mode is FirstRun.BASELINE else start - timedelta(days=settings.lookback_days)
        )
        fresh = [u for u in uploads if not self._is_seen(u.video_id)]
        poll.new = len(fresh)
        if poll.first_run and mode is FirstRun.BASELINE:
            for upload in fresh:
                self._record(creator.name, upload.video_id, SeenStatus.BASELINE, now)
            if fresh:
                logger.info(
                    "%s: first poll, %d item(s) recorded as baseline", creator.name, poll.new
                )
            return
        if poll.first_run:
            # Listing dates spare a metadata call for items clearly before the cutoff. Only
            # on the first poll: the fallback listing's dates are rounded to the nearest
            # unit, so a later upload could look older than the first poll. Later polls
            # judge each item by its metadata's exact date.
            old = {u.video_id for u in fresh if u.published is not None and u.published < cutoff}
            for video_id in old:
                self._record(creator.name, video_id, SeenStatus.BASELINE, now)
            fresh = [u for u in fresh if u.video_id not in old]
        listed = {u.video_id for u in uploads}
        for video_id, failure in list(self.failures.items()):
            if failure.creator == creator.name and video_id not in listed:
                del self.failures[video_id]  # dropped off the listing: forget it
        due = [
            u.video_id
            for u in fresh
            if u.video_id not in self.failures or self.failures[u.video_id].retry_at <= now
        ]
        if not due:
            return
        meta = self.source.videos(due, self.stop.is_set)
        for video_id in due:
            if video_id in meta.unavailable:
                self.failures.pop(video_id, None)
                self._record(creator.name, video_id, SeenStatus.UNAVAILABLE, now)
            elif video_id in meta.errors:
                error = redact(meta.errors[video_id])
                logger.warning("%s: %s: metadata failed: %s", creator.name, video_id, error)
                poll.errors.append({"creator": creator.name, "item": video_id, "error": error})
                if video_id not in meta.blocked:
                    self._metadata_failed(creator.name, video_id, now)
            elif video_id in meta.videos:
                self.failures.pop(video_id, None)
                self._decide(creator.name, meta.videos[video_id], settings, cutoff, now, poll)
            # else: not looked at (stop requested, or the batch hit a block): next poll
        if poll.new:
            logger.info(
                "%s: %d new item(s), %d enqueued", creator.name, poll.new, len(poll.enqueued)
            )

    def _metadata_failed(self, creator: str, video_id: str, now: datetime) -> None:
        failure = self.failures.get(video_id) or _Failure(creator, 0, now)
        failure.attempts += 1
        if failure.attempts >= MAX_METADATA_ATTEMPTS:
            self.failures.pop(video_id, None)
            logger.warning(
                "%s: %s: metadata failed %d times; giving up (enqueue it by hand if wanted)",
                creator,
                video_id,
                failure.attempts,
            )
            self._record(creator, video_id, SeenStatus.METADATA_FAILED, now)
            return
        failure.retry_at = now + METADATA_RETRY_BASE * 2 ** (failure.attempts - 1)
        self.failures[video_id] = failure

    def _decide(
        self,
        creator: str,
        video: VideoMeta,
        settings: Settings,
        cutoff: datetime,
        now: datetime,
        poll: CreatorPoll,
    ) -> None:
        # Undated: on the first poll it could be old -> baseline; later polls only list
        # recent uploads, so it is treated as new.
        if video.published is None:
            old = poll.first_run
        else:
            # A bare calendar day may be the uploader's local date: allow a day either way.
            slack = DAY if video.day_precision else timedelta(0)
            old = video.published < cutoff - slack
        if old:
            status = SeenStatus.BASELINE
        elif video.live_status in NOT_FINISHED:
            status = SeenStatus.WAITING_VOD
        elif video.duration_s is not None and video.duration_s < settings.min_duration_seconds:
            status = SeenStatus.SKIPPED_SHORT
        else:
            # An unknown duration is enqueued: the worker probes again and applies the
            # duration limits itself.
            status = SeenStatus.ENQUEUED
        job_id = self._record(creator, video.video_id, status, now, video)
        if job_id is not None:
            poll.enqueued.append((job_id, video.video_id, video.title))
            logger.info("%s: enqueued job %d for %s", creator, job_id, video.video_id)

    def _history_start(self, creator: str) -> datetime | None:
        """When the creator was first polled (its oldest seen item); None: never."""
        row = self.conn.execute(
            "SELECT MIN(first_seen_at) FROM seen_items WHERE platform = ? AND creator = ?",
            (PLATFORM, creator),
        ).fetchone()
        return from_iso(row[0])

    def _is_seen(self, video_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE platform = ? AND item_id = ?", (PLATFORM, video_id)
        ).fetchone()
        return row is not None

    def _record(
        self,
        creator: str,
        video_id: str,
        status: SeenStatus,
        now: datetime,
        video: VideoMeta | None = None,
    ) -> int | None:
        """Mark the item seen and, for ENQUEUED, create its job, in one transaction.
        Returns the new job id, or None (not enqueued, or already seen/queued)."""
        ref = SourceRef(PLATFORM, video_id)
        stamp = to_iso(now)
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO seen_items (platform, item_id, creator, status, first_seen_at,"
                " last_checked_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (platform, item_id) DO NOTHING",
                (PLATFORM, video_id, creator, status.value, stamp, stamp),
            )
            if cur.rowcount != 1 or status is not SeenStatus.ENQUEUED:
                return None
            # A finished stream (was_live) is queued like an upload for now: is_stream
            # routes a job through waiting_vod, which has no stage before S3 (CLP-26).
            return insert_job(
                self.conn,
                ref,
                now,
                creator=creator,
                title=video.title if video else None,
                duration_seconds=video.duration_s if video else None,
            )
