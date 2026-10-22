"""Clip boundary polish (CLP-68): trim dead air, then snap clip edges to scene cuts.

Times are seconds in the padded section file (`clips/<rank>/source.mp4`); `words` are
(start, end) pairs in the same timeline. `trim_dead_air`, `snap_to_cuts` and `polish`
(both, in that order) are pure. The trim comes first and is cut-aware, so each edge ends
up either on a cut or at the trim limit, and never a flash of another shot: a snap of
the trimmed edge is never undone by a later trim. An end on a cut stops FRAME_GUARD_S
before it, so the next shot's first frame is never rendered.

`detect_cuts` runs PySceneDetect's `ContentDetector` on a downscaled stream in a child
process (`python -I -m clipper.pipeline.boundaries <file>`) under a hard timeout, so a
hung decoder cannot stall the stage; the child limits its own memory and file protocols
(`harden`). Any failure logs a warning and yields no cuts (and is not cached): the clip
then keeps its edges apart from the trim. Cuts are cached as `<section>.cuts.json`, keyed
by the file's size and mtime (reused by reruns and by the face-tracked reframe, CLP-58).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from clipper.pipeline.common import Deadline, run_media, write_json_atomic
from clipper.pipeline.ytdlp import Runner
from clipper.redact import redact

logger = logging.getLogger(__name__)

DETECT_TIMEOUT_S = 120.0
DECODER_MEMORY_BYTES = 3 * 1024**3
"""Address-space cap of the detection child (a hostile file cannot exhaust the host)."""
CACHE_VERSION = 1
SNAP_TOLERANCE_S = 1.0
LEAD_IN_S = 0.25
"""Most silence kept before the first word."""
TAIL_S = 0.4
"""Most silence kept after the last word."""
FRAME_GUARD_S = 1 / 60
"""An end on a cut stops this much before it: half a frame at 30 fps."""

Words = Sequence[tuple[float, float]]


def snap_to_cuts(
    start: float,
    end: float,
    cuts: Sequence[float],
    words: Words,
    min_s: float,
    max_s: float,
    tol: float = SNAP_TOLERANCE_S,
    *,
    section: tuple[float, float] = (0.0, math.inf),
) -> tuple[float, float]:
    """Move each edge to the nearest cut within `tol` (an end to just before it) if the new
    edge is not inside a word, no whole word lies between the old and new edge (a snap never
    adds or drops a word), the clip stays within [min_s, max_s] and inside `section`."""

    def ok(a: float, b: float, old: float, new: float) -> bool:
        lo, hi = sorted((old, new))
        return (
            min_s <= b - a <= max_s
            and section[0] <= new <= section[1]
            and not any(ws < new < we or lo <= ws <= we <= hi for ws, we in words)
        )

    def best(edge: float, shift: float, valid: Callable[[float], bool]) -> float:
        near = sorted(
            (c - shift for c in cuts if abs(c - edge) <= tol), key=lambda c: abs(c - edge)
        )
        return next((c for c in near if valid(c)), edge)

    start = best(start, 0.0, lambda c: ok(c, end, start, c))
    end = best(end, FRAME_GUARD_S, lambda c: ok(start, c, end, c))
    return start, end


def trim_dead_air(
    start: float, end: float, words: Words, cuts: Sequence[float] = (), *, min_s: float = 0.0
) -> tuple[float, float]:
    """Start at most LEAD_IN_S before the first word in the clip and end at most TAIL_S
    after the last one; a cut in that dead air moves the edge onto it (the last cut before
    the first word, the first after the last word), so no sliver of another shot is left.
    Only shrinks; no words in range or a result under `min_s` keeps the clip as it is."""
    inside = [(ws, we) for ws, we in words if we > start and ws < end]
    if not inside:
        return start, end
    first, last = min(ws for ws, _ in inside), max(we for _, we in inside)
    a = max([start, first - LEAD_IN_S, *(c for c in cuts if start <= c < first)])
    b = min([end, last + TAIL_S, *(c - FRAME_GUARD_S for c in cuts if last < c <= end)])
    return (a, b) if b - a >= min_s else (start, end)


def polish(
    start: float,
    end: float,
    cuts: Sequence[float],
    words: Words,
    min_s: float,
    max_s: float,
    *,
    section: tuple[float, float] = (0.0, math.inf),
) -> tuple[float, float]:
    """The cut-aware trim, then the snap of the trimmed edges."""
    start, end = trim_dead_air(start, end, words, cuts, min_s=min_s)
    return snap_to_cuts(start, end, cuts, words, min_s, max_s, section=section)


def _cut_list(data: Any) -> list[float]:
    if not isinstance(data, list) or not all(
        isinstance(c, int | float) and not isinstance(c, bool) and math.isfinite(c) and c >= 0
        for c in data
    ):
        raise ValueError("not a list of cut times")
    return [float(c) for c in data]


def detect_cuts(section: Path, deadline: Deadline, *, runner: Runner | None = None) -> list[float]:
    """Scene-cut times (seconds) in `section`; [] with a warning on any failure (failures are
    not cached, so a later run tries again)."""
    cache = section.with_name(f"{section.name}.cuts.json")
    with suppress(OSError, ValueError, TypeError, KeyError):
        stat = section.stat()
        key = {"v": CACHE_VERSION, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if {k: cached[k] for k in key} == key:
            return _cut_list(cached["cuts"])
    args = [sys.executable, "-I", "-m", __name__, str(section)]
    try:
        out = run_media(args, deadline.timeout(DETECT_TIMEOUT_S), "scene detection", runner=runner)
        cuts = _cut_list(json.loads(out.stdout))
        stat = section.stat()
    except Exception as exc:
        reason = redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        logger.warning("scene detection failed for %s, edges kept: %s", section, reason)
        logger.debug("scene detection failure", exc_info=True)
        return []
    entry = {"v": CACHE_VERSION, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "cuts": cuts}
    with suppress(OSError):  # the cache is best effort
        write_json_atomic(cache, entry, indent=None)
    return cuts


def harden() -> None:
    """Child-side limits, set before cv2 loads: local files only, one BLAS thread, and a
    capped address space where the OS allows it (macOS refuses RLIMIT_AS)."""
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (DECODER_MEMORY_BYTES, DECODER_MEMORY_BYTES))
    except (ImportError, ValueError, OSError):
        logger.debug("RLIMIT_AS not available", exc_info=True)


def scan(path: Path) -> list[float]:
    """Run ContentDetector in this process (the child side of `detect_cuts`)."""
    from scenedetect import ContentDetector, SceneManager, open_video

    manager = SceneManager()
    manager.auto_downscale = True  # decode at ~256 px wide: cuts survive, cost does not
    manager.add_detector(ContentDetector())
    manager.detect_scenes(open_video(str(path)))
    return [round(begin.seconds, 3) for begin, _ in manager.get_scene_list()[1:]]


if __name__ == "__main__":  # pragma: no cover - child process of detect_cuts
    harden()
    print(json.dumps(scan(Path(sys.argv[1]))))
