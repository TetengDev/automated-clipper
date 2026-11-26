"""Commentary job stages (CLP-142), registered by `clipper.pipeline.build_commentary_stages`.

A commentary job (`platform = 'commentary'`, one per channel per local day, made by the
monitor) runs detected -> discovering -> fetching_sources -> transcribing -> scripting ->
voicing -> rendering -> clips_ready. Each stage ships with its own story. Until then its
placeholder here ends the day's job as `skipped`, naming that story: a skip sends no
alert and keeps the job out of every clip stage. A day's job is only useful that day, so
it is skipped rather than deferred (deferred days would pile up and all run at once when
the stage ships). `clipper retry <id>` re-runs a skipped day by hand.
"""

from __future__ import annotations

from collections.abc import Mapping

from clipper.jobs.models import Job, JobStatus
from clipper.jobs.stages import SkipJob, StageContext, StageDef
from clipper.pipeline import commentary_sources

NOT_BUILT: Mapping[JobStatus, tuple[str, str]] = {
    JobStatus.DISCOVERING: ("discovery", "CLP-144"),
    JobStatus.SCRIPTING: ("scripting", "CLP-146"),
    JobStatus.VOICING: ("voicing", "CLP-147"),
    JobStatus.RENDERING: ("rendering", "CLP-148"),
}
"""Status -> (what the stage does, the story that builds it)."""


def _not_built(what: str, issue: str) -> StageDef:
    def run(job: Job, ctx: StageContext) -> None:
        raise SkipJob(f"commentary {what} is not built yet ({issue})")

    return StageDef(run)


BUILT: Mapping[JobStatus, StageDef] = {
    JobStatus.FETCHING_SOURCES: commentary_sources.FETCH_STAGE,  # CLP-145
    JobStatus.TRANSCRIBING: commentary_sources.TRANSCRIBE_STAGE,  # CLP-145
}
"""Stages whose story has shipped."""

STAGES: Mapping[JobStatus, StageDef] = {
    **{status: _not_built(what, issue) for status, (what, issue) in NOT_BUILT.items()},
    **BUILT,
}
