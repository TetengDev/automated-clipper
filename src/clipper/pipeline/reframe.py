"""Face-tracked 9:16 reframe (CLP-58).

`detect_faces` runs the MediaPipe Tasks face detector (BlazeFace short range) on the padded
section, SAMPLE_FPS frames a second downscaled to SAMPLE_WIDTH px, in a child process with
the same limits, timeout, cache (`<section>.faces.json`) and fail-open path as scene
detection (`boundaries.cached_child`). The model is fetched once into the models dir
(CLIPPER_MODELS_DIR, else data/models) from a pinned URL and its SHA-256 is checked before
every use (`ensure_model`).

`plan_reframe` is pure: per sample the largest face's centre (or the centroid of two faces
of similar size); `crop_path` smooths that per output frame (EMA, dead-zone, speed cap),
jumps at scene cuts and keeps the 9:16 window inside the frame; `choose_layout` picks
face_track only when faces are in at least FACE_SHARE of the samples. Any failure means
blurred_fill: a clip never fails for want of faces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import sys
import tempfile
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from clipper.edl import FPS, MAX_CROP_KEYS, Layout
from clipper.pipeline.boundaries import cached_child, harden
from clipper.pipeline.common import Deadline, strip_control
from clipper.pipeline.ytdlp import Runner
from clipper.redact import redact

logger = logging.getLogger(__name__)

SAMPLE_FPS = 5.0
SAMPLE_WIDTH = 640
FACE_TIMEOUT_S = 120.0
MODEL_TIMEOUT_S = 30.0
MODEL_NAME = "blaze_face_short_range.tflite"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/"
    f"float16/1/{MODEL_NAME}"
)
MODEL_SHA256 = "b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f"
MODEL_MAX_BYTES = 1024 * 1024
MAX_REASON_CHARS = 300
MODEL_RETRY_S = 600.0
"""After a failed or mismatched fetch, no new fetch for this long (in this process)."""
MAX_SAMPLES = 3000
"""10 minutes at SAMPLE_FPS; a section is one clip plus padding."""
MAX_FACES = 4
"""Faces kept per sample (the largest); only the two largest are ever used."""
EMA_ALPHA = 0.3
DEAD_ZONE = 0.05
"""No move while the target is this close to the crop centre (share of the frame width)."""
MAX_SPEED = 0.03
"""Most the crop centre moves in one output frame (share of the frame width)."""
PAIR_RATIO = 0.6
"""Two faces are framed together when the smaller has at least this share of the area..."""
PAIR_FIT = 0.8
"""...and both centres fit in this central share of the crop window (to start pairing)..."""
PAIR_KEEP = 0.9
"""...or this share (to keep pairing): hysteresis, so the target does not flip-flop."""
LOCK_GRACE_SAMPLES = 5
"""Samples (1 s at SAMPLE_FPS) the lock holds while its face is not detected."""
SWITCH_RATIO = 1.25
SWITCH_SAMPLES = 3
"""The crop leaves its face for one SWITCH_RATIO larger for this many samples in a row."""
FACE_SHARE = 0.6
CUT_EPS_FRAMES = 0.05
"""Cut times are rounded to ms: a cut this close past a frame still starts on that frame."""

Face = tuple[float, float]
"""(centre x, area), as shares of the frame width and area."""


_fetch_failed_at: float | None = None


class Faces(NamedTuple):
    width: int
    height: int
    samples: list[tuple[float, list[Face]]]
    """(seconds in the section, faces) per sampled frame."""


def face_targets(
    samples: Sequence[tuple[float, Sequence[Face]]], cuts: Sequence[float], half_width: float = 0.5
) -> list[tuple[float, float | None]]:
    """The crop target per sample (time kept; None without faces).

    Two faces of similar size (the smaller at least PAIR_RATIO of the larger) that fit the
    window (`half_width` is half the crop width) give their centroid; pairing starts within
    PAIR_FIT of the window and stops only beyond PAIR_KEEP. Otherwise one face is locked and
    followed: the nearest face within `half_width` of the lock and at least PAIR_RATIO of its
    last area. While no face matches (a missed detection, or none at all) the lock holds for
    LOCK_GRACE_SAMPLES, then moves to the largest face. The lock moves to another face early
    only once the largest other face is at least SWITCH_RATIO larger for SWITCH_SAMPLES
    samples in a row, so noisy sizes never flip it. All of this resets at a cut."""
    cuts = sorted(cuts)
    out: list[tuple[float, float | None]] = []
    shot, lock, size, paired, streak, missed = -1, None, 0.0, False, 0, 0
    for t, faces in samples:
        if (here := bisect_right(cuts, t)) != shot:
            shot, lock, size, paired, streak, missed = here, None, 0.0, False, 0, 0
        top = sorted(faces, key=lambda f: f[1], reverse=True)[:2]
        fit = 2 * half_width * (PAIR_KEEP if paired else PAIR_FIT)
        paired = len(top) == 2 and (
            top[1][1] >= PAIR_RATIO * top[0][1] and abs(top[0][0] - top[1][0]) <= fit
        )
        if paired:
            lock, size, streak, missed = None, 0.0, 0, 0
            out.append((t, (top[0][0] + top[1][0]) / 2))
            continue
        near = [
            f
            for f in faces
            if lock is not None and abs(f[0] - lock) <= half_width and f[1] >= PAIR_RATIO * size
        ]
        if lock is not None and not near:
            missed += 1
            if missed <= LOCK_GRACE_SAMPLES or not faces:
                out.append((t, lock if faces else None))  # None: no face for the layout share
                continue
        if not faces:
            out.append((t, None))
            continue
        mine = min(near, key=lambda f: abs(f[0] - lock)) if near else top[0]
        missed = 0
        rival = max((f for f in faces if f is not mine), key=lambda f: f[1], default=None)
        streak = streak + 1 if rival and rival[1] >= SWITCH_RATIO * mine[1] else 0
        if rival and streak >= SWITCH_SAMPLES:
            mine, streak = rival, 0
        lock, size = mine
        out.append((t, lock))
    return out


def choose_layout(samples: Sequence[tuple[float, float | None]]) -> Layout:
    """face_track when a face is in at least FACE_SHARE of the samples."""
    hits = sum(x is not None for _, x in samples)
    return (
        Layout.FACE_TRACK if samples and hits >= FACE_SHARE * len(samples) else Layout.BLURRED_FILL
    )


def crop_path(
    samples: Sequence[tuple[float, float | None]],
    cuts: Sequence[float],
    duration_s: float,
    half_width: float,
    *,
    alpha: float = EMA_ALPHA,
    dead_zone: float = DEAD_ZONE,
    max_speed: float = MAX_SPEED,
    fps: int = FPS,
) -> list[tuple[float, float]]:
    """Crop-centre keys (clip seconds, share of the width) for a clip of `duration_s`.

    `samples` are (clip seconds, target or None) and `cuts` clip seconds. Per output frame
    the EMA of the targets moves the centre at most `max_speed`, and only while it is more
    than `dead_zone` away. Each shot starts on its first face (no easing across a cut: two
    keys at the cut's frame, old then new); without faces the centre holds. The centre stays
    within [half_width, 1 - half_width]. One key per sample interval, plus the cut keys and
    the last frame."""
    lo, hi = (half_width, 1 - half_width) if half_width < 0.5 else (0.5, 0.5)
    seen = sorted((t, x) for t, x in samples if x is not None)
    times = [t for t, _ in seen]
    frames = max(1, math.floor(duration_s * fps + 0.5))
    marks = sorted((min(max(math.ceil(c * fps - CUT_EPS_FRAMES), 0), frames), c) for c in cuts)
    first = max((c for f, c in marks if f == 0), default=-math.inf)
    bounds = [(0, first), *((f, c) for f, c in marks if 0 < f < frames), (frames, math.inf)]
    x = min(max(seen[0][1] if seen else 0.5, lo), hi)
    xs: list[float] = []
    for (f0, c0), (f1, c1) in zip(bounds, bounds[1:], strict=False):
        shot = seen[bisect_left(times, c0) : bisect_left(times, c1)]
        ema = shot[0][1] if shot else x
        x, i = min(max(ema, lo), hi), 0
        for f in range(f0, f1):
            while i < len(shot) and shot[i][0] <= f / fps:
                ema += alpha * (shot[i][1] - ema)
                i += 1
            if abs(ema - x) > dead_zone:
                x = min(max(x + max(-max_speed, min(max_speed, ema - x)), lo), hi)
            xs.append(x)
    every = max(1, round(fps / SAMPLE_FPS))
    jumps = {f for f, _ in bounds[1:-1]}
    keys: list[tuple[float, float]] = []
    for f, x in enumerate(xs):
        if f in jumps:
            keys.append((f / fps, xs[f - 1]))
        if f % every == 0 or f in jumps or f == frames - 1:
            keys.append((f / fps, x))
    return keys


def plan_reframe(
    faces: Faces, cuts: Sequence[float], in_s: float, out_s: float
) -> tuple[Layout, list[tuple[float, float]] | None]:
    """(layout, crop keys or None) for the clip [in_s, out_s] of the section (seconds)."""
    # Remotion trims the video to whole frames with Math.round (half up, not to even).
    origin = math.floor(in_s * FPS + 0.5) / FPS
    half = faces.height * 9 / 16 / faces.width / 2
    clip = [(t - origin, f) for t, f in faces.samples if in_s - 1 / SAMPLE_FPS < t <= out_s]
    samples = face_targets(clip, [c - origin for c in cuts], half)
    if half >= 0.5 or choose_layout(samples) is Layout.BLURRED_FILL:
        return Layout.BLURRED_FILL, None  # also a source already as narrow as 9:16
    keys = crop_path(samples, [c - origin for c in cuts], out_s - in_s, half)
    if len(keys) > MAX_CROP_KEYS:
        return Layout.BLURRED_FILL, None
    return Layout.FACE_TRACK, keys


def models_dir() -> Path:
    return Path(os.environ.get("CLIPPER_MODELS_DIR") or "data/models")


def download(url: str, timeout_s: float) -> bytes:
    """GET `url` with no redirects, at most MODEL_MAX_BYTES, in about `timeout_s`: each phase
    (TCP connect, TLS, headers, a body read) gets a quarter, and no body read starts after
    three quarters. DNS resolution is not covered by these timeouts."""
    data = bytearray()
    end = time.monotonic() + timeout_s * 3 / 4
    with httpx.stream(
        "GET", url, timeout=httpx.Timeout(timeout_s / 4), follow_redirects=False
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            data += chunk
            if len(data) > MODEL_MAX_BYTES:
                raise ValueError("face model download is too large")
            if time.monotonic() > end:
                raise TimeoutError("face model download is too slow")
    return bytes(data)


def read_model(path: Path) -> bytes | None:
    """The model bytes if `path` is a regular file (not a symlink, FIFO or device) matching
    MODEL_SHA256; else None. Read once, at most MODEL_MAX_BYTES."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            data = fh.read(MODEL_MAX_BYTES + 1)
        except OSError:  # e.g. EIO: no face tracking, never a failed clip
            return None
    return data if hashlib.sha256(data).hexdigest() == MODEL_SHA256 else None


def ensure_model(
    directory: Path | None = None,
    *,
    fetch: Callable[[str, float], bytes] = download,
    timeout_s: float = MODEL_TIMEOUT_S,
) -> Path | None:
    """The verified model file, fetched from MODEL_URL if missing or changed; None with a
    warning if it cannot be fetched or does not match MODEL_SHA256 (nothing unverified is
    kept). After a failure, fetches are skipped for MODEL_RETRY_S (one warning, not one
    timeout per clip)."""
    global _fetch_failed_at
    path = (directory or models_dir()) / MODEL_NAME
    if read_model(path) is not None:
        return path
    if _fetch_failed_at is not None and time.monotonic() - _fetch_failed_at < MODEL_RETRY_S:
        return None
    try:
        data = fetch(MODEL_URL, timeout_s)
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise ValueError(f"checksum mismatch for {MODEL_URL}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{MODEL_NAME}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    except Exception as exc:
        # A timeout under a shortened deadline says nothing about the network: retry later.
        short = timeout_s < MODEL_TIMEOUT_S
        if not (short and isinstance(exc, httpx.TimeoutException | TimeoutError)):
            _fetch_failed_at = time.monotonic()
        text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        reason = redact(strip_control(text))[:MAX_REASON_CHARS]
        logger.warning("face model fetch failed, face tracking off: %s", reason)
        return None
    _fetch_failed_at = None
    return path


def _number(value: Any, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= hi:
        raise ValueError("not face data")
    return float(value)


def _faces(data: Any) -> Faces:
    width, height, samples = data["width"], data["height"], data["samples"]
    if not (isinstance(samples, list) and len(samples) <= MAX_SAMPLES) or not all(
        isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 16384 for v in (width, height)
    ):
        raise ValueError("not face data")
    out: list[tuple[float, list[Face]]] = []
    for t, found in samples:
        if not isinstance(found, list) or len(found) > MAX_FACES:
            raise ValueError("not face data")
        out.append((_number(t, 1e6), [(_number(x, 1), _number(a, 1)) for x, a in found]))
    return Faces(width, height, out)


def detect_faces(
    section: Path, model: Path, deadline: Deadline, *, runner: Runner | None = None
) -> Faces | None:
    """Faces sampled over `section`; None with a warning on any failure."""
    args = [sys.executable, "-I", "-m", __name__, str(model), str(section)]
    return cached_child(
        section, "faces", args, _faces, deadline,
        what="face detection", fallback="blurred fill", timeout_s=FACE_TIMEOUT_S, runner=runner,
    )  # fmt: skip


def scan(model: Path, path: Path) -> dict[str, Any]:
    """Run the face detector in this process (the child side of `detect_faces`)."""
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    model_data = read_model(model)  # checked again here: the file may change after the parent
    if model_data is None:
        raise ValueError("face model checksum mismatch")
    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not (cap.isOpened() and 0 < fps <= 1000 and width > 0 and height > 0):
        raise ValueError("cannot read the video")
    scale = min(1.0, SAMPLE_WIDTH / width)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    options = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_buffer=model_data, delegate=BaseOptions.Delegate.CPU)
    )
    samples: list[list[Any]] = []
    index, due = 0, 0.0
    with vision.FaceDetector.create_from_options(options) as detector:
        while len(samples) < MAX_SAMPLES and cap.grab():
            if index >= due:
                due += fps / SAMPLE_FPS
                ok, frame = cap.retrieve()
                if ok:
                    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                    image = mp.Image(
                        image_format=mp.ImageFormat.SRGB,
                        data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB),
                    )
                    boxes = [d.bounding_box for d in detector.detect(image).detections]
                    found = [
                        (
                            min(max((b.origin_x + b.width / 2) / size[0], 0.0), 1.0),
                            min(b.width * b.height / (size[0] * size[1]), 1.0),
                        )
                        for b in boxes
                    ]
                    found.sort(key=lambda f: f[1], reverse=True)
                    faces = [[round(x, 4), round(a, 6)] for x, a in found[:MAX_FACES]]
                    samples.append([round(index / fps, 3), faces])
            index += 1
    return {"width": width, "height": height, "samples": samples}


if __name__ == "__main__":  # pragma: no cover - child process of detect_faces
    harden()
    print(json.dumps(scan(Path(sys.argv[1]), Path(sys.argv[2]))))
