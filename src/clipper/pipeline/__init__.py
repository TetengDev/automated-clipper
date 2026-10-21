"""Pipeline stage registry. Each stage (CLP-13..16) lives in its own module here and is
added to `build_stages()`; the contract is in `clipper.jobs.stages`."""

from __future__ import annotations

from collections.abc import Mapping

from clipper.jobs.models import JobStatus
from clipper.jobs.stages import DEFAULT_STAGES, StageDef


def build_stages() -> Mapping[JobStatus, StageDef]:
    return {**DEFAULT_STAGES}
