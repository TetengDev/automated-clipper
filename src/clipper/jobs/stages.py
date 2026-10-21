"""Stage contract: what a pipeline stage is and how it reports outcomes (CLP-13..16).

- A stage is registered as `StageDef(run, artifact)` keyed by the status it processes, in
  `clipper.pipeline.build_stages()`. `run(job, ctx)` does the work for that status;
  returning normally advances the job.
- `artifact` (relative to the job's media dir) marks the stage as done: if it already
  exists the stage is not run again, and `run` must create it, atomically (write a temp
  file, then rename), or the attempt counts as failed. This is what makes a killed worker
  resume without redoing earlier stages.
- Raise `SkipJob` to end the job as `skipped`, `PermanentError` to end it as `failed`;
  any other exception is retried later with backoff (status unchanged).
- Exception messages are stored in the DB and logged, so they must never contain secrets
  (API keys, tokens, signed URLs); `clipper.redact` scrubs known shapes only as a backstop.
- Statuses with no registered stage are never claimed: such jobs stay put until a worker
  that handles them runs (e.g. `waiting_vod` until the VOD poller exists).
- A stage must finish within the claim lease (DEFAULT_LEASE). There is no heartbeat yet
  (E3-S1b): past the lease another worker may take the job over, and the late result of
  the original worker is dropped. Every claim counts as an attempt, so a stage that keeps
  killing its worker ends the job as failed after MAX_ATTEMPTS claims.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from clipper.config import AppConfig
from clipper.jobs.models import Clock, Job, JobStatus, utcnow


class SkipJob(Exception):
    """Stage decided the job should not be clipped (message becomes skip_reason)."""


class PermanentError(Exception):
    """Stage failed in a way retrying cannot fix (message becomes last_error)."""


@dataclass(frozen=True)
class StageContext:
    media_dir: Path
    conn: sqlite3.Connection
    config: AppConfig
    clock: Clock = utcnow


Stage = Callable[[Job, StageContext], None]


@dataclass(frozen=True)
class StageDef:
    run: Stage
    artifact: str | None = None

    def __post_init__(self) -> None:
        if self.artifact is not None:
            path = PurePosixPath(self.artifact)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"artifact must be a relative path: {self.artifact!r}")


def _pass_through(job: Job, ctx: StageContext) -> None:
    """`detected` has no work of its own; the job just enters the pipeline."""


DEFAULT_STAGES: Mapping[JobStatus, StageDef] = {JobStatus.DETECTED: StageDef(_pass_through)}
