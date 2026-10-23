"""Face-tracked 9:16 reframe (CLP-58): crop path, layout choice, model fetch, detection.

`tests/fixtures/face.jpg` is cut from NASA's portrait of astronaut Eileen Collins (public
domain; also shipped as scikit-image's `astronaut` sample), scaled to 128x160 with the face
centred.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipper.edl import FPS, Layout
from clipper.pipeline import reframe as rf
from clipper.pipeline.common import Deadline
from clipper.pipeline.ytdlp import CommandTimeout, RunResult

HALF = 720 * 9 / 16 / 1280 / 2
"""Half the crop width of a 1280x720 source, as a share of the frame width."""
FACE = Path(__file__).parent / "fixtures" / "face.jpg"


def _xs(keys: list[tuple[float, float]], until: float) -> list[float]:
    return [x for t, x in keys if t < until]


# --- per-sample target and layout choice ------------------------------------------------


def _one(faces: list[tuple[float, float]], half: float = 0.5) -> float | None:
    """The target for a single sample (no history)."""
    return rf.face_targets([(0.0, faces)], [], half)[0][1]


def test_face_target() -> None:
    assert _one([]) is None
    assert _one([(0.3, 0.02)]) == 0.3
    # Two faces of similar size (the smaller at least 60% of the larger): their centroid.
    assert _one([(0.2, 0.010), (0.6, 0.006)]) == pytest.approx(0.4)
    assert _one([(0.6, 0.006), (0.2, 0.010)]) == pytest.approx(0.4)
    # A much smaller second face (a background person): the largest face only.
    assert _one([(0.2, 0.010), (0.6, 0.0059)]) == 0.2
    # More than two: the two largest.
    assert _one([(0.9, 0.001), (0.2, 0.010), (0.6, 0.008)]) == pytest.approx(0.4)


def test_pair_too_far_apart_for_the_window_frames_the_largest() -> None:
    # Two similar faces at 0.25 and 0.75 cannot both fit a 9:16 window of a 16:9 frame:
    # centring on the empty gap between them would frame neither.
    assert _one([(0.25, 0.010), (0.75, 0.009)], HALF) == 0.25
    assert _one([(0.75, 0.009), (0.25, 0.010)], HALF) == 0.25
    # Close enough to share the window: their centroid.
    assert _one([(0.45, 0.010), (0.55, 0.009)], HALF) == pytest.approx(0.5)


def test_noisy_two_shot_stays_on_one_face() -> None:
    # Similar faces at 0.3 and 0.7, areas jittering by 10%: "largest" flips sample to
    # sample, but the crop locks onto one face and never wanders between them.
    rng = random.Random(58)  # noqa: S311 - seeded test noise
    samples = [
        (i / 5, [(0.3, 0.01 * rng.uniform(0.9, 1.1)), (0.7, 0.01 * rng.uniform(0.9, 1.1))])
        for i in range(150)
    ]
    _, keys = rf.plan_reframe(rf.Faces(1280, 720, samples), [], 0.0, 30.0)
    assert keys is not None
    xs = {x for _, x in keys}
    assert xs in ({0.3}, {0.7})


def test_pair_near_the_fit_threshold_does_not_flip_flop() -> None:
    window = 2 * HALF
    # dx hovers around 0.8 of the window (between the 0.8 start and the 0.9 stop).
    dxs = [window * (0.78 if i == 0 else 0.8 + 0.06 * (-1) ** i) for i in range(100)]
    samples = [(i / 5, [(0.5 - dx / 2, 0.01), (0.5 + dx / 2, 0.009)]) for i, dx in enumerate(dxs)]
    targets = rf.face_targets(samples, [], HALF)
    assert {round(x, 9) for _, x in targets if x is not None} == {0.5}
    # Past the stop threshold the pair is dropped for the largest face.
    far = [*samples[:5], (1.0, [(0.5 - window * 0.47, 0.01), (0.5 + window * 0.47, 0.009)])]
    assert rf.face_targets(far, [], HALF)[-1][1] == pytest.approx(0.5 - window * 0.47)


def test_clear_sustained_size_change_switches_faces() -> None:
    small, big = 0.01, 0.01 * rf.SWITCH_RATIO * 1.01
    samples = [(i / 5, [(0.3, 0.01), (0.7, 0.009)]) for i in range(10)]
    samples += [(2.0 + i / 5, [(0.3, small), (0.7, big)]) for i in range(10)]
    xs = [x for _, x in rf.face_targets(samples, [], HALF)]
    assert xs[:10] == [0.3] * 10
    # A brief challenge does not switch; a sustained one does, after SWITCH_SAMPLES.
    n = rf.SWITCH_SAMPLES
    assert xs[10 : 10 + n - 1] == [0.3] * (n - 1)
    assert xs[10 + n - 1 :] == [0.7] * (10 - n + 1)
    blip = [*samples[:10], (2.0, [(0.3, small), (0.7, big)]), (2.2, [(0.3, 0.01), (0.7, 0.009)])]
    assert [x for _, x in rf.face_targets(blip, [], HALF)][-2:] == [0.3, 0.3]


TWO = [(0.3, 0.01), (0.7, 0.009)]


def test_single_missed_detection_keeps_the_lock() -> None:
    samples = [(i / 5, TWO) for i in range(40)]
    samples[20] = (4.0, [(0.7, 0.009)])  # the locked 0.3 face is missed once
    assert {x for _, x in rf.face_targets(samples, [], HALF)} == {0.3}


def test_zero_face_sample_keeps_the_lock() -> None:
    samples = [(i / 5, TWO) for i in range(40)]
    samples[20] = (4.0, [])
    samples[21] = (4.2, [(0.3, 0.009), (0.7, 0.01)])  # 0.7 now (slightly) the largest
    xs = [x for _, x in rf.face_targets(samples, [], HALF)]
    assert xs[20] is None  # no detection: not counted as a face for the layout choice
    assert {x for x in xs if x is not None} == {0.3}
    _, keys = rf.plan_reframe(rf.Faces(1280, 720, samples), [], 0.0, 8.0)
    assert keys is not None and {x for _, x in keys} == {0.3}


def test_third_similar_face_causes_no_jumps() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test noise
    samples = [
        (i / 5, [(x, 0.01 * rng.uniform(0.9, 1.1)) for x in (0.2, 0.5, 0.8)]) for i in range(150)
    ]
    xs = [x for _, x in rf.face_targets(samples, [], HALF)]
    assert len(set(xs)) == 1


def test_noisy_two_shot_with_dropouts_barely_crosses() -> None:
    rng = random.Random(58)  # noqa: S311 - seeded test noise
    samples = [
        (
            i / 5,
            [
                (x, 0.01 * rng.uniform(0.9, 1.1))
                for x in (0.3, 0.7)
                if rng.random() > 0.1  # each face missed 10% of the time
            ],
        )
        for i in range(150)
    ]
    _, keys = rf.plan_reframe(rf.Faces(1280, 720, samples), [], 0.0, 30.0)
    assert keys is not None
    sides = [x > 0.5 for _, x in keys]
    assert sum(a != b for a, b in zip(sides, sides[1:], strict=False)) <= 1


def test_missed_speaker_is_not_replaced_by_a_small_face_in_the_gate() -> None:
    # Speakers at 0.3 (locked) and 0.8, a small background face at 0.42 inside the gate;
    # the 0.3 speaker is missed once. The lock must not detour to 0.42 and then switch
    # to 0.8 for good.
    bg = (0.42, 0.004)
    samples = [
        (i / 5, [f for f in [(0.3, 0.0105), (0.8, 0.01), bg] if i != 40 or f[0] != 0.3])
        for i in range(150)
    ]
    assert {x for _, x in rf.face_targets(samples, [], HALF)} == {0.3}


def test_single_miss_with_only_a_small_face_nearby_holds() -> None:
    samples = [
        (i / 5, [(0.3, 0.01), (0.42, 0.004)] if i != 40 else [(0.42, 0.004)]) for i in range(100)
    ]
    assert [x for _, x in rf.face_targets(samples, [], HALF)] == [0.3] * 100


def test_lock_moves_when_the_face_really_leaves() -> None:
    samples = [(i / 5, TWO) for i in range(10)]
    samples += [(2.0 + i / 5, [(0.7, 0.009)]) for i in range(10)]
    xs = [x for _, x in rf.face_targets(samples, [], HALF)]
    g = rf.LOCK_GRACE_SAMPLES
    assert xs[: 10 + g] == [0.3] * (10 + g)  # held through the grace period
    assert xs[10 + g :] == [0.7] * (10 - g)


def test_face_lock_resets_at_a_cut() -> None:
    before = [(i / 5, [(0.3, 0.01), (0.7, 0.009)]) for i in range(10)]
    after = [(2.0 + i / 5, [(0.3, 0.009), (0.7, 0.01)]) for i in range(5)]
    xs = [x for _, x in rf.face_targets(before + after, [2.0], HALF)]
    assert xs[:10] == [0.3] * 10 and xs[10:] == [0.7] * 5  # the new shot's largest face
    xs = [x for _, x in rf.face_targets(before + after, [], HALF)]
    assert xs[10:] == [0.3] * 5  # without the cut, the lock holds


def test_plan_reframe_frames_the_largest_of_a_distant_pair() -> None:
    samples = [(i / 5, [(0.25, 0.010), (0.75, 0.009)]) for i in range(50)]
    _, keys = rf.plan_reframe(rf.Faces(1280, 720, samples), [], 0.0, 10.0)
    assert keys is not None and {x for _, x in keys} == {0.25}


def test_choose_layout() -> None:
    hits = [(i / 5, 0.5) for i in range(6)]
    misses = [(i / 5, None) for i in range(4)]
    assert rf.choose_layout(hits + misses) is Layout.FACE_TRACK  # 60%
    assert rf.choose_layout(hits[:5] + misses + [(2.0, None)]) is Layout.BLURRED_FILL  # 50%
    assert rf.choose_layout([]) is Layout.BLURRED_FILL


# --- crop path --------------------------------------------------------------------------


def _samples(target: Any, duration: float) -> list[tuple[float, float | None]]:
    return [(i / rf.SAMPLE_FPS, target(i / rf.SAMPLE_FPS)) for i in range(int(duration * 5) + 1)]


def test_steady_target_gives_a_steady_crop() -> None:
    keys = rf.crop_path(_samples(lambda t: 0.4, 4.0), [], 4.0, HALF)
    assert keys[0][0] == 0.0
    assert keys[-1][0] == pytest.approx((4.0 * FPS - 1) / FPS)
    assert {x for _, x in keys} == {0.4}
    assert len(keys) <= 4.0 * rf.SAMPLE_FPS + 2  # about one key per sample, not per frame
    assert [t for t, _ in keys] == sorted(t for t, _ in keys)


def test_dead_zone_holds_the_crop() -> None:
    # The target sits 4% of the frame width away: inside the 5% dead-zone, no move.
    target = lambda t: 0.5 if t < 1 else 0.54  # noqa: E731
    keys = rf.crop_path(_samples(target, 4.0), [], 4.0, HALF)
    assert {x for _, x in keys} == {0.5}


def test_speed_is_capped_per_frame() -> None:
    target = lambda t: 0.3 if t < 1 else 0.7  # noqa: E731
    keys = rf.crop_path(_samples(target, 6.0), [], 6.0, HALF)
    for (t0, x0), (t1, x1) in zip(keys, keys[1:], strict=False):
        frames = round((t1 - t0) * FPS)
        assert abs(x1 - x0) <= rf.MAX_SPEED * frames + 1e-9
    assert _xs(keys, 1.0) == [0.3] * len(_xs(keys, 1.0))
    assert abs(keys[-1][1] - 0.7) <= rf.DEAD_ZONE  # it got there, eased


def test_cut_resets_the_crop_without_easing() -> None:
    target = lambda t: 0.3 if t < 1 else 0.7  # noqa: E731
    keys = rf.crop_path(_samples(target, 3.0), [1.0], 3.0, HALF)
    # Two keys at the cut: the old position, then the new one; nothing in between.
    at_cut = [x for t, x in keys if t == pytest.approx(1.0)]
    assert at_cut == [0.3, 0.7]
    assert set(_xs(keys, 1.0)) == {0.3}
    assert {x for t, x in keys if t > 1.0} == {0.7}


def test_cut_between_samples_jumps_to_the_first_sample_after_it() -> None:
    # Cut at 1.1 s, samples every 0.2 s: the new shot's first sample is at 1.2 s, and the
    # crop jumps on the cut's frame (33), not the sample's frame.
    target = lambda t: 0.3 if t < 1.1 else 0.7  # noqa: E731
    keys = rf.crop_path(_samples(target, 3.0), [1.1], 3.0, HALF)
    assert [x for t, x in keys if t == pytest.approx(33 / FPS)] == [0.3, 0.7]
    assert {x for t, x in keys if t > 33 / FPS} == {0.7}


def test_crop_window_is_clamped_inside_the_frame() -> None:
    left = rf.crop_path(_samples(lambda t: 0.02, 2.0), [], 2.0, HALF)
    right = rf.crop_path(_samples(lambda t: 0.99, 2.0), [], 2.0, HALF)
    assert [x for _, x in left] == pytest.approx([HALF] * len(left))
    assert [x for _, x in right] == pytest.approx([1 - HALF] * len(right))


def test_faces_missing_hold_the_last_position_and_start_on_the_first_face() -> None:
    target = lambda t: None if t < 1 or 2 <= t < 3 else 0.7  # noqa: E731
    keys = rf.crop_path(_samples(target, 4.0), [], 4.0, HALF)
    assert {x for _, x in keys} == {0.7}  # no ease in from the centre either


def test_alpha_is_configurable() -> None:
    # alpha=1, no dead-zone, no speed cap: the crop follows each sample exactly.
    target = lambda t: 0.3 + 0.1 * t  # noqa: E731
    keys = rf.crop_path(_samples(target, 2.0), [], 2.0, 0.1, alpha=1.0, dead_zone=0, max_speed=1)
    for t, x in keys:
        assert x == pytest.approx(0.3 + 0.1 * math.floor(t * 5 + 1e-9) / 5, abs=1e-4)


def test_plan_reframe_uses_clip_time() -> None:
    # Section samples every 0.2 s from 0 to 20 s; clip [5.0, 15.0]; cut at 10.0 (clip 5.0).
    samples = [(i / 5, [(0.3 if i < 50 else 0.7, 0.02)]) for i in range(101)]
    faces = rf.Faces(1280, 720, samples)
    layout, keys = rf.plan_reframe(faces, [2.0, 10.0, 17.0], 5.0, 15.0)
    assert layout is Layout.FACE_TRACK and keys is not None
    assert keys[0] == (0.0, 0.3)
    assert [x for t, x in keys if t == pytest.approx(5.0)] == [0.3, 0.7]
    assert keys[-1][0] == pytest.approx(10.0 - 1 / FPS)


def test_clip_origin_rounds_half_up_like_remotion() -> None:
    # in_s = 0.15 s is frame 4.5: Remotion's Math.round trims 5 frames (Python's round would
    # say 4), so a cut at 0.15 s is on clip frame 0 and the crop starts in the new shot.
    samples = [(i / 5, [(0.3 if i / 5 < 0.15 else 0.7, 0.02)]) for i in range(30)]
    _, keys = rf.plan_reframe(rf.Faces(1280, 720, samples), [0.15], 0.15, 5.15)
    assert keys is not None
    assert keys[0] == (0.0, 0.7)
    assert 0.3 not in {x for _, x in keys}


def test_many_cuts_stay_cheap_and_correct() -> None:
    cuts = [i / 30 + 0.001 for i in range(1, 5000)]
    samples = [(i / 5, 0.5) for i in range(3000)]
    keys = rf.crop_path(samples, cuts, 180.0, HALF)
    assert {x for _, x in keys} == {0.5}


def test_plan_reframe_falls_back_to_blurred_fill() -> None:
    few = [(i / 5, [(0.5, 0.02)] if i % 2 else []) for i in range(50)]  # faces in 50%
    assert rf.plan_reframe(rf.Faces(1280, 720, few), [], 0.0, 10.0) == (Layout.BLURRED_FILL, None)
    full = [(i / 5, [(0.5, 0.02)]) for i in range(50)]
    # A portrait source is already narrower than 9:16: nothing to crop.
    assert rf.plan_reframe(rf.Faces(720, 1280, full), [], 0.0, 10.0) == (Layout.BLURRED_FILL, None)
    # Faces only outside the clip.
    assert rf.plan_reframe(rf.Faces(1280, 720, full), [], 20.0, 30.0) == (Layout.BLURRED_FILL, None)


def test_plan_reframe_too_many_keys_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rf, "MAX_CROP_KEYS", 10)
    full = [(i / 5, [(0.5, 0.02)]) for i in range(50)]
    assert rf.plan_reframe(rf.Faces(1280, 720, full), [], 0.0, 10.0) == (Layout.BLURRED_FILL, None)


# --- model download ---------------------------------------------------------------------


GOOD = b"model bytes"


@pytest.fixture(autouse=True)
def _no_failure_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rf, "_fetch_failed_at", None, raising=False)


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> str:
    sha = hashlib.sha256(GOOD).hexdigest()
    monkeypatch.setattr(rf, "MODEL_SHA256", sha)
    return sha


def test_model_pin_is_exact() -> None:
    assert rf.MODEL_URL.startswith("https://storage.googleapis.com/mediapipe-models/")
    assert rf.MODEL_URL.endswith("/float16/1/blaze_face_short_range.tflite")  # not "latest"
    assert len(rf.MODEL_SHA256) == 64 and int(rf.MODEL_SHA256, 16) >= 0


def test_models_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLIPPER_MODELS_DIR", raising=False)
    assert rf.models_dir() == Path("data/models").resolve()
    monkeypatch.setenv("CLIPPER_MODELS_DIR", str(tmp_path))
    assert rf.models_dir() == tmp_path


def test_model_is_fetched_once_and_verified(tmp_path: Path, pinned: str) -> None:
    calls: list[str] = []

    def fetch(url: str, timeout_s: float) -> bytes:
        calls.append(url)
        return GOOD

    path = rf.ensure_model(tmp_path / "models", fetch=fetch)
    assert path == tmp_path / "models" / rf.MODEL_NAME
    assert path.read_bytes() == GOOD
    assert rf.ensure_model(tmp_path / "models", fetch=fetch) == path
    assert calls == [rf.MODEL_URL]


def test_hash_mismatch_disables_face_tracking(
    tmp_path: Path, pinned: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert rf.ensure_model(tmp_path, fetch=lambda url, timeout_s: b"tampered") is None
    assert "checksum" in caplog.text
    assert list(tmp_path.iterdir()) == []  # nothing unverified is kept


def test_changed_model_on_disk_is_replaced(tmp_path: Path, pinned: str) -> None:
    (tmp_path / rf.MODEL_NAME).write_bytes(b"old or tampered")
    assert rf.ensure_model(tmp_path, fetch=lambda url, timeout_s: GOOD) == tmp_path / rf.MODEL_NAME
    assert (tmp_path / rf.MODEL_NAME).read_bytes() == GOOD


def test_download_failure_disables_face_tracking(
    tmp_path: Path, pinned: str, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(url: str, timeout_s: float) -> bytes:
        raise httpx.ConnectError("offline")

    assert rf.ensure_model(tmp_path, fetch=fail) is None
    assert "face model" in caplog.text


def test_failed_fetch_is_not_retried_for_a_while(
    tmp_path: Path, pinned: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    now = [1000.0]
    monkeypatch.setattr(rf.time, "monotonic", lambda: now[0])

    def tampered(url: str, timeout_s: float) -> bytes:
        calls.append(url)
        return b"tampered"

    assert rf.ensure_model(tmp_path, fetch=tampered) is None
    now[0] += rf.MODEL_RETRY_S - 1
    assert rf.ensure_model(tmp_path, fetch=tampered) is None
    assert len(calls) == 1  # each later clip falls back at once, without a 30 s fetch
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    now[0] += 2
    assert rf.ensure_model(tmp_path, fetch=lambda url, timeout_s: GOOD) is not None
    assert rf._fetch_failed_at is None


@pytest.mark.parametrize(
    ("error", "timeout_s", "remembered"),
    [
        (httpx.ReadTimeout("slow"), 1.5, False),  # this job's deadline was nearly spent
        (TimeoutError("face model download too slow"), 1.5, False),
        (httpx.ReadTimeout("slow"), rf.MODEL_TIMEOUT_S, True),  # a real full-length timeout
        (httpx.ConnectError("refused"), 1.5, True),
        (ValueError("face model download is too large"), 1.5, True),
    ],
)
def test_only_real_fetch_failures_are_remembered(
    tmp_path: Path, pinned: str, error: Exception, timeout_s: float, remembered: bool
) -> None:
    def fail(url: str, t: float) -> bytes:
        raise error

    assert rf.ensure_model(tmp_path, fetch=fail, timeout_s=timeout_s) is None
    assert (rf._fetch_failed_at is not None) is remembered


def test_short_timeout_mismatch_is_still_remembered(tmp_path: Path, pinned: str) -> None:
    assert rf.ensure_model(tmp_path, fetch=lambda u, t: b"tampered", timeout_s=1.5) is None
    assert rf._fetch_failed_at is not None


def test_fetch_warning_is_cleaned_and_bounded(
    tmp_path: Path, pinned: str, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(url: str, t: float) -> bytes:
        raise ValueError("bad\x1b[31m\u202e" + "x" * 2000)

    assert rf.ensure_model(tmp_path, fetch=fail) is None
    (warning,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "\x1b" not in warning and "\u202e" not in warning
    assert len(warning) < 400


def test_model_read_error_never_fails_the_clip(
    tmp_path: Path, pinned: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / rf.MODEL_NAME).write_bytes(GOOD)

    def eio(fd: int) -> Any:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(rf.os, "fstat", eio)
    assert rf.read_model(tmp_path / rf.MODEL_NAME) is None
    assert rf.ensure_model(tmp_path, fetch=lambda u, t: b"tampered") is None  # no raise


def test_model_read_refuses_symlinks_and_fifos(tmp_path: Path, pinned: str) -> None:
    real = tmp_path / "elsewhere.tflite"
    real.write_bytes(GOOD)
    link = tmp_path / "models" / rf.MODEL_NAME
    link.parent.mkdir()
    link.symlink_to(real)
    assert rf.read_model(link) is None
    fifo = tmp_path / "fifo.tflite"
    os.mkfifo(fifo)
    assert rf.read_model(fifo) is None  # returns at once: never waits for a writer
    assert rf.read_model(real) == GOOD
    real.write_bytes(b"tampered")
    assert rf.read_model(real) is None
    # A symlinked model is replaced by a verified regular file.
    assert rf.ensure_model(link.parent, fetch=lambda url, timeout_s: GOOD) == link
    assert not link.is_symlink() and link.read_bytes() == GOOD


def test_child_verifies_the_model_itself(tmp_path: Path, section: Path) -> None:
    bad = tmp_path / rf.MODEL_NAME
    bad.write_bytes(b"swapped after the parent's check")
    with pytest.raises(ValueError, match="checksum"):
        rf.scan(bad, section)


def test_download_timeout_covers_every_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = rf.httpx.stream

    def spy(*args: Any, **kw: Any) -> Any:
        seen.append(kw["timeout"])
        return real(*args, **kw)

    monkeypatch.setattr(rf.httpx, "stream", spy)
    with respx.mock:
        respx.get(rf.MODEL_URL).mock(return_value=httpx.Response(200, content=GOOD))
        rf.download(rf.MODEL_URL, 30.0)
    (timeout,) = seen
    # A quarter per phase: TCP connect and TLS each get the connect timeout, then headers,
    # then body reads (none starts after three quarters).
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == timeout.read == timeout.write == timeout.pool == 7.5


@respx.mock
def test_download_is_bounded() -> None:
    respx.get(rf.MODEL_URL).mock(return_value=httpx.Response(200, content=GOOD))
    assert rf.download(rf.MODEL_URL, 5.0) == GOOD
    respx.get(rf.MODEL_URL).mock(
        return_value=httpx.Response(200, content=b"x" * (rf.MODEL_MAX_BYTES + 1))
    )
    with pytest.raises(ValueError):
        rf.download(rf.MODEL_URL, 5.0)
    respx.get(rf.MODEL_URL).mock(return_value=httpx.Response(302, headers={"location": "x"}))
    with pytest.raises(httpx.HTTPStatusError):
        rf.download(rf.MODEL_URL, 5.0)  # no redirects off the pinned host


# --- detection child --------------------------------------------------------------------


class FakeRunner:
    def __init__(self, result: RunResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float, dict[str, Any]]] = []

    def __call__(self, args: list[str], timeout_s: float, **kw: Any) -> RunResult:
        self.calls.append((list(args), timeout_s, kw))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def section(tmp_path: Path) -> Path:
    path = tmp_path / "clips" / "1" / "source.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mp4")
    return path


OUT = {"width": 1280, "height": 720, "samples": [[0.0, [[0.4, 0.02]]], [0.2, []]]}


def test_detect_faces_runs_hardened_child_and_caches(section: Path, tmp_path: Path) -> None:
    model = tmp_path / rf.MODEL_NAME
    runner = FakeRunner(RunResult(0, json.dumps(OUT) + "\n", ""))

    faces = rf.detect_faces(section, model, Deadline(), runner=runner)

    assert faces == rf.Faces(1280, 720, [(0.0, [(0.4, 0.02)]), (0.2, [])])
    ((args, timeout_s, kw),) = runner.calls
    assert args == [
        sys.executable, "-I", "-m", "clipper.pipeline.reframe", str(model), str(section)
    ]  # fmt: skip
    assert kw == {"env": {"MALLOC_ARENA_MAX": "2"}}
    assert 0 < timeout_s <= rf.FACE_TIMEOUT_S
    cached = json.loads(section.with_name("source.mp4.faces.json").read_text())
    assert cached["faces"] == OUT and cached["size"] == section.stat().st_size
    again = FakeRunner(CommandTimeout("must not run"))
    assert rf.detect_faces(section, model, Deadline(), runner=again) == faces
    assert again.calls == []


@pytest.mark.parametrize(
    "result",
    [
        CommandTimeout("python timed out after 120s"),
        RunResult(1, "", "Traceback\nRuntimeError: token=sk-ant-api03-" + "x" * 40),
        RunResult(0, "not json", ""),
        RunResult(0, json.dumps({**OUT, "width": 0}), ""),
        RunResult(0, json.dumps({**OUT, "width": True}), ""),
        RunResult(0, json.dumps({**OUT, "samples": [[0.0, [[1.5, 0.02]]]]}), ""),
        RunResult(0, json.dumps({**OUT, "samples": [[-1.0, []]]}), ""),
        RunResult(0, json.dumps({**OUT, "samples": [[0.0, [[0.5, "big"]]]]}), ""),
        RunResult(0, json.dumps({**OUT, "samples": [[0.0, []]] * (rf.MAX_SAMPLES + 1)}), ""),
        RunResult(0, json.dumps({**OUT, "samples": [[0.0, [[0.5, 0.1]] * 9]]}), ""),
        OSError("no such interpreter"),
    ],
)
def test_detection_failure_means_no_face_tracking(
    section: Path, result: Any, caplog: pytest.LogCaptureFixture
) -> None:
    assert rf.detect_faces(section, Path("m"), Deadline(), runner=FakeRunner(result)) is None
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "face detection" in warning.getMessage()
    assert "sk-ant-api03" not in warning.getMessage()
    assert not section.with_name("source.mp4.faces.json").exists()


def test_bad_faces_cache_is_ignored(section: Path) -> None:
    stat = section.stat()
    key = {"v": 1, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    section.with_name("source.mp4.faces.json").write_text(
        json.dumps({**key, "faces": {**OUT, "height": -1}})
    )
    runner = FakeRunner(RunResult(0, json.dumps(OUT), ""))
    assert rf.detect_faces(section, Path("m"), Deadline(), runner=runner) is not None
    assert len(runner.calls) == 1


# --- integration: a moving face ---------------------------------------------------------


def _model() -> Path:
    path = rf.models_dir() / rf.MODEL_NAME
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != rf.MODEL_SHA256:
        pytest.skip("face model not in CLIPPER_MODELS_DIR (or data/models)")
    return path


def _crop_x(keys: list[tuple[float, float]], t: float) -> float:
    """Mirror of render/src/crop.ts: the last key at or before t, linear to the next one."""
    i = sum(kt <= t + 1e-9 for kt, _ in keys) - 1  # keys are sorted: a prefix
    if i < 0 or i + 1 == len(keys):
        return keys[max(i, 0)][1]
    (t0, x0), (t1, x1) = keys[i], keys[i + 1]
    return x0 + (x1 - x0) * (t - t0) / (t1 - t0)


def _face_left(t: float) -> float:
    return 100 + 80 * t if t < 4 else 900 - 60 * (t - 4)


@pytest.mark.slow
def test_crop_follows_a_moving_face(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    model = _model()
    path = tmp_path / "source.mp4"
    # 8 s at 1280x720: a grey shot with the face moving right, then a hard cut to a blue
    # shot with the face moving left from elsewhere. The face (3x the fixture) is centred
    # in its image, so its centre is at x + 192.
    x = "if(lt(t,4),100+80*t,900-60*(t-4))"
    bg = "color=c={}:s=1280x720:r=30:d=4"
    subprocess.run(  # noqa: S603
        [
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", bg.format("0x707070"),
            "-f", "lavfi", "-i", bg.format("0x203080"),
            "-loop", "1", "-i", str(FACE),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[bg];[2:v]scale=384:480[f];"
            f"[bg][f]overlay=x='{x}':y=120:shortest=1",
            "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path),
        ],
        check=True,
    )  # fmt: skip
    from clipper.pipeline.boundaries import detect_cuts

    cuts = detect_cuts(path, Deadline())
    assert cuts == [pytest.approx(4.0, abs=0.1)]
    faces = rf.detect_faces(path, model, Deadline())  # through the hardened child
    assert faces is not None
    assert rf._faces(rf.scan(model, path)) == faces  # in process, the same result

    layout, keys = rf.plan_reframe(faces, cuts, 0.0, 8.0)

    assert layout is Layout.FACE_TRACK and keys is not None
    inside = 0
    frames = 8 * FPS
    for f in range(frames):
        t = f / FPS
        face_x = (_face_left(t) + 192) / 1280
        inside += abs(face_x - _crop_x(keys, t)) <= 0.6 * HALF  # central 60% of the crop
    assert inside >= 0.95 * frames
