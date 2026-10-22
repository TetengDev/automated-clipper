"""Clip boundary polish (CLP-68): snap clip edges to scene cuts, then trim dead air.

Times are seconds in the padded section file (`clips/<rank>/source.mp4`); `words` are
(start, end) pairs in the same timeline. `snap_to_cuts` and `trim_dead_air` are pure.

`detect_cuts` runs PySceneDetect's `ContentDetector` on a downscaled stream in a child
process (`python -I -m clipper.pipeline.boundaries <file>`) under a hard timeout, so a
hung decoder cannot stall the stage. Any failure logs a warning and yields no cuts: the
clip then renders with its original boundaries. Cuts are cached as `<section>.cuts.json`
(reused by reruns and by the face-tracked reframe, CLP-58).
"""

from __future__ import annotations

import json
import logging
import math
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from clipper.pipeline.common import Deadline, run_media, write_json_atomic
from clipper.pipeline.ytdlp import Runner
from clipper.redact import redact

logger = logging.getLogger(__name__)

DETECT_TIMEOUT_S = 120.0
SNAP_TOLERANCE_S = 1.0
LEAD_IN_S = 0.25
"""Most silence kept before the first word."""
TAIL_S = 0.4
"""Most silence kept after the last word."""

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
    """Move each boundary to the nearest cut within `tol` that is not inside a word, keeps
    the clip within [min_s, max_s] and stays inside `section`; else keep it."""

    def ok(a: float, b: float, edge: float) -> bool:
        return (
            min_s <= b - a <= max_s
            and section[0] <= edge <= section[1]
            and not any(ws < edge < we for ws, we in words)
        )

    def best(edge: float, valid: Any) -> float:
        near = sorted((c for c in cuts if abs(c - edge) <= tol), key=lambda c: abs(c - edge))
        return next((c for c in near if valid(c)), edge)

    start = best(start, lambda c: ok(c, end, c))
    end = best(end, lambda c: ok(start, c, c))
    return start, end


def trim_dead_air(
    start: float, end: float, words: Words, *, min_s: float = 0.0
) -> tuple[float, float]:
    """Start at most LEAD_IN_S before the first word in the clip and end at most TAIL_S
    after the last one. Only shrinks; no words in range or a result under `min_s` keeps
    the clip as it is."""
    inside = [(ws, we) for ws, we in words if we > start and ws < end]
    if not inside:
        return start, end
    a = max(start, min(ws for ws, _ in inside) - LEAD_IN_S)
    b = min(end, max(we for _, we in inside) + TAIL_S)
    return (a, b) if b - a >= min_s else (start, end)


def _cut_list(data: Any) -> list[float]:
    if not isinstance(data, list) or not all(
        isinstance(c, int | float) and not isinstance(c, bool) and math.isfinite(c) and c >= 0
        for c in data
    ):
        raise ValueError("not a list of cut times")
    return [float(c) for c in data]


def detect_cuts(section: Path, deadline: Deadline, *, runner: Runner | None = None) -> list[float]:
    """Scene-cut times (seconds) in `section`; [] with a warning on any failure."""
    cache = section.with_name(f"{section.name}.cuts.json")
    with suppress(OSError, ValueError):
        return _cut_list(json.loads(cache.read_text(encoding="utf-8")))
    args = [sys.executable, "-I", "-m", __name__, str(section)]
    try:
        out = run_media(args, deadline.timeout(DETECT_TIMEOUT_S), "scene detection", runner=runner)
        cuts = _cut_list(json.loads(out.stdout))
    except Exception as exc:
        reason = redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        logger.warning("scene detection failed for %s, boundaries kept: %s", section, reason)
        return []
    with suppress(OSError):  # the cache is best effort
        write_json_atomic(cache, cuts, indent=None)
    return cuts


def scan(path: Path) -> list[float]:
    """Run ContentDetector in this process (the child side of `detect_cuts`)."""
    from scenedetect import ContentDetector, SceneManager, open_video

    manager = SceneManager()
    manager.auto_downscale = True  # decode at ~256 px wide: cuts survive, cost does not
    manager.add_detector(ContentDetector())
    manager.detect_scenes(open_video(str(path)))
    return [round(begin.seconds, 3) for begin, _ in manager.get_scene_list()[1:]]


if __name__ == "__main__":  # pragma: no cover - child process of detect_cuts
    print(json.dumps(scan(Path(sys.argv[1]))))
