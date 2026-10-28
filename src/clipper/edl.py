"""Edit Decision List (EDL): the composition props the Remotion `Clip` template renders.

This pydantic model is the source of truth. `export_json_schema()` produces the JSON Schema
committed at `render/schema/edl.schema.json`, which the TypeScript side validates with ajv
(plus `render/src/validate.ts` mirrors the cross-field checks below that JSON Schema cannot
express). A test asserts the committed file never drifts from this module.

Regenerate after changing the model:

    uv run python -m clipper.edl > render/schema/edl.schema.json

Timeline conventions:
- `source.in_s` / `source.out_s` are seconds in the *source* media.
- `words[*].start_s` / `end_s` are also seconds in the *source* timeline (straight from the
  transcript), so a re-trim only changes `source`; the renderer drops words outside the cut.
- `hook.duration_s` and `crop[*].t_s` are seconds from the start of the clip (output time).

Schema v2 (CLP-73) adds jump cuts: `segments` are the kept ranges (source time, on the
frame grid, spanning `source` exactly) and `cuts` record what was cut or only proposed.
Without segments (every v1 EDL) the clip is the one range `source`; `spans` gives the
ranges either way. The renderer maps the words onto the output timeline.
"""

from __future__ import annotations

import json
import math
import sys
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EDL_SCHEMA_VERSION = 2
FPS = 30
WIDTH = 1080
HEIGHT = 1920
MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 180.0
MAX_WORDS = 5000
MAX_CROP_KEYS = 2000
MIN_SEGMENT_SECONDS = 0.3
MAX_SEGMENTS = 600
"""MAX_CLIP_SECONDS of MIN_SEGMENT_SECONDS segments."""
MAX_CUTS = 1000
GRID_TOLERANCE_S = 0.001
"""A segment edge this close to a whole frame is on the frame grid."""
_EPS = 1e-6
# Relative POSIX path under the media root: no leading '/', '.', or '-'; no backslashes.
# '..' segments are rejected by the validator below (the Rust regex engine has no lookahead).
SOURCE_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.\-/ ]{0,511}$"


class Layout(StrEnum):
    """Frame layout: the source fit over a blurred copy, or a 9:16 crop following faces."""

    BLURRED_FILL = "blurred_fill"
    FACE_TRACK = "face_track"


class HookStyle(StrEnum):
    STAKES = "stakes"
    QUESTION = "question"
    QUOTE = "quote"


class CaptionStyle(StrEnum):
    BOLD_POP = "bold_pop"


class CutKind(StrEnum):
    SILENCE = "silence"
    STUTTER = "stutter"
    RETAKE = "retake"
    FALSE_START = "false_start"
    FILLER = "filler"


def frames(seconds: float) -> int:
    """Whole frames at FPS, rounded half up like Math.round in render/src."""
    return math.floor(seconds * FPS + 0.5)


class _Strict(BaseModel):
    # allow_inf_nan=False: JSON Schema/ajv only accept finite numbers; match that.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SourceSegment(_Strict):
    path: str = Field(
        pattern=SOURCE_PATH_PATTERN,
        description="Source media path relative to the media root (POSIX separators).",
    )
    in_s: float = Field(ge=0, description="Cut start, seconds in the source.")
    out_s: float = Field(gt=0, description="Cut end, seconds in the source.")

    @model_validator(mode="after")
    def _check(self) -> SourceSegment:
        if ".." in self.path.split("/"):
            raise ValueError("source.path must not contain '..' segments")
        duration = self.out_s - self.in_s
        if not MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS:
            raise ValueError(
                f"source.out_s - source.in_s must be between {MIN_CLIP_SECONDS:g} and "
                f"{MAX_CLIP_SECONDS:g} seconds"
            )
        return self

    @property
    def duration_s(self) -> float:
        return self.out_s - self.in_s


class Hook(_Strict):
    text: str = Field(min_length=1, max_length=120, pattern=r"\S")
    style: HookStyle = HookStyle.STAKES
    duration_s: float = Field(2.0, ge=1.0, le=3.0, description="Seconds from clip start.")
    ai_generated: bool = Field(False, description="Synthetic voice or text (CLP-96 disclosure).")


class Word(_Strict):
    text: str = Field(min_length=1, max_length=64, pattern=r"\S")
    start_s: float = Field(ge=0, description="Seconds in the source timeline.")
    end_s: float = Field(ge=0, description="Seconds in the source timeline.")

    @model_validator(mode="after")
    def _check(self) -> Word:
        if self.end_s < self.start_s:
            raise ValueError("word end_s must be >= start_s")
        return self


class Segment(_Strict):
    """A kept range of the clip, seconds in the source."""

    in_s: float = Field(ge=0)
    out_s: float = Field(gt=0)
    ai_generated: bool = Field(False, description="Generated footage, e.g. b-roll (CLP-96).")

    @model_validator(mode="after")
    def _check(self) -> Segment:
        if any(abs(t * FPS - frames(t)) > GRID_TOLERANCE_S * FPS for t in (self.in_s, self.out_s)):
            raise ValueError("segment edges must be on the frame grid")
        if self.out_s - self.in_s < MIN_SEGMENT_SECONDS - _EPS:
            raise ValueError(f"segments must be at least {MIN_SEGMENT_SECONDS:g} seconds")
        return self


class Cut(_Strict):
    """A range cut out of the clip (`applied`) or only proposed, seconds in the source."""

    in_s: float = Field(ge=0)
    out_s: float = Field(gt=0)
    kind: CutKind
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=200, pattern=r"\S")
    applied: bool

    @model_validator(mode="after")
    def _check(self) -> Cut:
        if self.out_s <= self.in_s:
            raise ValueError("cut out_s must be > in_s")
        return self


class CropKey(_Strict):
    """Where the 9:16 crop window is centred at `t_s` (seconds from the clip start), as a
    share of the source width. Keys are linear in between; two keys at one time are a jump
    (a scene cut)."""

    t_s: float = Field(ge=0, description="Seconds from the clip start.")
    x_center: float = Field(ge=0, le=1, description="Crop centre, share of the source width.")


class EDL(_Strict):
    """Composition props for the Remotion `Clip` composition (1080x1920 @ 30 fps)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, title="ClipEDL")

    schema_version: Literal[1, 2] = EDL_SCHEMA_VERSION
    source: SourceSegment
    layout: Layout = Layout.BLURRED_FILL
    hook: Hook | None = None
    words: tuple[Word, ...] = Field(default=(), max_length=MAX_WORDS)
    captions_enabled: bool = True
    caption_style: CaptionStyle = CaptionStyle.BOLD_POP
    progress_bar: bool = True
    crop: Annotated[tuple[CropKey, ...], Field(min_length=1, max_length=MAX_CROP_KEYS)] | None = (
        None
    )
    segments: (
        Annotated[tuple[Segment, ...], Field(min_length=1, max_length=MAX_SEGMENTS)] | None
    ) = None
    cuts: tuple[Cut, ...] = Field(default=(), max_length=MAX_CUTS)

    @model_validator(mode="after")
    def _check(self) -> EDL:
        if self.schema_version == 1 and (self.segments is not None or self.cuts):
            raise ValueError("segments and cuts need schema_version 2")
        segs = self.segments or ()
        if any(b.in_s < a.out_s for a, b in zip(segs, segs[1:], strict=False)):
            raise ValueError("segments must be sorted and must not overlap")
        if segs and (
            abs(segs[0].in_s - self.source.in_s) > _EPS
            or abs(segs[-1].out_s - self.source.out_s) > _EPS
        ):
            raise ValueError("segments must span source from in_s to out_s")
        if not MIN_CLIP_SECONDS - _EPS <= self.duration_s <= MAX_CLIP_SECONDS:
            raise ValueError(
                f"the segments must total between {MIN_CLIP_SECONDS:g} and "
                f"{MAX_CLIP_SECONDS:g} seconds"
            )
        for name, key, times in (
            ("words", "start_s", [w.start_s for w in self.words]),
            ("crop", "t_s", [k.t_s for k in self.crop or ()]),
        ):
            bad = next((i for i in range(1, len(times)) if times[i] < times[i - 1]), None)
            if bad is not None:
                raise ValueError(f"{name} must be sorted by {key} (index {bad})")
        if (self.layout is Layout.FACE_TRACK) != (self.crop is not None):
            raise ValueError(
                "crop is required when layout is face_track"
                if self.crop is None
                else "crop is only allowed when layout is face_track"
            )
        if self.hook is not None and self.hook.duration_s > self.duration_s:
            raise ValueError("hook.duration_s must not exceed the clip duration")
        return self

    @property
    def spans(self) -> tuple[tuple[float, float], ...]:
        """The kept ranges (source seconds); a v1 EDL is its one range."""
        if self.segments is None:
            return ((self.source.in_s, self.source.out_s),)
        return tuple((s.in_s, s.out_s) for s in self.segments)

    @property
    def duration_s(self) -> float:
        return sum(b - a for a, b in self.spans)

    @property
    def duration_in_frames(self) -> int:
        # Per segment, round half up, matching Math.round in render/src/timeline.ts.
        return max(1, sum(frames(b - a) for a, b in self.spans))

    def to_props(self) -> dict[str, Any]:
        """JSON-ready composition props (defaults included, so TS never guesses). An unset
        `ai_generated` flag is left out, so props without AI elements are unchanged."""
        props = self.model_dump(mode="json")
        for element in [props["hook"], *(props["segments"] or ())]:
            if element is not None and not element["ai_generated"]:
                del element["ai_generated"]
        return props


def has_ai_elements(edl: EDL) -> bool:
    """Whether the clip has an AI-generated element (a synthetic hook voice or generated
    footage): the platforms' AI disclosure flags must then be set (CLP-96)."""
    hook = edl.hook is not None and edl.hook.ai_generated
    return hook or any(s.ai_generated for s in edl.segments or ())


def parse_edl(data: str | bytes | dict[str, Any]) -> EDL:
    """Validate EDL JSON with strict JSON types (no "1.5" -> 1.5 coercion), like ajv does.

    Raises pydantic.ValidationError on invalid input.
    """
    text = json.dumps(data) if isinstance(data, dict) else data
    return EDL.model_validate_json(text, strict=True)


def export_json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for the EDL, shared with `render/` (ajv)."""
    schema = EDL.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://clipper.local/schema/edl.schema.json"
    return schema


def dump_json_schema() -> str:
    return json.dumps(export_json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.stdout.write(dump_json_schema())
