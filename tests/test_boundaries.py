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


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        ([9.3], (9.3, 40.0)),  # cut 0.7 s before the start: inside the tolerance
        ([10.9], (10.9, 40.0)),
        ([8.9], (10.0, 40.0)),  # 1.1 s away: outside
        ([40.8], (10.0, 40.8)),  # the end snaps too
        ([38.9, 41.1], (10.0, 40.0)),
        ([9.5, 10.6], (9.5, 40.0)),  # the nearest cut wins
        ([9.4, 39.2], (9.4, 39.2)),  # start and end each snap
    ],
)
def test_snap_within_tolerance(cuts: list[float], expected: tuple[float, float]) -> None:
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX) == expected


def test_snap_that_breaks_min_or_max_keeps_the_boundary() -> None:
    assert bd.snap_to_cuts(10.0, 30.0, [10.8], [], MIN, MAX) == (10.0, 30.0)  # 19.2 s < min
    assert bd.snap_to_cuts(10.0, 30.0, [29.5], [], MIN, MAX) == (10.0, 30.0)
    assert bd.snap_to_cuts(10.0, 69.5, [70.2], [], MIN, MAX) == (10.0, 69.5)  # 60.2 s > max
    # The start is kept, the end still snaps on its own.
    assert bd.snap_to_cuts(10.0, 30.0, [10.8, 30.5], [], MIN, MAX) == (10.0, 30.5)


def test_snap_never_lands_mid_word() -> None:
    words = [(9.0, 9.6), (39.9, 40.5)]
    assert bd.snap_to_cuts(10.0, 40.0, [9.4, 40.2], words, MIN, MAX) == (10.0, 40.0)
    # A cut in the gap, or exactly on a word edge, is fine.
    assert bd.snap_to_cuts(10.0, 40.0, [9.6, 40.5], words, MIN, MAX) == (9.6, 40.5)
    # The nearest cut is mid-word, the next one is in a gap.
    assert bd.snap_to_cuts(10.0, 40.0, [9.5, 10.7], words, MIN, MAX) == (10.7, 40.0)


def test_snap_stays_inside_the_section() -> None:
    cuts = [9.3, 40.8]
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX, section=(9.5, 40.5)) == (10.0, 40.0)
    assert bd.snap_to_cuts(10.0, 40.0, cuts, [], MIN, MAX, section=(9.3, 40.8)) == (9.3, 40.8)


def test_trim_dead_air() -> None:
    assert bd.trim_dead_air(10.0, 40.0, [(12.0, 13.0), (20.0, 21.0)]) == (11.75, 21.4)
    # Speech right at the edges (or straddling them): nothing to trim, never extended.
    assert bd.trim_dead_air(10.0, 40.0, [(9.5, 10.1), (39.8, 40.2)]) == (10.0, 40.0)
    assert bd.trim_dead_air(10.0, 40.0, [(10.1, 11.0), (39.7, 39.8)]) == (10.0, 40.0)


@pytest.mark.parametrize("words", [[], [(1.0, 2.0), (40.0, 41.0)], [(5.0, 10.0)]])
def test_trim_without_words_in_range_keeps_the_clip(words: list[tuple[float, float]]) -> None:
    assert bd.trim_dead_air(10.0, 40.0, words) == (10.0, 40.0)


def test_trim_never_goes_below_min() -> None:
    assert bd.trim_dead_air(10.0, 40.0, [(20.0, 21.0)], min_s=20) == (10.0, 40.0)
    assert bd.trim_dead_air(10.0, 40.0, [(12.0, 35.0)], min_s=20) == (11.75, 35.4)


class FakeRunner:
    def __init__(self, result: RunResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args: list[str], timeout_s: float, **kw: Any) -> RunResult:
        self.calls.append((list(args), timeout_s))
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
    assert 0 < timeout_s <= bd.DETECT_TIMEOUT_S
    cache = section.with_name("source.mp4.cuts.json")
    assert json.loads(cache.read_text()) == [2.0, 5.5]
    # A rerun reads the cache and never runs detection again.
    again = FakeRunner(CommandTimeout("must not run"))
    assert bd.detect_cuts(section, Deadline(), runner=again) == [2.0, 5.5]
    assert again.calls == []


@pytest.mark.parametrize("cached", ["{}", "[-1.0]", '["2"]', "[NaN]", "not json"])
def test_bad_cache_is_ignored(section: Path, cached: str) -> None:
    section.with_name("source.mp4.cuts.json").write_text(cached)
    runner = FakeRunner(RunResult(0, "[3.0]", ""))
    assert bd.detect_cuts(section, Deadline(), runner=runner) == [3.0]
    assert len(runner.calls) == 1


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
