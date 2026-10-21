"""Helpers shared by the pipeline stages (CLP-13..16): atomic artifact writes, reading the
previous stage's artifact, ffmpeg/ffprobe runs with redacted errors, per-creator settings,
and the `spend` ledger.

Retryable failures of external tools raise `MediaError` (ffmpeg/ffprobe) or
`clipper.pipeline.ytdlp.YtDlpError` (yt-dlp); both are `RuntimeError`s. Subprocesses go
through a `Runner` (`clipper.pipeline.ytdlp.run_command` unless a test substitutes one).

Paid API calls go through `reserve_spend`: the budget check and a reservation of the
estimated cost happen in one `BEGIN IMMEDIATE` transaction (so two workers cannot both
pass the check), and the row is settled to the actual cost afterwards. Over the daily cap
the stage is `Deferred` to the next UTC midnight; it never fails or skips the job.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time as _time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from clipper.config import AppConfig, Creator, Settings
from clipper.db import transaction
from clipper.jobs.models import Job, to_iso
from clipper.jobs.queue import DEFAULT_LEASE
from clipper.jobs.stages import Deferred, PermanentError, SkipJob
from clipper.pipeline import ytdlp
from clipper.pipeline.ytdlp import CommandTimeout, Runner, RunResult
from clipper.redact import redact

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT_S = 60.0
STDERR_TAIL_CHARS = 300
REDACT_LIMIT_CHARS = 64 * 1024
"""Error text is redacted over at most its last 64 KB, then cut for storage."""
STAGE_BUDGET_S = 100 * 60.0
"""Wall-clock budget of one stage run: well inside the 2 h claim lease."""
PROTOCOL_WHITELIST = ["-protocol_whitelist", "file,pipe"]
"""ffmpeg/ffprobe input option: local files only, never network or nested protocols."""
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Invisible characters that can spoof text: bidi controls, zero-width space/non-joiner,
# LRM/RLM, word joiner, BOM, tag characters. ZWJ (U+200D) stays: compound emoji need it.
_INVISIBLE = re.compile(
    "[\u200b\u200c\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed (retryable). Message carries a short, redacted stderr tail."""


class NoDuration(MediaError):
    """ffprobe ran fine but the file reports no duration (not a usable media file)."""


class StageTimeout(RuntimeError):
    """The stage used up its wall-clock budget (retryable; the next run resumes)."""


@dataclass
class Deadline:
    """Wall-clock budget for one stage run; `timeout()` shrinks per-call timeouts to it."""

    budget_s: float = STAGE_BUDGET_S
    clock: Callable[[], float] = _time.monotonic
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float:
        return self.budget_s - (self.clock() - self.started)

    def timeout(self, wanted_s: float) -> float:
        left = self.remaining()
        if left <= 1.0:
            raise StageTimeout(f"stage exceeded its {self.budget_s / 60:.0f} min budget")
        return min(wanted_s, left)


def strip_control(text: str) -> str:
    """Drop control and invisible spoofing characters (incl. newlines, bidi overrides) and
    collapse whitespace."""
    return " ".join(_CONTROL.sub(" ", _INVISIBLE.sub("", text)).split())


def default_runner() -> Runner:
    """The process runner stages use when none is injected (looked up at call time)."""
    return ytdlp.run_command


def write_json_atomic(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    """Write JSON to a temp file in the same directory, fsync, then rename over `path`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, *, required: bool = True) -> Any:
    """Load an input artifact. Missing (when required) or unreadable is permanent: the
    earlier stage that produced it will not run again for this job."""
    if not path.is_file():
        if required:
            raise PermanentError(f"{path.name} is missing")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PermanentError(f"{path.name} is unreadable: {type(exc).__name__}") from None


def stderr_tail(stderr: str) -> str:
    """Last lines of `stderr`, redacted before cutting (a cut must not split a secret)."""
    text = redact(stderr[-REDACT_LIMIT_CHARS:])
    return " | ".join(text.strip().splitlines()[-3:])[-STDERR_TAIL_CHARS:]


def run_media(
    args: Sequence[str],
    timeout_s: float,
    what: str,
    *,
    runner: Runner | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> RunResult:
    """Run an ffmpeg/ffprobe argv; non-zero exit or timeout -> `MediaError`."""
    run = runner if runner is not None else default_runner()
    try:
        if on_stderr_line is None:
            result = run(args, timeout_s)
        else:
            result = run(args, timeout_s, on_stderr_line=on_stderr_line)
    except CommandTimeout as exc:
        raise MediaError(f"{what}: {exc}") from None
    if result.returncode != 0:
        raise MediaError(f"{what} failed (exit {result.returncode}): {stderr_tail(result.stderr)}")
    return result


def ffprobe_duration(
    path: Path, *, runner: Runner | None = None, timeout_s: float = FFPROBE_TIMEOUT_S
) -> float:
    """Container duration in seconds. `MediaError` if ffprobe fails or times out
    (retryable), `NoDuration` if it ran but reported no usable duration."""
    args = [
        "ffprobe", *PROTOCOL_WHITELIST, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path.resolve()),
    ]  # fmt: skip
    out = run_media(args, timeout_s, "duration probe", runner=runner).stdout
    try:
        duration = float(out.strip())
    except ValueError:
        raise NoDuration("duration probe returned no duration") from None
    if not duration > 0 or duration == float("inf"):
        raise NoDuration(f"duration probe returned {duration}")
    return duration


def creator_for(job: Job, config: AppConfig) -> Creator | None:
    """The job's configured creator, or None (e.g. a manually enqueued URL)."""
    try:
        return config.creator(job.creator)
    except KeyError:
        return None


def settings_for(job: Job, config: AppConfig) -> Settings:
    """Effective settings for the job's creator; global settings for unknown creators."""
    creator = creator_for(job, config)
    return config.effective_settings(creator.name) if creator else config.settings


def first_seen(marker: Path, now: datetime, *, max_age: timedelta | None = None) -> datetime:
    """When a condition (source not ready, no API credit) was first hit for this job: read
    from `marker`, or recorded as `now`. Remove the marker once the condition clears.

    A future-dated marker (clock skew) counts as `now`; one older than `max_age` is stale
    (from an earlier, unrelated episode) and is restarted at `now`."""
    try:
        seen = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
        fresh = max_age is None or now - seen <= max_age
        if seen.tzinfo is not None and seen <= now and fresh:
            return seen
    except (OSError, ValueError):
        pass
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f"{marker.name}.tmp")
    tmp.write_text(now.astimezone(UTC).isoformat(), encoding="utf-8")
    os.replace(tmp, marker)
    return now


def next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(0), tzinfo=UTC)


def _utc_day(now: datetime) -> tuple[str, str]:
    start = datetime.combine(now.astimezone(UTC).date(), time(0), tzinfo=UTC)
    return to_iso(start), to_iso(start + timedelta(days=1))


def spend_today(conn: sqlite3.Connection, kind: str, now: datetime) -> float:
    """USD recorded for `kind` ('llm' | 'transcribe') during the current UTC day."""
    start, end = _utc_day(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM spend"
        " WHERE kind = ? AND created_at >= ? AND created_at < ?",
        (kind, start, end),
    ).fetchone()
    return float(row[0])


@dataclass
class SpendCharge:
    """What a reserved call actually cost; filled in by the caller, settled on exit."""

    usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    audio_seconds: float | None = None


@contextmanager
def reserve_spend(
    conn: sqlite3.Connection,
    *,
    kind: str,
    label: str,
    provider: str,
    job_id: int,
    estimate_usd: float,
    cap_usd: float,
    now: datetime,
) -> Iterator[SpendCharge]:
    """Check the daily cap and reserve `estimate_usd` atomically, then settle the row to
    the `SpendCharge` the caller fills in (deleted if nothing was charged).

    Unsettled rows this job left behind (worker killed mid-call) are dropped first.
    Over the cap -> `Deferred` to the next UTC midnight. An estimate larger than the whole
    cap can never fit -> `SkipJob`. A DB error while settling after a failure is logged,
    never raised over the original exception.
    """
    if estimate_usd > cap_usd:
        raise SkipJob(f"{label} cost ${estimate_usd:.2f} exceeds the daily cap ${cap_usd:.2f}")
    start, end = _utc_day(now)
    with transaction(conn):
        # A reservation this job leaked when its worker was killed (never settled: settled
        # rows always carry tokens or audio seconds). Assumes a single holder per job: the
        # claim lease makes this worker the job's only runner, and a row older than the
        # lease cannot belong to a call still in flight.
        conn.execute(
            "DELETE FROM spend WHERE job_id = ? AND kind = ?"
            " AND input_tokens IS NULL AND audio_seconds IS NULL AND created_at < ?",
            (job_id, kind, to_iso(now - DEFAULT_LEASE)),
        )
        spent = float(
            conn.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM spend"
                " WHERE kind = ? AND created_at >= ? AND created_at < ?",
                (kind, start, end),
            ).fetchone()[0]
        )
        if spent + estimate_usd > cap_usd:
            raise Deferred(
                next_utc_midnight(now),
                f"daily {label} budget reached (${spent:.2f} of ${cap_usd:.2f} spent,"
                f" ${estimate_usd:.2f} needed)",
            )
        row_id = conn.execute(
            "INSERT INTO spend (job_id, provider, kind, usd, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, provider, kind, round(estimate_usd, 6), to_iso(now)),
        ).lastrowid
    charge = SpendCharge()
    try:
        yield charge
    except BaseException:
        try:
            _settle(conn, row_id, charge)
        except sqlite3.Error:
            logger.exception("could not settle spend row %s (reserved $%.4f)", row_id, estimate_usd)
        raise
    _settle(conn, row_id, charge)


def _settle(conn: sqlite3.Connection, row_id: int | None, charge: SpendCharge) -> None:
    with transaction(conn):
        if charge.usd <= 0 and charge.input_tokens is None and charge.audio_seconds is None:
            conn.execute("DELETE FROM spend WHERE id = ?", (row_id,))
            return
        conn.execute(
            "UPDATE spend SET usd = ?, input_tokens = ?, output_tokens = ?, audio_seconds = ?"
            " WHERE id = ?",
            (
                round(max(charge.usd, 0.0), 6),
                charge.input_tokens,
                charge.output_tokens,
                None if charge.audio_seconds is None else round(charge.audio_seconds, 3),
                row_id,
            ),
        )
