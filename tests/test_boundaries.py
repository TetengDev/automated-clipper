"""Clip boundary polish (CLP-68): scene-cut snap, dead-air trim, scene detection."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from clipper.pipeline import boundaries as bd
from clipper.pipeline.common import Deadline
from clipper.pipeline.ytdlp import CommandTimeout, RunResult

MIN, MAX = 20, 60
G = bd.FRAME_GUARD_S
"""An end on a cut stops just before the cut, so the next shot's first frame is never in."""


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        ([9.3], (9.3, 40.0)),  # cut 0.7 s before the start: inside the tolerance
        ([10.9], (10.9, 40.0)),
        ([8.9], (10.0, 40.0)),  # 1.1 s away: outside
        ([40.8], (10.0, 40.8 - G)),  # the end snaps too
        ([38.9, 41.1], (10.0, 40.0)),
        ([9.5, 10.6], (9.5, 40.0)),  # the nearest cut wins
        ([9.4, 39.2], (9.4, 39.2 - G)),  # start and end each snap
    ],
)
def test_snap_within_tolerance(cuts: list[float], expected: tuple[float, float]) -> None:
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX) == pytest.approx(expected)


def test_end_snap_stops_before_the_cut_frame() -> None:
    _, end = bd.snap_to_cuts(10.0, 40.0, [40.5], [], MIN, MAX)
    assert 40.5 - 1 / 30 < end < 40.5  # never reaches the first frame of the next shot
    assert round(end, 3) < 40.5  # also after _polish rounds it


def test_snap_that_breaks_min_or_max_keeps_the_boundary() -> None:
    assert bd.snap_to_cuts(10.0, 30.0, [10.8], [], MIN, MAX) == (10.0, 30.0)  # 19.2 s < min
    assert bd.snap_to_cuts(10.0, 30.0, [29.5], [], MIN, MAX) == (10.0, 30.0)
    assert bd.snap_to_cuts(10.0, 69.5, [70.2], [], MIN, MAX) == (10.0, 69.5)  # 60.2 s > max
    # The start is kept, the end still snaps on its own.
    assert bd.snap_to_cuts(10.0, 30.0, [10.8, 30.5], [], MIN, MAX) == pytest.approx(
        (10.0, 30.5 - G)
    )


def test_snap_never_lands_mid_word() -> None:
    words = [(9.0, 9.6), (39.9, 40.5)]
    assert bd.snap_to_cuts(10.0, 40.0, [9.4, 40.2], words, MIN, MAX) == (10.0, 40.0)
    # A cut in the gap, or exactly on a word edge, is fine.
    assert bd.snap_to_cuts(10.0, 40.0, [9.6, 40.6], words, MIN, MAX) == pytest.approx(
        (9.6, 40.6 - G)
    )
    # The nearest cut is mid-word, the next one is in a gap.
    assert bd.snap_to_cuts(10.0, 40.0, [9.5, 10.7], words, MIN, MAX) == (10.7, 40.0)


def test_snap_never_pulls_in_or_drops_a_whole_word() -> None:
    # Extending the start to 9.1 would add the previous sentence's last word.
    words = [(9.2, 9.8), (10.0, 10.5), (30.0, 30.5), (40.2, 40.6)]
    assert bd.snap_to_cuts(10.0, 40.0, [9.1, 40.9], words, MIN, MAX) == (10.0, 40.0)
    # Moving inward past a whole word would drop it.
    assert bd.snap_to_cuts(9.9, 40.0, [10.6], words, MIN, MAX) == (9.9, 40.0)
    # A word straddling the old edge is not between: the snap completes it.
    assert bd.snap_to_cuts(10.2, 40.0, [9.9], words, MIN, MAX) == (9.9, 40.0)


def test_snap_stays_inside_the_section() -> None:
    cuts = [9.3, 40.8]
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX, section=(9.5, 40.5)) == (10.0, 40.0)
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX, section=(9.3, 40.8)) == pytest.approx(
        (9.3, 40.8 - G)
    )


def test_trim_dead_air() -> None:
    assert bd.trim_dead_air(10.0, 40.0, [(12.0, 13.0), (20.0, 21.0)]) == (11.75, 21.4)
    # Speech right at the edges (or straddling them): nothing to trim, never extended.
    assert bd.trim_dead_air(10.0, 40.0, [(9.5, 10.1), (39.8, 40.2)]) == (10.0, 40.0)
    assert bd.trim_dead_air(10.0, 40.0, [(10.1, 11.0), (39.7, 39.8)]) == (10.0, 40.0)


def test_trim_never_leaves_a_flash_of_another_shot() -> None:
    words = [(12.0, 13.0), (30.0, 38.0)]
    # Last word ends at 38.0, a cut at 38.2: end before the cut, not at 38.4.
    assert bd.trim_dead_air(10.0, 40.0, words, [38.2]) == pytest.approx((11.75, 38.2 - G))
    # First word at 12.0, a cut at 11.9: start on the cut, not at 11.75.
    assert bd.trim_dead_air(10.0, 40.0, words, [11.9]) == pytest.approx((11.9, 38.4))
    # The first cut after the last word and the last cut before the first word win.
    assert bd.trim_dead_air(10.0, 40.0, words, [10.5, 11.8, 38.1, 39.0]) == pytest.approx(
        (11.8, 38.1 - G)
    )
    # Cuts outside the dead air change nothing.
    assert bd.trim_dead_air(10.0, 40.0, words, [9.0, 11.0, 20.0, 38.9]) == (11.75, 38.4)


@pytest.mark.parametrize("words", [[], [(1.0, 2.0), (40.0, 41.0)], [(5.0, 10.0)]])
def test_trim_without_words_in_range_keeps_the_clip(words: list[tuple[float, float]]) -> None:
    assert bd.trim_dead_air(10.0, 40.0, words, [20.0]) == (10.0, 40.0)


def test_trim_never_goes_below_min() -> None:
    assert bd.trim_dead_air(10.0, 40.0, [(25.0, 26.0)], min_s=20) == (10.0, 40.0)
    assert bd.trim_dead_air(10.0, 40.0, [(20.0, 21.0)], min_s=20) == (19.75, 40.0)  # one edge
    assert bd.trim_dead_air(10.0, 40.0, [(12.0, 35.0)], min_s=20) == (11.75, 35.4)


def test_trim_under_min_tries_one_edge_at_a_time() -> None:
    words = [(12.0, 13.0), (28.0, 29.0)]  # a full trim (11.75, 29.4) is 17.65 s
    assert bd.trim_dead_air(10.0, 40.0, words, min_s=20) == (11.75, 40.0)
    # Cut-driven moves are kept first; the dead-air trim then goes on one edge only.
    assert bd.trim_dead_air(10.0, 40.0, words, [39.0], min_s=20) == pytest.approx((11.75, 39.0 - G))
    assert bd.trim_dead_air(10.0, 31.0, words, [10.5], min_s=20) == (10.5, 31.0)


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        ([20.5], (10.05, 20.5 - G)),  # cut exactly on the last word's end
        ([20.45], (10.05, 20.45 - G)),  # cut just inside the last word
        ([10.4], (10.4, 20.9)),  # cut inside the first word (10.3-10.6)
        ([10.3 + 0.2], (10.05, 20.9)),  # deeper than EPS: the word is kept whole
    ],
)
def test_trim_takes_a_cut_just_inside_the_first_or_last_word(
    cuts: list[float], expected: tuple[float, float]
) -> None:
    words = [(10.3, 10.6), (15.0, 16.0), (19.8, 20.5)]
    assert bd.trim_dead_air(10.0, 30.0, words, cuts) == pytest.approx(expected)


def test_cut_inside_a_short_edge_word_never_drops_it() -> None:
    # Words shorter than WORD_EPS_S: a cut past the edge word must not drop it.
    first_short = [(10.3, 10.38), (10.42, 11.0), (30.0, 39.0)]
    assert bd.polish(10.0, 40.0, [10.40], first_short, MIN, MAX)[0] <= 10.3
    last_short = [(10.0, 11.0), (19.0, 19.58), (19.62, 19.7)]
    assert bd.trim_dead_air(5.0, 21.0, last_short, [19.61])[1] >= 19.7


def test_edge_on_a_cut_is_not_snapped_away() -> None:
    # The trim put the start on the cut at 10.4 (inside the first word); a valid cut at 9.6
    # within the tolerance must not pull the start back over the 0.8 s of the other shot.
    words = [(10.3, 10.6), (35.0, 36.0)]
    assert bd.polish(10.0, 40.0, [9.6, 10.4], words, MIN, MAX) == pytest.approx((10.4, 36.4))


@pytest.mark.parametrize(
    ("cuts", "words", "expected"),
    [
        # Cut 0.7 s before the first word at the start: the snap is kept (no trim undoes it).
        ([9.3], [(10.0, 11.0), (30.0, 39.9)], (9.3, 40.0)),
        # Dead air then a cut near the trimmed start: the start lands on the cut.
        ([14.2], [(15.0, 16.0), (30.0, 39.9)], (14.2, 40.0)),
        # No cut nearby: trim limits only.
        ([], [(15.0, 16.0), (30.0, 35.0)], (14.75, 35.4)),
        # Trimmed end snaps out to a cut in the dead air after the last word.
        ([36.0], [(15.0, 16.0), (30.0, 35.0)], (14.75, 36.0 - G)),
        # Last word 38.0, cut 38.2: ends on the cut, no flash frame.
        ([38.2], [(10.0, 11.0), (30.0, 38.0)], (10.0, 38.2 - G)),
    ],
)
def test_polish_lands_on_a_cut_or_the_trim_limit(
    cuts: list[float], words: list[tuple[float, float]], expected: tuple[float, float]
) -> None:
    assert bd.polish(10.0, 40.0, cuts, words, MIN, MAX) == pytest.approx(expected)


class FakeRunner:
    def __init__(self, result: RunResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []
        self.kw: list[dict[str, Any]] = []

    def __call__(self, args: list[str], timeout_s: float, **kw: Any) -> RunResult:
        self.calls.append((list(args), timeout_s))
        self.kw.append(kw)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def section(tmp_path: Path) -> Path:
    path = tmp_path / "clips" / "1" / "source.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mp4")
    return path


def test_detect_cuts_runs_bounded_subprocess_and_caches(section: Path) -> None:
    runner = FakeRunner(RunResult(0, "[2.0, 5.5]\n", ""))

    assert bd.detect_cuts(section, Deadline(), runner=runner) == [2.0, 5.5]

    ((args, timeout_s),) = runner.calls
    assert args == [sys.executable, "-I", "-m", "clipper.pipeline.boundaries", str(section)]
    assert runner.kw == [{"env": {"MALLOC_ARENA_MAX": "2"}}]  # glibc reads it at startup
    assert 0 < timeout_s <= bd.DETECT_TIMEOUT_S
    cache = section.with_name("source.mp4.cuts.json")
    stat = section.stat()
    assert json.loads(cache.read_text()) == {
        "v": 1,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "cuts": [2.0, 5.5],
    }
    # A rerun reads the cache and never runs detection again.
    again = FakeRunner(CommandTimeout("must not run"))
    assert bd.detect_cuts(section, Deadline(), runner=again) == [2.0, 5.5]
    assert again.calls == []


def test_cache_of_a_replaced_section_is_ignored(section: Path) -> None:
    bd.detect_cuts(section, Deadline(), runner=FakeRunner(RunResult(0, "[2.0]", "")))
    section.write_bytes(b"another download")  # e.g. fetch_clips fetched a new section

    runner = FakeRunner(RunResult(0, "[7.0]", ""))
    assert bd.detect_cuts(section, Deadline(), runner=runner) == [7.0]
    assert len(runner.calls) == 1


def _cache(section: Path, **change: Any) -> str:
    stat = section.stat()
    entry = {"v": 1, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "cuts": [2.0]}
    return json.dumps({**entry, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"v": 2},
        {"size": 1},
        {"mtime_ns": 1},
        {"cuts": [-1.0]},
        {"cuts": ["2"]},
        {"cuts": [float("nan")]},
        {"cuts": [True]},
        {"cuts": {}},
    ],
)
def test_bad_cache_is_ignored(section: Path, change: dict[str, Any]) -> None:
    section.with_name("source.mp4.cuts.json").write_text(_cache(section, **change))
    runner = FakeRunner(RunResult(0, "[3.0]", ""))
    assert bd.detect_cuts(section, Deadline(), runner=runner) == [3.0]
    assert len(runner.calls) == 1


@pytest.mark.parametrize("cached", ["[2.0]", "not json", "{}", "[" * 100_000 + "]" * 100_000])
def test_unreadable_or_old_cache_is_ignored(section: Path, cached: str) -> None:
    section.with_name("source.mp4.cuts.json").write_text(cached)
    runner = FakeRunner(RunResult(0, "[3.0]", ""))
    assert bd.detect_cuts(section, Deadline(), runner=runner) == [3.0]


@pytest.mark.parametrize(
    "result",
    [
        CommandTimeout("python timed out after 120s"),
        RunResult(1, "", "Traceback ...\ncv2.error: cannot open token=sk-ant-api03-" + "x" * 40),
        RunResult(0, "garbage", ""),
        RunResult(0, '{"cuts": 1}', ""),
        OSError("no such interpreter"),
    ],
)
def test_detection_failure_returns_no_cuts_and_warns(
    section: Path, result: RunResult | BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    assert bd.detect_cuts(section, Deadline(), runner=FakeRunner(result)) == []
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "scene detection" in warning.getMessage()
    assert "sk-ant-api03" not in warning.getMessage()
    assert not section.with_name("source.mp4.cuts.json").exists()  # a failure is not cached


def test_oversized_cache_is_ignored(section: Path) -> None:
    padded = _cache(section) + " " * bd.MAX_CACHE_BYTES  # valid JSON, too big to read
    section.with_name("source.mp4.cuts.json").write_text(padded)
    runner = FakeRunner(RunResult(0, "[3.0]", ""))
    assert bd.detect_cuts(section, Deadline(), runner=runner) == [3.0]


def test_section_changed_during_detection_is_not_cached(section: Path) -> None:
    class Replacing(FakeRunner):
        def __call__(self, args: list[str], timeout_s: float, **kw: Any) -> RunResult:
            section.write_bytes(b"replaced while detecting")
            return super().__call__(args, timeout_s, **kw)

    assert bd.detect_cuts(section, Deadline(), runner=Replacing(RunResult(0, "[2.0]", ""))) == [2.0]
    assert not section.with_name("source.mp4.cuts.json").exists()


def test_failure_details_go_to_debug(section: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger=bd.__name__)
    bd.detect_cuts(section, Deadline(), runner=FakeRunner(OSError("boom")))
    (debug,) = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert debug.exc_info is not None


def test_child_hardening(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: limits.append((kind, value)))
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)

    bd.harden()

    assert limits == [(resource.RLIMIT_AS, (bd.DECODER_MEMORY_BYTES, bd.DECODER_MEMORY_BYTES))]
    # OpenCV parses these as `key;value|key;value` (av_dict_parse_string with ";" and "|").
    assert bd.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "protocol_whitelist;file|threads;2"
    assert bd.os.environ["OPENBLAS_NUM_THREADS"] == "1"


def test_child_hardening_tolerates_no_rlimit(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    def refuse(kind: int, value: tuple[int, int]) -> None:
        raise ValueError("not allowed")  # macOS refuses RLIMIT_AS

    monkeypatch.setattr(resource, "setrlimit", refuse)
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "x")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "8")
    bd.harden()  # does not raise
    assert bd.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "protocol_whitelist;file|threads;2"


def test_exhausted_deadline_returns_no_cuts(
    section: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner(RunResult(0, "[1.0]", ""))
    assert bd.detect_cuts(section, Deadline(budget_s=0.5), runner=runner) == []
    assert runner.calls == []
    assert any("scene detection" in r.getMessage() for r in caplog.records)


@pytest.mark.slow
def test_detects_a_hard_cut(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "source.mp4"
    color = "color=c={}:s=320x180:r=25:d=2"
    subprocess.run(  # noqa: S603
        [
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", color.format("red"),
            "-f", "lavfi", "-i", color.format("blue"),
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path),
        ],
        check=True,
    )  # fmt: skip

    (cut,) = bd.scan(path)  # in process
    assert cut == pytest.approx(2.0, abs=0.1)
    (cut,) = bd.detect_cuts(path, Deadline())  # through the bounded subprocess
    assert cut == pytest.approx(2.0, abs=0.1)


def test_cache_read_is_bounded_even_if_the_file_grows(tmp_path: Path) -> None:
    # The size cap is enforced on the bytes read, not on an earlier stat().
    section = tmp_path / "source.mp4"
    section.write_bytes(b"x")
    cache = tmp_path / "source.mp4.cuts.json"
    cache.write_text(" " * (bd.MAX_CACHE_BYTES + 10) + "{}")
    assert bd._read_cache(cache) is None


def test_extra_child_env_cannot_override_the_allowlist() -> None:
    from clipper.pipeline import ytdlp

    with pytest.raises(ValueError):
        ytdlp.run_command(["true"], 5.0, env={"PATH": "/evil"})
    with pytest.raises(ValueError):
        ytdlp.run_command(["true"], 5.0, env={"LD_PRELOAD": "/x.so"})
    with pytest.raises(TypeError):
        bd.CHILD_ENV["X"] = "1"  # type: ignore[index]


def test_cut_list_is_sorted_unique_and_capped() -> None:
    assert bd._cut_list([3.0, 1.0, 3.0, 2]) == [1.0, 2.0, 3.0]
    many = bd._cut_list([float(i) for i in range(bd.MAX_CUTS + 100, 0, -1)])
    assert len(many) == bd.MAX_CUTS and many[0] == 1.0
