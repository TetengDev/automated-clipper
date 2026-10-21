"""Export approved clips to output/ready/<YYYY-MM-DD>/ (+ optional rclone copy).

Package per approved version (`<base>` = `<creator-slug>_<title-slug>-<clip_id>_v<n>`):
    <base>.mp4            the rendered clip
    <base>.caption.txt    caption + hashtags, ready to paste
    <base>.json           title, source URL with timestamp, creator, score, reason, ...

The date folder is the UTC date of the approval. Exactly once per version: success is
recorded as an `export` row in clip_reviews (the clip becomes `exported`); a version that
already has one is never exported again. The package is built in output/.staging/, copied
to the rclone remote from there, and only then renamed into ready/, so neither ready/ nor
the remote ever sees a partial package. A failure leaves the clip `approved`, records an
`export_failed` row with the error, and is retried by calling `export_clip` again
("Retry export" in the review app, or `clipper export-pending`); repeated failures are
logged as alerts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from clipper.clips import (
    ClipStatus,
    ReviewAction,
    get_clip,
    record_review,
    resolve_media_path,
)
from clipper.db import transaction
from clipper.jobs.models import to_iso
from clipper.redact import redact

DEFAULT_OUTPUT_DIR = Path("output")
READY_DIR = "ready"
STAGING_DIR = ".staging"
MAX_EXPORT_FAILURES = 3  # failures of one version before an alert is logged
SLUG_MAX_LENGTH = 60
RCLONE_TIMEOUT_SECONDS = 300
STAGING_MAX_AGE_SECONDS = 3600  # older staging dirs are leftovers of crashed exports
_ERROR_MAX_LENGTH = 500
_STDERR_TAIL = 200
_REDACT_INPUT_MAX = 4000  # bound the text fed to the redaction regexes
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

logger = logging.getLogger(__name__)

Runner = Callable[[list[str]], None]
ExportStatus = Literal["exported", "already_exported", "failed", "not_approved"]


class ExportError(Exception):
    """The clip cannot be exported (missing source file, sync failure, ...)."""


@dataclass(frozen=True)
class ExportResult:
    clip_id: int
    version: int
    status: ExportStatus
    files: tuple[Path, ...] = ()
    error: str | None = None
    failures: int = 0
    alert: bool = False


class ExportLocks:
    """In-process, non-blocking per-clip guard so one clip is never exported twice at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: set[int] = set()

    def try_acquire(self, clip_id: int) -> bool:
        with self._lock:
            if clip_id in self._busy:
                return False
            self._busy.add(clip_id)
            return True

    def release(self, clip_id: int) -> None:
        with self._lock:
            self._busy.discard(clip_id)

    def busy(self, clip_id: int) -> bool:
        with self._lock:
            return clip_id in self._busy


def slugify(text: str | None, fallback: str = "clip") -> str:
    """Lowercase ASCII words joined by '-': safe on every filesystem, never empty."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-") or fallback


def base_name(clip_id: int, creator: str, title: str | None, version: int) -> str:
    return f"{slugify(creator, 'creator')}_{slugify(title)}-{clip_id}_v{version}"


def run_subprocess(args: list[str]) -> None:
    subprocess.run(  # noqa: S603 - fixed argv list, no shell
        args, check=True, capture_output=True, text=True, timeout=RCLONE_TIMEOUT_SECONDS
    )


def rclone_args(remote: str, local_dir: Path, day: str) -> list[str]:
    dest = f"{remote}{day}" if remote.endswith((":", "/")) else f"{remote}/{day}"
    return ["rclone", "copy", "--", str(local_dir), dest]


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _copy(src: Path, dest: Path) -> None:
    with src.open("rb") as source, dest.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())


def _export_row(conn: sqlite3.Connection, clip_id: int, version: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clip_reviews WHERE clip_id = ? AND version = ? AND action = ?"
        " ORDER BY id LIMIT 1",
        (clip_id, version, str(ReviewAction.EXPORT)),
    ).fetchone()


def _files_of(row: sqlite3.Row | None) -> tuple[Path, ...]:
    if row is None or not row["payload"]:
        return ()
    return tuple(Path(p) for p in json.loads(row["payload"]).get("files", []))


def _approved_day(conn: sqlite3.Connection, clip_id: int, version: int, now: datetime) -> str:
    """UTC date of the approval, so retries on a later day reuse the folder."""
    row = conn.execute(
        "SELECT created_at FROM clip_reviews WHERE clip_id = ? AND version = ? AND action = ?"
        " ORDER BY id DESC LIMIT 1",
        (clip_id, version, str(ReviewAction.APPROVE)),
    ).fetchone()
    day = str(row["created_at"])[:10] if row else ""
    return day if _DAY.match(day) else to_iso(now)[:10]


def _check_inside(path: Path, root: Path) -> Path:
    """Raise unless `path` (following any existing symlinks) lies inside `root`."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError(f"{path} escapes {root}")
    return path


def _make_dir_inside(path: Path, root: Path) -> Path:
    _check_inside(path, root)
    path.mkdir(parents=True, exist_ok=True)
    return _check_inside(path, root)


def sweep_staging(output_dir: Path, now: datetime) -> int:
    """Remove staging dirs left by crashed exports (older than an hour). Returns the count."""
    staging = output_dir / STAGING_DIR
    if staging.is_symlink() or not staging.is_dir():
        return 0
    cutoff = now.timestamp() - STAGING_MAX_AGE_SECONDS
    removed = 0
    for entry in staging.iterdir():
        try:
            if entry.lstat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("cannot remove stale staging entry %s: %s", entry, exc)
    return removed


def _caption_text(caption: str | None, hashtags: tuple[str, ...]) -> str:
    parts = [p for p in ((caption or "").strip(), " ".join(hashtags)) if p]
    return "\n\n".join(parts) + "\n"


def _publish_package(
    conn: sqlite3.Connection,
    clip_id: int,
    version: int,
    *,
    output_dir: Path,
    media_root: Path,
    now: datetime,
    rclone_remote: str | None,
    runner: Runner,
) -> tuple[Path, tuple[Path, ...]]:
    clip = get_clip(conn, clip_id)
    if clip is None:
        raise ExportError(f"clip {clip_id} not found")
    ver = clip.current
    if ver.version != version:
        raise ExportError(f"clip {clip_id} version changed (v{version} -> v{ver.version})")
    source = resolve_media_path(media_root, ver.video_path)
    if source is None or source.suffix.lower() != ".mp4":
        raise ExportError(f"rendered video for clip {clip_id} v{ver.version} is missing")
    day = _approved_day(conn, clip_id, ver.version, now)
    day_dir = _check_inside(output_dir / READY_DIR / day, output_dir)
    staging_root = _make_dir_inside(output_dir / STAGING_DIR, output_dir)
    base = base_name(clip_id, clip.creator, ver.title, ver.version)
    meta = {
        "clip_id": clip_id,
        "version": ver.version,
        "title": ver.title,
        "creator": clip.creator,
        "source_url": clip.source_url,
        "source_title": clip.source_title,
        "start_s": ver.start_s,
        "end_s": ver.end_s,
        "score": ver.score,
        "reason": ver.reason,
        "caption": ver.caption,
        "hashtags": list(ver.hashtags),
        "prompt_version": ver.prompt_version,
        "exported_at": to_iso(now),
    }
    names = (f"{base}.mp4", f"{base}.caption.txt", f"{base}.json")
    stage = Path(tempfile.mkdtemp(dir=staging_root, prefix=f"{base}."))
    try:
        _copy(source, stage / names[0])
        _write_bytes(stage / names[1], _caption_text(ver.caption, ver.hashtags).encode())
        _write_bytes(
            stage / names[2], (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode()
        )
        if rclone_remote:
            runner(rclone_args(rclone_remote, stage, day))
        _make_dir_inside(day_dir, output_dir)
        # Per-file renames: a crash mid-way leaves complete files only, and the retry
        # (no export row yet) overwrites them with the same names.
        for name in names:
            os.replace(stage / name, day_dir / name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return day_dir, tuple(day_dir / name for name in names)


def _error_text(exc: BaseException) -> str:
    """Redact before cutting, so truncation can never expose half of a masked secret."""
    text = f"{type(exc).__name__}: {str(exc)[:_REDACT_INPUT_MAX]}"
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        stderr = str(exc.stderr).strip()[-_REDACT_INPUT_MAX:]
        tail = redact(redact(stderr)[-_STDERR_TAIL:])
        text = f"{redact(text)} ({tail})"
    return redact(redact(text)[:_ERROR_MAX_LENGTH])


def export_clip(
    conn: sqlite3.Connection,
    clip_id: int,
    *,
    output_dir: Path,
    media_root: Path,
    now: datetime,
    rclone_remote: str | None = None,
    runner: Runner = run_subprocess,
) -> ExportResult:
    """Export an approved clip's current version once. Safe to call repeatedly."""
    row = conn.execute(
        "SELECT status, current_version FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    if row is None:
        raise LookupError(clip_id)
    status, version = row["status"], row["current_version"]
    if status == ClipStatus.EXPORTED:
        return ExportResult(
            clip_id, version, "already_exported", _files_of(_export_row(conn, clip_id, version))
        )
    if status != ClipStatus.APPROVED:
        return ExportResult(clip_id, version, "not_approved")
    done = _export_row(conn, clip_id, version)
    if done is not None:
        _mark_exported(conn, clip_id, now)
        return ExportResult(clip_id, version, "already_exported", _files_of(done))

    try:
        day_dir, files = _publish_package(
            conn,
            clip_id,
            version,
            output_dir=output_dir,
            media_root=media_root,
            now=now,
            rclone_remote=rclone_remote,
            runner=runner,
        )
    except Exception as exc:  # any failure must be recorded so the clip can be retried
        return _record_failure(conn, clip_id, version, exc, now)

    with transaction(conn):
        existing = _export_row(conn, clip_id, version)
        if existing is None:
            payload = {"files": [str(f) for f in files], "rclone_remote": rclone_remote}
            try:
                record_review(conn, clip_id, version, ReviewAction.EXPORT, now, payload=payload)
            except sqlite3.IntegrityError:  # unique index: another exporter recorded it first
                existing = _export_row(conn, clip_id, version)
        _mark_exported(conn, clip_id, now, in_transaction=True)
    if existing is not None:
        return ExportResult(clip_id, version, "already_exported", _files_of(existing))
    logger.info("exported clip %d v%d to %s", clip_id, version, day_dir)
    return ExportResult(clip_id, version, "exported", files)


def _mark_exported(
    conn: sqlite3.Connection, clip_id: int, now: datetime, *, in_transaction: bool = False
) -> None:
    sql = "UPDATE clips SET status = ?, updated_at = ? WHERE id = ? AND status = ?"
    params = (str(ClipStatus.EXPORTED), to_iso(now), clip_id, str(ClipStatus.APPROVED))
    if in_transaction:
        conn.execute(sql, params)
        return
    with transaction(conn):
        conn.execute(sql, params)


def _record_failure(
    conn: sqlite3.Connection, clip_id: int, version: int, exc: BaseException, now: datetime
) -> ExportResult:
    error = _error_text(exc)
    with transaction(conn):
        record_review(
            conn, clip_id, version, ReviewAction.EXPORT_FAILED, now, payload={"error": error}
        )
        failures = conn.execute(
            "SELECT COUNT(*) FROM clip_reviews WHERE clip_id = ? AND version = ? AND action = ?",
            (clip_id, version, str(ReviewAction.EXPORT_FAILED)),
        ).fetchone()[0]
    alert = failures >= MAX_EXPORT_FAILURES
    if alert:
        logger.error(
            "ALERT export of clip %d v%d failed %d times: %s", clip_id, version, failures, error
        )
    else:
        logger.warning("export of clip %d v%d failed (%d): %s", clip_id, version, failures, error)
    return ExportResult(clip_id, version, "failed", error=error, failures=failures, alert=alert)


def export_pending(
    conn: sqlite3.Connection,
    *,
    output_dir: Path,
    media_root: Path,
    now: datetime,
    rclone_remote: str | None = None,
    runner: Runner = run_subprocess,
) -> list[ExportResult]:
    """Retry every approved-but-not-exported clip (for a periodic/CLI retry).

    One clip's failure never stops the loop; failures are recorded by `export_clip`, and
    anything that escapes it (e.g. a locked DB) is logged and reported as failed.
    """
    sweep_staging(output_dir, now)
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM clips WHERE status = ? ORDER BY id", (str(ClipStatus.APPROVED),)
        )
    ]
    results: list[ExportResult] = []
    for clip_id in ids:
        try:
            results.append(
                export_clip(
                    conn,
                    clip_id,
                    output_dir=output_dir,
                    media_root=media_root,
                    now=now,
                    rclone_remote=rclone_remote,
                    runner=runner,
                )
            )
        except Exception as exc:
            logger.exception("export of clip %d crashed", clip_id)
            results.append(ExportResult(clip_id, 0, "failed", error=_error_text(exc)))
    return results
