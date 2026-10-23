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
from types import MappingProxyType
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
WORD_EPS_S = 0.15
"""The trim may cut this far into the first or last word to land on a scene cut."""
MAX_CACHE_BYTES = 1024 * 1024
MAX_CUTS = 5000
"""Cuts kept per section (sorted, unique), so the work done on them stays bounded."""
CHILD_ENV = MappingProxyType({"MALLOC_ARENA_MAX": "2"})
"""Extra env for the detection child (glibc reads it at startup, so not in `harden`)."""

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
    adds or drops a word), the clip stays within [min_s, max_s] and inside `section`. An edge
    already on a cut stays. An outward snap may add back up to `tol` of silence after a
    trim; that is deliberate: an edge on a cut beats a tight edge."""

    def ok(a: float, b: float, old: float, new: float) -> bool:
        lo, hi = sorted((old, new))
        return (
            min_s <= b - a <= max_s
            and section[0] <= new <= section[1]
            and not any(ws < new < we or lo <= ws <= we <= hi for ws, we in words)
        )

    def best(edge: float, shift: float, valid: Callable[[float], bool]) -> float:
        if any(abs(c - shift - edge) < 1e-9 for c in cuts):
            return edge
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
    after the last one; a cut in that dead air (or up to WORD_EPS_S into the first or last
    word) moves the edge onto it (the last such cut at the start, the first at the end), so
    no sliver of another shot is left. Only shrinks; no words in range keeps the clip. If
    the result is under `min_s`, the cut-driven moves are kept first and the dead-air trim
    is tried on one edge at a time, before falling back to the untrimmed clip."""
    inside = [(ws, we) for ws, we in words if we > start and ws < end]
    if not inside:
        return start, end
    first_ws, first_we = min(inside)
    last_ws, last_we = max(inside, key=lambda w: (w[1], w[0]))
    first, last = first_ws, last_we
    # A cut may land up to WORD_EPS_S into the edge word, but never past it (short words).
    a_lim = min(first + WORD_EPS_S, first_we)
    b_lim = max(last - WORD_EPS_S, last_ws)
    a_cut = max([start, *(c for c in cuts if start <= c < a_lim)])
    b_cut = min([end, *(c - FRAME_GUARD_S for c in cuts if b_lim < c <= end)])
    a, b = max(a_cut, first - LEAD_IN_S), min(b_cut, last + TAIL_S)
    tries = ((a, b), (a, b_cut), (a_cut, b), (a_cut, b_cut), (a_cut, end), (start, b_cut))
    return next(((x, y) for x, y in tries if y - x >= min_s), (start, end))


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
    return sorted({float(c) for c in data})[:MAX_CUTS]


def _read_cache(cache: Path) -> Any:
    """The parsed cache, or None if it is missing, too big or not JSON. The size cap is
    applied to the bytes actually read, so a file that grows after a stat() is still bounded."""
    with suppress(OSError, ValueError, RecursionError), cache.open("rb") as fh:
        raw = fh.read(MAX_CACHE_BYTES + 1)
        if len(raw) <= MAX_CACHE_BYTES:
            return json.loads(raw)
    return None


def cached_child[T](
    section: Path,
    field: str,
    args: Sequence[str],
    parse: Callable[[Any], T],
    deadline: Deadline,
    *,
    what: str,
    fallback: str,
    timeout_s: float,
    runner: Runner | None = None,
    read: Callable[[Any], Any] = lambda out: json.loads(out.stdout),
) -> T | None:
    """`parse` of the JSON a hardened child (`args`) prints about `section` (or of what
    `read` makes of its run result), cached as `<section>.<field>.json` keyed by the
    file's size and mtime; None with a warning on any failure (failures are not cached,
    so a later run tries again)."""
    cache = section.with_name(f"{section.name}.{field}.json")
    with suppress(OSError, ValueError, TypeError, KeyError, RecursionError):
        key = _key(section)
        cached = _read_cache(cache)
        if cached is not None and {k: cached[k] for k in key} == key:
            return parse(cached[field])
    try:
        key = _key(section)
        out = run_media(args, deadline.timeout(timeout_s), what, runner=runner, env=CHILD_ENV)
        data = read(out)
        result = parse(data)
        unchanged = _key(section) == key  # not replaced while it was being read
    except Exception as exc:
        reason = redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        logger.warning("%s failed for %s, %s: %s", what, section, fallback, reason)
        logger.debug("%s failure", what, exc_info=True)
        return None
    if unchanged:
        with suppress(OSError):  # the cache is best effort
            write_json_atomic(cache, {**key, field: data}, indent=None)
    return result


def detect_cuts(section: Path, deadline: Deadline, *, runner: Runner | None = None) -> list[float]:
    """Scene-cut times (seconds) in `section`; [] with a warning on any failure."""
    args = [sys.executable, "-I", "-m", __name__, str(section)]
    cuts = cached_child(
        section, "cuts", args, _cut_list, deadline,
        what="scene detection", fallback="edges kept", timeout_s=DETECT_TIMEOUT_S, runner=runner,
    )  # fmt: skip
    return cuts or []


def _key(section: Path) -> dict[str, int]:
    stat = section.stat()
    return {"v": CACHE_VERSION, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def harden() -> None:
    """Child-side limits, set before cv2 loads: local files only, two decoder threads, one
    BLAS thread, and a
    capped address space where the OS allows it (macOS refuses RLIMIT_AS)."""
    # `key;value|key;value` (OpenCV hands it to av_dict_parse_string with ";" and "|").
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file|threads;2"
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
