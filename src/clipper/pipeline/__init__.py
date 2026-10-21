"""Pipeline stage registry. Each stage (CLP-13..16) lives in its own module here and is
added to `build_stages()`; the contract is in `clipper.jobs.stages`.

Artifacts, in order (each stage reads the ones before it from the job's media dir):
fetching_audio -> audio_16k.flac (+ meta.json, signals.json), transcribing ->
transcript.json, selecting -> highlights.json, fetching_clips -> sections.json,
rendering -> `clips` rows in `pending_review` (CLP-63), after which the job is `clips_ready`.

Stage modules are imported inside `build_stages()` so importing this package stays cheap
and cannot form an import cycle with `clipper.jobs`.
"""

from __future__ import annotations

from collections.abc import Mapping

from clipper.jobs.models import JobStatus
from clipper.jobs.stages import DEFAULT_STAGES, StageDef


def build_stages() -> Mapping[JobStatus, StageDef]:
    from clipper.pipeline import fetch_audio, fetch_clips, render_clips, select, transcribe

    return {
        **DEFAULT_STAGES,
        JobStatus.FETCHING_AUDIO: fetch_audio.STAGE,
        JobStatus.TRANSCRIBING: transcribe.STAGE,
        JobStatus.SELECTING: select.STAGE,
        JobStatus.FETCHING_CLIPS: fetch_clips.STAGE,
        JobStatus.RENDERING: render_clips.STAGE,
    }
