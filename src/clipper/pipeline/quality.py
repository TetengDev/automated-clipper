"""Quality gates on a rendered clip, before review (CLP-74). Ideas from herk-edit-kit's
validate-plan and quality gates, in our own code.

`check_clip` returns machine-readable reasons; an empty list means the clip passed:

    caption_mismatch   an EDL word inside the clip never shows as a caption (frame rounding)
    word_crosses_join  a word runs over a jump-cut join, so half of it is cut
    late_speech        the first word starts more than 0.25 s (plus one frame of rounding)
                       into the clip; not checked when the clip starts on a scene cut,
                       where the boundary snap may add up to 1 s of lead-in on purpose
    loudness           integrated loudness off -14 LUFS by more than 1 LU, or true peak
                       above -1 dBTP (ffmpeg ebur128 on the final video)
    join_asr           a tiny Whisper pass on +-1 s around a join hears other words than the
                       EDL has there (Jaccard < 0.6, joins with 4+ words only): a cut that
                       clips speech
    gate_error         a gate could not run (logged); never blocks the clip

Segments on the frame grid need no gate here: `parse_edl` rejects any EDL whose segments
are off the grid before it is rendered. The gates share GATES_TIMEOUT_S of the stage.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from clipper.clips_repo import GATE_ERROR
from clipper.edl import EDL, FPS, frames
from clipper.pipeline.common import Deadline, MediaError, run_media, strip_control
from clipper.pipeline.transcribe import ChunkTranscript, FasterWhisperTranscriber
from clipper.redact import redact
from clipper.render import FFMPEG_INPUT_GUARD, TARGET_LUFS, join_times, segment_props

logger = logging.getLogger(__name__)

GATES_TIMEOUT_S = 180.0
"""All gates of one clip, incl. a first fetch of the tiny model (about 75 MB)."""
SPEECH_START_MAX_S = 0.25
LOUDNESS_TOLERANCE_LU = 1.0
MAX_TRUE_PEAK_DB = -1.0
JOIN_WINDOW_S = 1.0
MIN_JOIN_JACCARD = 0.6
MIN_JOIN_WORDS = 4
"""A join with fewer EDL words around it is not judged (too little to compare)."""
ASR_MODEL = "tiny"
_TOLERANCE_S = 0.5 / FPS
_NUMBER = r"(-?(?:\d+(?:\.\d+)?|inf))"
_EBUR128 = re.compile(
    rf"Summary:.*?\bI:\s*{_NUMBER}\s*LUFS.*?\bPeak:\s*{_NUMBER}\s*dBFS", re.DOTALL
)
_NOT_WORD = re.compile(r"[^\w]+")


class Asr(Protocol):
    def ensure_model(self, timeout_s: float) -> Path | None: ...

    def transcribe(
        self, audio: Path, *, language: str | None, timeout_s: float
    ) -> ChunkTranscript: ...


@dataclass
class OnceAsr:
    """`asr` whose model is checked (and fetched if needed) once per stage run: after a
    failed fetch the remaining clips skip the join check (`gate_error`) instead of each
    waiting out another download."""

    asr: Asr
    ready: bool = False
    failure: str | None = None

    def ensure_model(self, timeout_s: float) -> Path | None:
        if self.failure is not None:
            raise MediaError(f"tiny model unavailable this run: {self.failure}")
        if not self.ready:
            try:
                self.asr.ensure_model(timeout_s=timeout_s)
            except Exception as exc:
                self.failure = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                raise
            self.ready = True
        return None

    def transcribe(self, audio: Path, *, language: str | None, timeout_s: float) -> ChunkTranscript:
        return self.asr.transcribe(audio, language=language, timeout_s=timeout_s)


def default_asr(*, cpu_threads: int) -> OnceAsr:
    return OnceAsr(
        FasterWhisperTranscriber(model=ASR_MODEL, compute_type="int8", cpu_threads=cpu_threads)
    )


def output_words(edl: EDL) -> list[tuple[str, float, float]]:
    """(text, start, end) of each caption on the output timeline, as the renderer maps them
    (render/src/timeline.ts `outputWords` on the pre-cut props): whole-frame segments, a
    word overlapping none of them dropped, times inside a cut moved onto its join."""
    props = segment_props(edl)
    segs = props.get("segments") or [props["source"]]
    spans = [(frames(s["in_s"]) / FPS, frames(s["out_s"] - s["in_s"]) / FPS) for s in segs]

    def out(t: float) -> float:
        return sum(min(max(t - a, 0.0), n) for a, n in spans)

    return [
        (w["text"], out(w["start_s"]), out(w["end_s"]))
        for w in props["words"]
        if any(w["start_s"] < a + n and w["end_s"] > a for a, n in spans)
    ]


def edl_reasons(edl: EDL, *, starts_on_cut: bool = False) -> list[str]:
    """The gates that need only the EDL."""
    reasons = []
    shown = output_words(edl)
    expected = [
        w.text for w in edl.words if any(w.start_s < b and w.end_s > a for a, b in edl.spans)
    ]
    if [t for t, _, _ in shown] != expected:
        reasons.append("caption_mismatch")
    spans = edl.spans
    joins = [b for _, b in spans[:-1]] + [a for a, _ in spans[1:]]
    if any(
        w.start_s < t - _TOLERANCE_S and w.end_s > t + _TOLERANCE_S
        for t in joins
        for w in edl.words
    ):
        reasons.append("word_crosses_join")
    # One frame of slack: a jump-cut segment start is rounded down onto the frame grid.
    if not starts_on_cut and shown and shown[0][1] > SPEECH_START_MAX_S + 1 / FPS + 1e-9:
        reasons.append("late_speech")
    return reasons


def loudness_ok(integrated: float, true_peak: float) -> bool:
    return (
        math.isfinite(integrated)
        and abs(integrated - TARGET_LUFS) <= LOUDNESS_TOLERANCE_LU
        and true_peak <= MAX_TRUE_PEAK_DB
    )


def parse_ebur128(stderr: str) -> tuple[float, float]:
    """(integrated LUFS, true peak dBFS) from the summary ffmpeg's ebur128 filter prints."""
    found = _EBUR128.findall(stderr or "")
    if not found:
        raise MediaError("could not parse the ebur128 summary")
    integrated, peak = found[-1]
    return float(integrated), float(peak)


def measure_loudness(video: Path, timeout_s: float) -> tuple[float, float]:
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", *FFMPEG_INPUT_GUARD,
        "-i", str(video), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-",
    ]  # fmt: skip
    return parse_ebur128(run_media(args, timeout_s, "ffmpeg ebur128").stderr)


def _norm(text: str) -> str:
    return _NOT_WORD.sub("", text.lower())


def _join_windows(edl: EDL) -> list[tuple[float, set[str]]]:
    """(join time, EDL words within JOIN_WINDOW_S of it), for the joins worth judging."""
    shown = output_words(edl)
    windows = []
    for t in join_times(edl):
        near = {_norm(w) for w, a, b in shown if _near(t, a, b)}
        near.discard("")
        if len(near) >= MIN_JOIN_WORDS:
            windows.append((t, near))
    return windows


def _near(t: float, start: float, end: float) -> bool:
    return start < t + JOIN_WINDOW_S and end > t - JOIN_WINDOW_S


def join_mismatches(edl: EDL, heard: Sequence[tuple[str, float, float]]) -> list[float]:
    """Joins where the heard words (text, output start, end) differ from the EDL's; a word
    counts on either side when it overlaps the window."""
    bad = []
    for t, expected in _join_windows(edl):
        got = {_norm(w) for w, a, b in heard if _near(t, a, b)}
        got.discard("")
        if len(expected & got) / len(expected | got) < MIN_JOIN_JACCARD:
            bad.append(t)
    return bad


def _hear(
    video: Path, language: str | None, asr: Asr, gates: Deadline
) -> list[tuple[str, float, float]]:
    """(word, start, end) the tiny model hears in the clip's audio."""
    asr.ensure_model(timeout_s=gates.timeout(GATES_TIMEOUT_S))
    for stale in video.parent.glob(".gates-*"):  # left by a worker killed mid-gate
        shutil.rmtree(stale, ignore_errors=True)
    with tempfile.TemporaryDirectory(dir=video.parent, prefix=".gates-") as tmp:
        audio = Path(tmp) / "audio.flac"
        args = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            *FFMPEG_INPUT_GUARD, "-i", str(video),
            "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(audio),
        ]  # fmt: skip
        run_media(args, gates.timeout(GATES_TIMEOUT_S), "ffmpeg audio for the join check")
        chunk = asr.transcribe(audio, language=language, timeout_s=gates.timeout(GATES_TIMEOUT_S))
    return [(w.word, w.start, w.end) for w in chunk.words]


def check_clip(
    edl: EDL,
    video: Path,
    *,
    language: str | None,
    asr: Asr,
    deadline: Deadline,
    starts_on_cut: bool = False,
) -> list[str]:
    """Every gate's reasons for the rendered `video` of `edl`. A gate that errors is logged
    and adds `gate_error` (once, at the end) instead of raising."""
    gates = Deadline(budget_s=min(GATES_TIMEOUT_S, deadline.remaining()))
    reasons: list[str] = []
    errored = False

    def gate(name: str, run: Callable[[], list[str]]) -> None:
        nonlocal errored
        try:
            reasons.extend(run())
        except Exception as exc:  # a gate must never block a clip
            detail = strip_control(redact(str(exc).splitlines()[0] if str(exc) else repr(exc)))
            logger.warning("quality gate %s could not run: %s", name, detail[:300])
            errored = True

    def loudness() -> list[str]:
        ok = loudness_ok(*measure_loudness(video, gates.timeout(GATES_TIMEOUT_S)))
        return [] if ok else ["loudness"]

    def joins() -> list[str]:
        if not _join_windows(edl):
            return []
        return ["join_asr"] if join_mismatches(edl, _hear(video, language, asr, gates)) else []

    gate("edl", lambda: edl_reasons(edl, starts_on_cut=starts_on_cut))
    gate("loudness", loudness)
    gate("join_asr", joins)
    return [*reasons, GATE_ERROR] if errored else reasons
