"""Janitor (CLP-38): review expiry, media retention and the disk budget.

run-monitor calls `tick()` after every poll cycle; the sweeps run at most once an hour.
Each sweep is one method, named in `SWEEPS` and run in that order. A sweep that fails is
logged and reported; the others still run.

- `expire_reviews`: clips still waiting for review (`pending_review`, `needs_attention`)
  `review_expiry_days` after their current version was created are `archived`, with an
  `archive` audit row. A re-render that returns a clip to review starts the wait over.
- `prune_sources`: `clips_ready` jobs older than `media_retention_hours` (since they became
  ready) lose their source media: the audio, leftover downloads, and each clip's downloaded
  section (`clips/<rank>/source.mp4`, its sidecar, temp files). Rendered clips
  (`v<N>.mp4/jpg/json`), transcripts and the other small artifacts stay.
- `prune_dead_clips`: `rejected` and `archived` clips lose their clip dir
  `rejected_retention_days` after their last status change. Their DB rows stay.
- `enforce_disk_budget`: measures the media dir. At 80% of `disk_budget_gb` the `alert`
  notifier fires once per crossing; it fires again only after usage has dropped below 80%.
  That state is kept in memory, so a restart can send it once more. Above the budget, the
  source media of `clips_ready` jobs goes too, oldest job first, until usage is under it.

Deletion rules. Files are deleted only here, and only inside `media_root`:
- Paths are built from validated parts (`SourceRef.key`, a clip's rank, fixed names) under
  the resolved media root, never from path text stored in the DB.
- Nothing is deleted at, through or below a symlink. A path whose real location differs
  from its own (a link anywhere below the root) is refused, links met while emptying a dir
  are left in place, and no link is ever followed.
- Only regular files and empty dirs are removed. A file or dir that cannot be deleted or
  read (e.g. root-owned) is logged and skipped; the sweep goes on with the next one.
- Clips that are `approved`, `exported`, `changes_requested` or `re_rendering` keep their
  whole clip dir (a re-render reads the section). Any file that a clip in those states or
  a post points at is kept too.
- Jobs that are not `clips_ready` are never touched: a worker may be using their files.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from clipper.clips import REVIEWABLE, ClipStatus, ReviewAction, record_review, resolve_media_path
from clipper.config import AppConfig
from clipper.db import transaction
from clipper.jobs.models import Clock, JobStatus, to_iso, utcnow
from clipper.notify import Notify, NotifyError
from clipper.pipeline.fetch_audio import AUDIO_NAME, INFO_TMP_NAME, SOURCE_STEM
from clipper.pipeline.fetch_clips import SIDECAR, TMP_STEM
from clipper.redact import redact
from clipper.urls import InvalidSourceURL, SourceRef

logger = logging.getLogger(__name__)

GB = 10**9
ALERT_SHARE = 0.8
INTERVAL = timedelta(hours=1)
SECTION_NAME = "source.mp4"
"""A clip's downloaded section (`fetching_clips`), inside `clips/<rank>/`."""
SWEEPS = ("expire_reviews", "prune_sources", "prune_dead_clips", "enforce_disk_budget")
KEPT = frozenset(
    {
        ClipStatus.APPROVED,
        ClipStatus.EXPORTED,
        ClipStatus.CHANGES_REQUESTED,
        ClipStatus.RE_RENDERING,
    }
)
"""Clips whose files no sweep deletes."""
DEAD = (ClipStatus.REJECTED, ClipStatus.ARCHIVED)

Sweep = Callable[[datetime, "JanitorReport"], None]


def media_bytes(root: Path) -> int:
    """Bytes in the regular files under `root`. Links are not followed or counted."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            with suppress(OSError):
                info = os.lstat(os.path.join(dirpath, name))
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


@dataclass
class JanitorReport:
    archived: int = 0
    removed: int = 0
    """Files deleted."""
    freed_bytes: int = 0
    usage_bytes: int | None = None
    """The media dir's size, as measured for the disk budget."""
    errors: list[str] = field(default_factory=list)


@dataclass
class Janitor:
    conn: sqlite3.Connection
    config: AppConfig
    media_root: Path
    alert: Notify
    clock: Clock = utcnow
    disk_usage: Callable[[Path], int] = media_bytes
    last_run: datetime | None = None
    disk_alerted: bool = False
    """Usage is at or above 80% and the alert went out; reset once it drops below."""

    def tick(self) -> JanitorReport | None:
        """The monitor's hook: run when an hour has passed since the last run. Never raises."""
        now = self.clock()
        if self.last_run is not None and now - self.last_run < INTERVAL:
            return None
        self.last_run = now
        try:
            return self.run()
        except Exception:
            logger.exception("janitor run failed")
            return None

    def run(self) -> JanitorReport:
        now = self.clock()
        report = JanitorReport()
        for name in SWEEPS:
            sweep: Sweep = getattr(self, name)
            try:
                sweep(now, report)
            except Exception as exc:
                error = redact(f"{type(exc).__name__}: {exc}")
                logger.error("janitor: %s failed: %s", name, error)
                report.errors.append(f"{name}: {error}")
        logger.info(
            "janitor: %d clip(s) archived, %d file(s) deleted (%.1f MB), %d error(s)",
            report.archived,
            report.removed,
            report.freed_bytes / 1e6,
            len(report.errors),
        )
        return report

    # --- sweeps ---------------------------------------------------------------------------

    def expire_reviews(self, now: datetime, report: JanitorReport) -> None:
        cutoff = now - timedelta(days=self.config.settings.review_expiry_days)
        waiting = [str(s) for s in REVIEWABLE]
        marks = ", ".join("?" * len(waiting))
        # One write transaction: a reviewer's approve cannot land between the read and the
        # update, so a clip that was just approved is never archived.
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT c.id, c.current_version FROM clips c"  # noqa: S608 - placeholders
                " JOIN clip_versions v ON v.clip_id = c.id AND v.version = c.current_version"
                f" WHERE c.status IN ({marks}) AND v.created_at < ? ORDER BY c.id",
                (*waiting, to_iso(cutoff)),
            ).fetchall()
            for clip_id, version in rows:
                self.conn.execute(
                    "UPDATE clips SET status = ?, updated_at = ? WHERE id = ?",
                    (str(ClipStatus.ARCHIVED), to_iso(now), clip_id),
                )
                record_review(
                    self.conn,
                    clip_id,
                    version,
                    ReviewAction.ARCHIVE,
                    now,
                    payload={"reason": "review_expired"},
                )
        report.archived += len(rows)

    def prune_sources(self, now: datetime, report: JanitorReport) -> None:
        cutoff = now - timedelta(hours=self.config.settings.media_retention_hours)
        root = self.media_root.resolve()
        kept = self._kept(root)
        for key in self._ready_jobs(cutoff):
            self._prune_job(root, key, kept, report)

    def prune_dead_clips(self, now: datetime, report: JanitorReport) -> None:
        cutoff = now - timedelta(days=self.config.settings.rejected_retention_days)
        dead = [str(s) for s in DEAD]
        marks = ", ".join("?" * len(dead))
        rows = self.conn.execute(
            "SELECT j.platform, j.item_id, c.rank FROM clips c"  # noqa: S608 - placeholders
            f" JOIN jobs j ON j.id = c.job_id WHERE c.status IN ({marks}) AND c.updated_at < ?"
            " ORDER BY c.id",
            (*dead, to_iso(cutoff)),
        ).fetchall()
        root = self.media_root.resolve()
        kept = self._kept(root)
        for platform, item_id, rank in rows:
            clip_dir = _clip_dir(root, platform, item_id, rank)
            if clip_dir is not None:
                self._remove(root, clip_dir, kept, report)

    def enforce_disk_budget(self, now: datetime, report: JanitorReport) -> None:
        budget_gb = self.config.settings.disk_budget_gb
        budget = budget_gb * GB
        root = self.media_root.resolve()
        used = self.disk_usage(root)
        report.usage_bytes = used
        if used < ALERT_SHARE * budget:
            self.disk_alerted = False
        elif not self.disk_alerted:
            text = (
                f"Disk: media uses {used / GB:.1f} GB, {used / budget:.0%} of the"
                f" {budget_gb} GB disk budget"
            )
            try:
                self.alert(text)
            except NotifyError as exc:
                logger.warning("janitor: disk alert not sent (%s); next run tries again", exc)
            else:
                self.disk_alerted = True
        if used <= budget:
            return
        kept = self._kept(root)
        for key in self._ready_jobs(now):
            used -= self._prune_job(root, key, kept, report)
            if used <= budget:
                return
        logger.warning(
            "janitor: media uses %.1f GB, over the %d GB disk budget, with no source media"
            " left to delete",
            used / GB,
            budget_gb,
        )

    # --- helpers --------------------------------------------------------------------------

    def _ready_jobs(self, before: datetime) -> list[str]:
        """Media dir names of `clips_ready` jobs that became ready before `before`, oldest
        first."""
        rows = self.conn.execute(
            "SELECT platform, item_id FROM jobs WHERE status = ? AND updated_at < ?"
            " ORDER BY updated_at, id",
            (JobStatus.CLIPS_READY.value, to_iso(before)),
        ).fetchall()
        keys = []
        for platform, item_id in rows:
            try:
                keys.append(SourceRef(platform, item_id).key)
            except InvalidSourceURL:
                logger.warning("janitor: job with an invalid source id skipped")
        return keys

    def _kept(self, root: Path) -> frozenset[Path]:
        """Paths no sweep may delete: the clip dirs of KEPT clips, and the files those
        clips' versions and every post point at."""
        statuses = [str(s) for s in KEPT]
        marks = ", ".join("?" * len(statuses))
        kept: set[Path] = set()
        clips = self.conn.execute(
            "SELECT j.platform, j.item_id, c.rank FROM clips c"  # noqa: S608 - placeholders
            f" JOIN jobs j ON j.id = c.job_id WHERE c.status IN ({marks})",
            statuses,
        )
        for platform, item_id, rank in clips:
            clip_dir = _clip_dir(root, platform, item_id, rank)
            if clip_dir is not None:
                kept.add(clip_dir)
        stored = self.conn.execute(
            "SELECT v.video_path, v.thumb_path FROM clip_versions v"  # noqa: S608 - placeholders
            f" JOIN clips c ON c.id = v.clip_id WHERE c.status IN ({marks})"
            " UNION ALL SELECT video_path, NULL FROM posts",
            statuses,
        )
        for row in stored:
            kept.update(_stored_paths(root, row))
        return frozenset(kept)

    def _prune_job(self, root: Path, key: str, kept: frozenset[Path], report: JanitorReport) -> int:
        """Delete one job's source media; returns the bytes freed."""
        job_dir = root / key
        paths = [
            job_dir / AUDIO_NAME,
            job_dir / INFO_TMP_NAME,
            *_listed(root, job_dir, f"{SOURCE_STEM}.*"),
        ]
        for clip_dir in _listed(root, job_dir / "clips", "*"):
            if clip_dir.name.isdigit():
                paths += [
                    clip_dir / SECTION_NAME,
                    clip_dir / SIDECAR,
                    *_listed(root, clip_dir, f"{TMP_STEM}*"),
                ]
        return sum(self._remove(root, path, kept, report) for path in paths)

    def _remove(self, root: Path, path: Path, kept: frozenset[Path], report: JanitorReport) -> int:
        """Delete a regular file, or empty a dir bottom-up and remove it; returns the bytes
        freed. Refuses anything outside `root`, at or under a symlink, or kept."""
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return 0
        if not _real_inside(root, path):
            logger.warning("janitor: %s is a symlink or under one; not deleted", path)
            return 0
        if any(path == k or path.is_relative_to(k) for k in kept):
            return 0
        if stat.S_ISREG(info.st_mode):
            try:
                path.unlink()
            except OSError as exc:  # e.g. a root-owned dir: skip it, never stall the sweep
                logger.warning("janitor: cannot delete %s: %s", path, type(exc).__name__)
                return 0
            report.removed += 1
            report.freed_bytes += info.st_size
            return info.st_size
        if not stat.S_ISDIR(info.st_mode):
            logger.warning("janitor: %s is not a regular file or dir; not deleted", path)
            return 0
        try:
            children = sorted(path.iterdir())
        except OSError as exc:
            logger.warning("janitor: cannot read %s: %s", path, type(exc).__name__)
            return 0
        freed = sum(self._remove(root, child, kept, report) for child in children)
        with suppress(OSError):  # not empty: something in it was kept or refused
            path.rmdir()
        return freed


def _clip_dir(root: Path, platform: str, item_id: str, rank: int) -> Path | None:
    try:
        key = SourceRef(platform, item_id).key  # type: ignore[arg-type]
    except InvalidSourceURL:
        return None
    return root / key / "clips" / str(int(rank))


def _real_inside(root: Path, path: Path) -> bool:
    """`path` is below `root` and no part of it below the root is a symlink."""
    return path.is_relative_to(root) and path != root and os.path.realpath(path) == str(path)


def _listed(root: Path, directory: Path, pattern: str) -> list[Path]:
    """Entries of a real dir under `root` matching `pattern`; nothing for a link or a miss."""
    if not _real_inside(root, directory) or not directory.is_dir():
        return []
    return sorted(directory.glob(pattern))


def _stored_paths(root: Path, stored: Iterable[str | None]) -> set[Path]:
    """Real paths inside `root` of DB-stored media paths. A relative path may be relative
    to the media root or to the working dir (the worker's default media dir is relative)."""
    found: set[Path] = set()
    for text in stored:
        if not text or "\x00" in text:
            continue
        for candidate in (text, str(Path(text).absolute())):
            real = resolve_media_path(root, candidate)
            if real is not None:
                found.add(real)
    return found
