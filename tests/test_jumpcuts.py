"""Jump cuts inside a clip (CLP-73): pure cut rules, the frame-grid apply, the crop
remap and the silencedetect child."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clipper.edl import FPS, Cut, Word
from clipper.pipeline import jumpcuts as jc
from clipper.pipeline.common import Deadline

FRAME = 1 / FPS


def w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start_s=start, end_s=end)


def cut(a: float, b: float, *, applied: bool = True, kind: str = "silence") -> Cut:
    return Cut(in_s=a, out_s=b, kind=kind, confidence=1.0, reason="test", applied=applied)


EVERYWHERE = [(0.0, 1000.0)]
"""silencedetect output that calls the whole section silent."""


# --- silence ---------------------------------------------------------------------------


def test_long_gap_with_silence_is_cut_down_to_a_breath() -> None:
    words = [w("hello", 0.0, 0.5), w("world", 1.7, 2.1)]  # a 1.2 s gap
    (c,) = jc.silence_cuts(words, [(0.5, 1.7)])
    assert (c.kind, c.applied, c.confidence) == ("silence", True, 1.0)
    # The 0.14 s breath is kept, split 55/45 around the gap.
    assert c.in_s == pytest.approx(0.5 + 0.14 * 0.55)
    assert c.out_s == pytest.approx(1.7 - 0.14 * 0.45)
    assert 1.2 - (c.out_s - c.in_s) == pytest.approx(jc.BREATH_S)
    assert "1.20" in c.reason


def test_gap_without_silencedetect_silence_is_kept() -> None:
    """Game audio or laughter: the transcript has a gap, ffmpeg hears sound."""
    words = [w("hello", 0.0, 0.5), w("world", 1.7, 2.1)]
    assert jc.silence_cuts(words, []) == []
    # Silence over only part of the gap is not enough either.
    assert jc.silence_cuts(words, [(0.5, 1.0), (1.3, 1.7)]) == []


def test_short_gap_is_never_cut() -> None:
    words = [w("a", 0.0, 0.5), w("b", 1.04, 1.5)]  # 0.54 s
    assert jc.silence_cuts(words, EVERYWHERE) == []


@pytest.mark.parametrize(
    ("first", "gap", "keep"),
    [("so", 1.0, 0.14), ("done.", 1.0, 0.20), ("wait?", 1.0, 0.20), ("so", 2.5, 0.24)],
)
def test_breath_is_longer_after_a_sentence_and_a_long_pause(
    first: str, gap: float, keep: float
) -> None:
    words = [w(first, 0.0, 0.5), w("next", 0.5 + gap, 1.0 + gap)]
    (c,) = jc.silence_cuts(words, EVERYWHERE)
    assert gap - (c.out_s - c.in_s) == pytest.approx(keep)
    assert c.in_s - 0.5 >= jc.WORD_END_PAD_S


# --- stutter ---------------------------------------------------------------------------


def test_stutter_cuts_the_first_occurrence() -> None:
    words = [w("I", 0.0, 0.2), w("i,", 0.4, 0.6), w("think", 0.6, 1.0)]
    (c,) = jc.stutter_cuts(words)
    assert (c.in_s, c.out_s, c.kind, c.applied) == (0.0, 0.4, "stutter", True)
    assert 0 < c.confidence <= 1


def test_repeat_further_apart_is_not_a_stutter() -> None:
    assert jc.stutter_cuts([w("go", 0.0, 0.2), w("go", 0.8, 1.0)]) == []


def test_tagalog_reduplication_is_not_a_stutter() -> None:
    assert jc.stutter_cuts([w("ano", 0.0, 0.2), w("ano", 0.3, 0.5)]) == []


# --- fillers ---------------------------------------------------------------------------


def test_filler_between_silences_is_cut() -> None:
    words = [w("so", 0.0, 0.4), w("um,", 0.8, 1.0), w("yeah", 1.5, 1.9)]
    (c,) = jc.filler_cuts(words, [(0.4, 0.8), (1.0, 1.5)])
    assert (c.kind, c.applied) == ("filler", True)
    assert c.in_s < 0.8 and c.out_s > 1.0  # the filler and the silence around it
    assert c.in_s >= 0.4 + jc.WORD_END_PAD_S and c.out_s < 1.5


def test_filler_without_silence_on_both_sides_is_kept() -> None:
    words = [w("so", 0.0, 0.4), w("uh", 0.8, 1.0), w("yeah", 1.5, 1.9)]
    assert jc.filler_cuts(words, [(0.4, 0.8)]) == []
    assert jc.filler_cuts(words, [(1.0, 1.5)]) == []


def test_filler_in_connected_speech_is_kept() -> None:
    """A particle like Tagalog "eh" without a real pause on each side is speech."""
    words = [w("grabe", 0.0, 0.4), w("eh", 0.5, 0.7), w("talaga", 0.8, 1.2)]
    assert jc.filler_cuts(words, EVERYWHERE) == []


@pytest.mark.parametrize("token", ["ano", "yung", "parang", "kasi", "like", "so"])
def test_only_um_uh_eh_ah_are_fillers(token: str) -> None:
    words = [w("so", 0.0, 0.4), w(token, 0.8, 1.0), w("yeah", 1.5, 1.9)]
    assert jc.filler_cuts(words, EVERYWHERE) == []


@pytest.mark.parametrize("token", ["um", "Uh", "eh...", "ah", "umm"])
def test_filler_tokens(token: str) -> None:
    words = [w("so", 0.0, 0.4), w(token, 0.8, 1.0), w("yeah", 1.5, 1.9)]
    assert len(jc.filler_cuts(words, EVERYWHERE)) == 1


# --- retakes ---------------------------------------------------------------------------


def _phrase(text: str, start: float) -> list[Word]:
    return [w(t, start + i * 0.3, start + i * 0.3 + 0.25) for i, t in enumerate(text.split())]


def test_retake_is_proposed_only() -> None:
    first = _phrase("the boss fight in this game was honestly brutal", 0.0)
    second = _phrase("the boss fight in this game was honestly insane", first[-1].end_s + 1.0)
    (c,) = jc.retake_candidates(first + second)
    assert c.kind in {"retake", "false_start"}
    assert c.applied is False
    assert (c.in_s, c.out_s) == (first[0].start_s, pytest.approx(second[0].start_s))
    assert c.confidence >= jc.RETAKE_JACCARD


def test_different_phrases_are_not_retakes() -> None:
    first = _phrase("we went to the market yesterday morning", 0.0)
    second = _phrase("then the stream crashed during the raid", first[-1].end_s + 1.0)
    assert jc.retake_candidates(first + second) == []


def test_tagalog_stop_words_do_not_make_a_retake() -> None:
    first = _phrase("ano yung parang kasi naman talaga ganun", 0.0)
    second = _phrase("ano yung parang kasi naman talaga diba", first[-1].end_s + 1.0)
    assert jc.retake_candidates(first + second) == []


# --- plan by mode ----------------------------------------------------------------------


def _mixed() -> list[Word]:
    words = [w("I", 0.0, 0.2), w("I", 0.35, 0.5), w("know", 0.5, 0.8)]
    words += [w("um", 1.6, 1.8), w("right", 2.6, 3.0)]  # filler between two long gaps
    return words


def test_mode_off_plans_nothing() -> None:
    assert jc.plan_cuts(_mixed(), EVERYWHERE, "off") == []


def test_mode_silences_applies_only_silence() -> None:
    planned = jc.plan_cuts(_mixed(), EVERYWHERE, "silences")
    assert {c.kind for c in planned if c.applied} == {"silence"}


def test_mode_full_adds_stutters_and_fillers() -> None:
    planned = jc.plan_cuts(_mixed(), EVERYWHERE, "full")
    assert {c.kind for c in planned if c.applied} == {"silence", "stutter", "filler"}


@pytest.mark.parametrize("mode", ["silences", "full"])
def test_retakes_are_never_applied(mode: str) -> None:
    first = _phrase("the boss fight in this game was honestly brutal", 0.0)
    second = _phrase("the boss fight in this game was honestly insane", first[-1].end_s + 1.0)
    planned = jc.plan_cuts(first + second, [], mode)
    retakes = [c for c in planned if c.kind in {"retake", "false_start"}]
    assert retakes and not any(c.applied for c in retakes)


def test_taglish_keeps_every_word() -> None:
    """AC: "ano", "yung" and "parang" are real words, never cut (even repeated)."""
    text = "ano yung ano ano parang game kasi parang ang hirap nung boss eh grabe"
    words = [w(t, i * 0.4, i * 0.4 + 0.3) for i, t in enumerate(text.split())]
    planned = jc.plan_cuts(words, EVERYWHERE, "full")
    segments = jc.apply_cuts((0.0, words[-1].end_s), planned)
    for word in words:
        mid = (word.start_s + word.end_s) / 2
        assert any(a <= mid < b for a, b in segments), word.text
    assert not [c for c in planned if c.applied]


# --- apply -----------------------------------------------------------------------------


def _on_grid(x: float) -> bool:
    return abs(x * FPS - round(x * FPS)) < 1e-6


def test_apply_without_cuts_keeps_the_range() -> None:
    assert jc.apply_cuts((1.0, 6.0), []) == [(1.0, 6.0)]


def test_apply_splits_on_the_frame_grid() -> None:
    segments = jc.apply_cuts((0.0, 10.0), [cut(3.01, 4.52)])
    assert len(segments) == 2
    assert all(_on_grid(x) for seg in segments for x in seg)
    # Remotion's rounding: floor(x * fps + 0.5).
    assert segments == [(0.0, 90 / FPS), (136 / FPS, 10.0)]


def test_apply_ignores_proposals_and_clips_to_the_range() -> None:
    segments = jc.apply_cuts((2.0, 8.0), [cut(3.0, 4.0, applied=False), cut(7.0, 9.0)])
    assert segments == [(2.0, 7.0)]


def test_apply_merges_overlapping_cuts() -> None:
    segments = jc.apply_cuts((0.0, 10.0), [cut(2.0, 4.0), cut(3.0, 5.0)])
    assert segments == [(0.0, 2.0), (5.0, 10.0)]


def test_short_segment_merges_back_across_the_shorter_cut() -> None:
    # (5.0, 5.2) would be a 0.2 s segment: the 0.4 s cut after it is restored, not the 3 s one.
    segments = jc.apply_cuts((0.0, 10.0), [cut(2.0, 5.0), cut(5.2, 5.6)])
    assert segments == [(0.0, 2.0), (5.0, 10.0)]
    assert all(b - a >= jc.MIN_SEGMENT_S - 1e-9 for a, b in segments)


def test_short_first_segment_merges_forward() -> None:
    segments = jc.apply_cuts((0.0, 10.0), [cut(0.1, 3.0)])
    assert segments == [(0.0, 10.0)]


def test_cut_over_the_whole_range_keeps_it() -> None:
    assert jc.apply_cuts((1.0, 4.0), [cut(0.0, 5.0)]) == [(1.0, 4.0)]


# --- crop remap ------------------------------------------------------------------------


def test_crop_keys_move_to_the_output_timeline_with_a_jump_at_each_join() -> None:
    keys = [(0.0, 0.2), (10.0, 0.8)]  # linear over the span
    out = jc.crop_on_output(keys, [(1.0, 3.0), (6.0, 8.0)], origin=1.0)
    assert out == [
        (0.0, pytest.approx(0.2)),
        (2.0, pytest.approx(0.32)),
        (2.0, pytest.approx(0.5)),  # the join: two keys at one time, a jump
        (4.0, pytest.approx(0.62)),
    ]


def test_crop_keys_inside_a_segment_keep_their_scene_jumps() -> None:
    keys = [(0.0, 0.3), (1.0, 0.3), (1.0, 0.7), (4.0, 0.7)]  # a scene cut at 1.0
    out = jc.crop_on_output(keys, [(0.0, 2.0), (3.0, 4.0)], origin=0.0)
    assert out == [(0.0, 0.3), (1.0, 0.3), (1.0, 0.7), (2.0, 0.7), (2.0, 0.7), (3.0, 0.7)]


def test_crop_end_key_takes_the_value_before_a_jump_at_the_join() -> None:
    keys = [(0.0, 0.3), (2.0, 0.3), (2.0, 0.7), (4.0, 0.7)]
    out = jc.crop_on_output(keys, [(0.0, 2.0), (2.5, 4.0)], origin=0.0)
    assert out[1] == (2.0, 0.3) and out[2] == (2.0, 0.7)


# --- silencedetect ---------------------------------------------------------------------

OUTPUT = """\
frame:23   pts:24000   pts_time:0.5
lavfi.silence_start=0.5
frame:81   pts:81600   pts_time:1.7
lavfi.silence_end=1.7
lavfi.silence_duration=1.2
frame:90   pts:0   pts_time:0
lavfi.silence_start=-1e-05
lavfi.silence_end=3.25
lavfi.silence_start=9.5
"""
"""silencedetect's frame metadata as `ametadata=mode=print:file=-` writes it to stdout."""


def test_parse_silencedetect() -> None:
    # A start without an end runs to the end of the file.
    assert jc.parse_silencedetect(OUTPUT) == [[0.5, 1.7], [0.0, 3.25], [9.5, jc.FILE_END_S]]


class FakeRunner:
    def __init__(self, stdout: str = OUTPUT, code: int = 0) -> None:
        self.stdout, self.code = stdout, code
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout_s: float, **kw: Any) -> Any:
        self.calls.append(list(args))
        assert 0 < timeout_s <= jc.SILENCE_TIMEOUT_S
        return subprocess.CompletedProcess(args, self.code, self.stdout, "")


def test_detect_silences_runs_a_guarded_ffmpeg_and_caches(tmp_path: Path) -> None:
    section = tmp_path / "source.mp4"
    section.write_bytes(b"mp4")
    runner = FakeRunner()
    got = jc.detect_silences(section, Deadline(), runner=runner)
    assert got == [(0.5, 1.7), (0.0, 3.25), (9.5, jc.FILE_END_S)]
    (argv,) = runner.calls
    assert argv[0] == "ffmpeg"
    i = argv.index("-i")
    assert argv[i - 4 : i] == ["-protocol_whitelist", "file", "-format_whitelist", argv[i - 1]]
    assert argv[i + 1] == str(section)
    assert "-vn" in argv
    assert argv[argv.index("-af") + 1].startswith("silencedetect=noise=-40dB:d=0.2,")
    # Cached by size and mtime: no second run.
    assert jc.detect_silences(section, Deadline(), runner=runner) == got
    assert len(runner.calls) == 1


def test_detect_silences_fails_open(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    section = tmp_path / "source.mp4"
    section.write_bytes(b"mp4")
    caplog.set_level(logging.WARNING)
    assert jc.detect_silences(section, Deadline(), runner=FakeRunner(code=1)) is None
    assert "silence detection failed" in caplog.text
    assert not (tmp_path / "source.mp4.silences.json").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_detect_silences_with_real_ffmpeg(tmp_path: Path) -> None:
    section = tmp_path / "source.mp4"
    tone = "sine=frequency=440:sample_rate=48000:duration=1"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", tone,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1.2",
            "-f", "lavfi", "-i", tone,
            "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", "-c:a", "aac", str(section),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    got = jc.detect_silences(section, Deadline())
    assert got is not None
    assert any(s <= 1.1 and e >= 2.1 for s, e in got)


@pytest.mark.parametrize(
    "bad",
    ["nope", [[1.0]], [[2.0, 1.0]], [[-1.0, 1.0]], [[True, 1.0]], [["0", "1"]], [[0, 1]] * 20_001],
)
def test_tampered_silence_cache_is_ignored(tmp_path: Path, bad: Any) -> None:
    section = tmp_path / "source.mp4"
    section.write_bytes(b"mp4")
    stat = section.stat()
    cache = {"v": 1, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "silences": bad}
    (tmp_path / "source.mp4.silences.json").write_text(json.dumps(cache))
    runner = FakeRunner()
    assert jc.detect_silences(section, Deadline(), runner=runner) == [
        (0.5, 1.7),
        (0.0, 3.25),
        (9.5, jc.FILE_END_S),
    ]
    assert len(runner.calls) == 1


def test_silence_end_without_a_start_is_ignored() -> None:
    assert jc.parse_silencedetect("lavfi.silence_end=1.0\nlavfi.silence_start=2e0\n") == [
        [2.0, jc.FILE_END_S]
    ]


def test_cut_outside_the_range_is_ignored() -> None:
    assert jc.apply_cuts((2.0, 8.0), [cut(0.0, 1.0), cut(9.0, 9.5)]) == [(2.0, 8.0)]


def test_retakes_are_only_looked_for_nearby() -> None:
    first = _phrase("the boss fight in this game was honestly brutal", 0.0)
    second = _phrase("the boss fight in this game was honestly insane", 100.0)
    assert jc.retake_candidates(first + second) == []
