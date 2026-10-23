"""Quality gates on a rendered clip (CLP-74): pure EDL checks, loudness, the ASR join check."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clipper.edl import EDL, parse_edl
from clipper.pipeline import quality as q
from clipper.pipeline.common import Deadline, MediaError
from clipper.pipeline.transcribe import ChunkTranscript, Word


def _edl(
    words: list[tuple[str, float, float]],
    segments: list[tuple[float, float]] | None = None,
    source: tuple[float, float] = (10.0, 20.0),
) -> EDL:
    data: dict[str, Any] = {
        "source": {"path": "clips/1/source.mp4", "in_s": source[0], "out_s": source[1]},
        "words": [{"text": t, "start_s": a, "end_s": b} for t, a, b in words],
    }
    if segments is not None:
        data["segments"] = [{"in_s": a, "out_s": b} for a, b in segments]
    return parse_edl(data)


TWO_SEGMENTS = [(10.0, 14.0), (15.0, 20.0)]
"""One join at output 4.0 s; source 14..15 is cut."""


def test_clean_clip_has_no_edl_reasons() -> None:
    edl = _edl([("hi", 10.1, 10.4), ("there", 13.2, 13.8), ("again", 15.2, 15.6)], TWO_SEGMENTS)
    assert q.edl_reasons(edl) == []


def test_output_words_mirror_the_renderer() -> None:
    """Same mapping as render/src/timeline.ts outputWords: a word inside a cut is dropped."""
    edl = _edl([("a", 10.5, 11.0), ("cut", 14.2, 14.6), ("b", 15.5, 16.0)], TWO_SEGMENTS)
    assert q.output_words(edl) == [
        ("a", pytest.approx(0.5), pytest.approx(1.0)),
        ("b", pytest.approx(4.5), pytest.approx(5.0)),
    ]


def test_word_split_across_a_join_fails() -> None:
    edl = _edl([("hi", 10.1, 10.4), ("split", 13.7, 14.4), ("x", 15.2, 15.5)], TWO_SEGMENTS)
    assert q.edl_reasons(edl) == ["word_crosses_join"]


def test_word_starting_inside_the_cut_crosses_the_join() -> None:
    edl = _edl([("hi", 10.1, 10.4), ("late", 14.8, 15.3)], TWO_SEGMENTS)
    assert q.edl_reasons(edl) == ["word_crosses_join"]


def test_words_at_the_clip_edges_are_not_joins() -> None:
    """The renderer clamps words at the clip start and end; only joins inside count."""
    edl = _edl([("early", 9.8, 10.3), ("late", 19.7, 20.4)], TWO_SEGMENTS)
    assert q.edl_reasons(edl) == []


def test_a_v1_clip_has_no_joins() -> None:
    edl = _edl([("hi", 10.1, 10.4), ("long", 13.7, 14.4)])
    assert q.edl_reasons(edl) == []


def test_speech_must_start_within_a_quarter_second() -> None:
    assert q.edl_reasons(_edl([("hi", 10.25, 10.5)])) == []
    assert q.edl_reasons(_edl([("hi", 10.4, 10.6)])) == ["late_speech"]


def test_no_words_is_not_late_speech() -> None:
    assert q.edl_reasons(_edl([])) == []


def test_word_lost_to_frame_rounding_is_a_caption_mismatch() -> None:
    """A word overlapping the kept range by less than the frame rounding never shows."""
    # 9.98 s plays as 299 frames (9.967 s): a word at 19.97 s is in the range, not the video.
    edl = _edl([("hi", 10.1, 10.4), ("edge", 19.97, 19.975)], source=(10.0, 19.98))
    assert q.edl_reasons(edl) == ["caption_mismatch"]
    assert (
        q.edl_reasons(_edl([("hi", 10.1, 10.4), ("in", 19.9, 19.95)], source=(10.0, 19.98))) == []
    )


@pytest.mark.parametrize(
    ("integrated", "peak", "ok"),
    [
        (-14.0, -1.5, True),
        (-13.0, -1.0, True),
        (-15.0, -2.0, True),
        (-20.0, -6.0, False),
        (-12.5, -1.5, False),
        (-14.0, -0.5, False),
        (float("-inf"), float("-inf"), False),
        (float("nan"), -1.5, False),
    ],
)
def test_loudness_window(integrated: float, peak: float, ok: bool) -> None:
    assert q.loudness_ok(integrated, peak) is ok


EBUR128_TAIL = """
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -20.3 LUFS
    Threshold: -30.4 LUFS

  Loudness range:
    LRA:         5.1 LU

  True peak:
    Peak:       -6.2 dBFS
"""


def test_parse_ebur128_summary() -> None:
    assert q.parse_ebur128(EBUR128_TAIL) == (-20.3, -6.2)


def test_parse_ebur128_silence() -> None:
    tail = EBUR128_TAIL.replace("-20.3", "-70.0").replace("-6.2", "-inf")
    assert q.parse_ebur128(tail) == (-70.0, float("-inf"))


@pytest.mark.parametrize("text", ["", "no summary here", EBUR128_TAIL.replace("Peak:", "Pk:")])
def test_unparsable_ebur128_raises(text: str) -> None:
    with pytest.raises(MediaError):
        q.parse_ebur128(text)


def test_measure_loudness_runs_hardened_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args: list[str], timeout_s: float, what: str, **kw: Any) -> Any:
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", EBUR128_TAIL)

    monkeypatch.setattr(q, "run_media", fake_run)
    assert q.measure_loudness(Path("/m/v1.mp4"), 30.0) == (-20.3, -6.2)
    args = seen[0]
    assert args[0] == "ffmpeg" and "-protocol_whitelist" in args
    assert "ebur128=peak=true" in args and args[-3:] == ["-f", "null", "-"]


def test_join_check_compares_heard_words_around_each_join() -> None:
    edl = _edl(
        [("we", 12.6, 12.9), ("are", 13.0, 13.3), ("so", 13.4, 13.7), ("back", 15.1, 15.4),
         ("now", 15.5, 15.8)],
        TWO_SEGMENTS,
    )  # fmt: skip
    assert q.join_times(edl) == [pytest.approx(4.0)]
    heard_ok = [("We", 3.0), ("are", 3.1), ("so", 3.5), ("back!", 4.2), ("now", 4.6)]
    assert q.join_mismatches(edl, heard_ok) == []
    heard_bad = [("we", 3.0), ("arso", 3.4), ("bah", 4.2)]
    assert q.join_mismatches(edl, heard_bad) == [pytest.approx(4.0)]


def test_join_with_too_few_words_is_not_judged() -> None:
    edl = _edl([("hi", 10.1, 10.4), ("yo", 15.2, 15.4)], TWO_SEGMENTS)
    assert q.join_mismatches(edl, []) == []


class FakeAsr:
    def __init__(self, words: list[tuple[str, float, float]] | None = None) -> None:
        self.words = words or []
        self.calls: list[Any] = []
        self.fail: BaseException | None = None

    def ensure_model(self, timeout_s: float) -> Path:
        self.calls.append(("ensure", timeout_s))
        if self.fail:
            raise self.fail
        return Path("snap")

    def transcribe(self, audio: Path, *, language: str | None, timeout_s: float) -> ChunkTranscript:
        assert audio.is_file()
        self.calls.append(("transcribe", audio.suffix, language))
        return ChunkTranscript(language, tuple(Word(*w) for w in self.words), ())


@pytest.fixture
def fake_media(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ffmpeg: ebur128 reports `state["loudness"]`; the audio cut writes its file."""
    state: dict[str, Any] = {"loudness": (-14.0, -1.6)}

    def fake_run(args: list[str], timeout_s: float, what: str, **kw: Any) -> Any:
        if "ebur128=peak=true" in args:
            i, tp = state["loudness"]
            tail = EBUR128_TAIL.replace("-20.3", f"{i:.1f}").replace("-6.2", f"{tp:.1f}")
            return subprocess.CompletedProcess(args, 0, "", tail)
        Path(args[-1]).write_bytes(b"flac")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(q, "run_media", fake_run)
    return state


JOIN_WORDS = [("hi", 10.1, 10.4), ("so", 13.4, 13.7), ("we", 13.75, 13.9), ("back", 15.1, 15.4)]
HEARD = [("so", 3.4, 3.7), ("we", 3.75, 3.9), ("back", 4.1, 4.4)]


def _check(edl: EDL, video: Path, asr: FakeAsr) -> list[str]:
    return q.check_clip(edl, video, language="tl", asr=asr, deadline=Deadline())


def test_passing_clip_has_no_reasons(tmp_path: Path, fake_media: dict[str, Any]) -> None:
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"mp4")
    asr = FakeAsr(HEARD)
    assert _check(_edl(JOIN_WORDS, TWO_SEGMENTS), video, asr) == []
    assert ("transcribe", ".flac", "tl") in asr.calls
    assert list(tmp_path.iterdir()) == [video]  # the audio cut is cleaned up


def test_misheard_join_fails(tmp_path: Path, fake_media: dict[str, Any]) -> None:
    asr = FakeAsr([("sowe", 3.5, 3.9), ("bah", 4.1, 4.4)])
    assert _check(_edl(JOIN_WORDS, TWO_SEGMENTS), tmp_path / "v1.mp4", asr) == ["join_asr"]


def test_quiet_clip_fails_loudness(tmp_path: Path, fake_media: dict[str, Any]) -> None:
    fake_media["loudness"] = (-20.0, -6.0)
    assert _check(_edl([("hi", 10.1, 10.4)]), tmp_path / "v1.mp4", FakeAsr()) == ["loudness"]


def test_clip_without_joins_skips_the_asr(tmp_path: Path, fake_media: dict[str, Any]) -> None:
    asr = FakeAsr()
    assert _check(_edl([("hi", 10.1, 10.4)]), tmp_path / "v1.mp4", asr) == []
    assert asr.calls == []


@pytest.mark.parametrize("where", ["loudness", "asr"])
def test_a_gate_that_errors_is_gate_error_not_a_failure(
    tmp_path: Path,
    fake_media: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    asr = FakeAsr(HEARD)
    if where == "loudness":
        monkeypatch.setattr(q, "measure_loudness", _raise(MediaError("ffmpeg timed out")))
    else:
        asr.fail = MediaError("model fetch timed out")
    assert _check(_edl(JOIN_WORDS, TWO_SEGMENTS), tmp_path / "v1.mp4", asr) == ["gate_error"]
    assert "quality gate" in caplog.text and "timed out" in caplog.text


def test_gate_errors_never_hide_real_failures(
    tmp_path: Path, fake_media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(q, "measure_loudness", _raise(MediaError("boom")))
    reasons = _check(_edl([("hi", 10.6, 10.9)]), tmp_path / "v1.mp4", FakeAsr())
    assert reasons == ["late_speech", "gate_error"]


def test_gates_get_a_bounded_share_of_the_stage(
    tmp_path: Path, fake_media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []

    def measure(video: Path, timeout_s: float) -> tuple[float, float]:
        seen.append(timeout_s)
        return -14.0, -2.0

    monkeypatch.setattr(q, "measure_loudness", measure)
    _check(_edl([("hi", 10.1, 10.4)]), tmp_path / "v1.mp4", FakeAsr())
    assert 0 < seen[0] <= q.GATES_TIMEOUT_S


def test_default_asr_is_the_pinned_tiny_model() -> None:
    asr = q.default_asr(cpu_threads=2)
    assert (asr.model, asr.compute_type, asr.cpu_threads) == ("tiny", "int8", 2)


def _raise(exc: BaseException) -> Any:
    def fail(*a: Any, **kw: Any) -> Any:
        raise exc

    return fail


def _tone_video(path: Path, gain_db: float) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    subprocess.run(  # noqa: S603
        [
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x112:r=10:d=6",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
            "-af", f"volume={gain_db:.2f}dB", "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


@pytest.mark.slow
@pytest.mark.parametrize(("target", "reasons"), [(-20.0, ["loudness"]), (-14.0, [])])
def test_loudness_gate_on_real_audio(tmp_path: Path, target: float, reasons: list[str]) -> None:
    """A clip at -20 LUFS fails the loudness gate; one at -14 LUFS passes (real ffmpeg)."""
    measured, _ = q.measure_loudness(_tone_video(tmp_path / "raw.mp4", 0.0), 60.0)
    video = _tone_video(tmp_path / "v1.mp4", target - measured)
    edl = _edl([("hi", 10.1, 10.4)])
    got = q.check_clip(edl, video, language=None, asr=FakeAsr(), deadline=Deadline())
    assert got == reasons
