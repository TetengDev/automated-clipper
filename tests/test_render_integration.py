"""Real render through Remotion + ffmpeg. Skips unless node, ffmpeg and
render/node_modules are available (CI's Python job has no Node; the node job covers TS)."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from clipper.edl import parse_edl
from clipper.render import REMOTION_CLI, TARGET_LUFS, render_clip

REPO = Path(__file__).resolve().parents[1]
RENDER_DIR = REPO / "render"
WORDS = ["we", "did", "not", "see", "that", "coming", "at", "all"]

pytestmark = [
    pytest.mark.render,
    pytest.mark.skipif(
        not (shutil.which("node") and shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="needs node, ffmpeg and ffprobe",
    ),
    pytest.mark.skipif(
        not (RENDER_DIR / REMOTION_CLI).exists(),
        reason="run `npm ci` in render/ first",
    ),
]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=300)  # noqa: S603


def _make_fixture(media_root: Path, seconds: float = 7.0) -> None:
    (media_root / "src").mkdir(parents=True)
    _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-af", "volume=-20dB",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(media_root / "src" / "fixture.mp4"),
        ]
    )  # fmt: skip


def _integrated_lufs(path: Path) -> float:
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "ebur128",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    summary = proc.stderr.rsplit("Summary:", 1)[1]
    line = next(ln for ln in summary.splitlines() if ln.strip().startswith("I:"))
    return float(line.split()[1])


def _hook_yellow_fraction(path: Path, at_s: float) -> float:
    """Fraction of pixels in the hook-card band matching the `stakes` card yellow (#FFD400).
    The blurred background is darkened, so nothing else in the band is that bright."""
    raw = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(path), "-frames:v", "1",
            "-vf", "crop=1080:440:0:200", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout  # fmt: skip
    pixels = len(raw) // 3
    hits = sum(
        1 for i in range(0, len(raw), 3) if raw[i] > 220 and raw[i + 1] > 180 and raw[i + 2] < 80
    )
    return hits / pixels


def test_render_fixture(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    _make_fixture(media_root)
    edl = parse_edl(
        {
            "source": {"path": "src/fixture.mp4", "in_s": 1.0, "out_s": 6.0},
            "hook": {"text": "This changes everything", "style": "stakes", "duration_s": 2.0},
            "words": [
                {"text": w, "start_s": 1.2 + i * 0.4, "end_s": 1.55 + i * 0.4}
                for i, w in enumerate(WORDS)
            ],
        }
    )
    started = time.monotonic()
    result = render_clip(edl, media_root, tmp_path / "out", timeout_s=600)
    elapsed = time.monotonic() - started

    probe = json.loads(
        _run(
            [
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(result.video_path),
            ]
        ).stdout
    )  # fmt: skip
    streams = {s["codec_type"]: s for s in probe["streams"]}
    video, audio = streams["video"], streams["audio"]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1080, 1920)
    assert video["r_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) == pytest.approx(edl.duration_s, abs=0.2)
    assert _integrated_lufs(result.video_path) == pytest.approx(TARGET_LUFS, abs=1.0)

    # faststart: moov atom precedes mdat.
    head = result.video_path.read_bytes()[:1_000_000]
    assert 0 <= head.find(b"moov") < head.find(b"mdat") or head.find(b"mdat") == -1

    # Hook card visible from the first frame until hook.duration_s, gone after.
    assert _hook_yellow_fraction(result.video_path, 0.0) > 0.05
    assert _hook_yellow_fraction(result.video_path, 1.7) > 0.05
    assert _hook_yellow_fraction(result.video_path, 2.2) < 0.001

    assert result.thumbnail_path.stat().st_size > 0
    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["edl"] == edl.to_props()
    print(f"render took {elapsed:.1f}s")  # noqa: T201 - visible with -s


def test_ts_rejects_invalid_props(tmp_path: Path) -> None:
    """The Remotion side refuses invalid props itself (calculateMetadata -> ajv)."""
    media_root = tmp_path / "media"
    media_root.mkdir()
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps({"source": {"path": "x.mp4", "in_s": 0, "out_s": 5}, "layout": "bogus"})
    )
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "node", str(REMOTION_CLI), "render", "src/index.ts", "Clip",
            str(tmp_path / "x.mp4"), f"--props={props}", f"--public-dir={media_root}",
        ],
        cwd=RENDER_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )  # fmt: skip
    assert proc.returncode != 0
    assert "invalid clip props" in proc.stdout + proc.stderr
    assert not (tmp_path / "x.mp4").exists()


def _mean_rgb(path: Path, at_s: float) -> tuple[float, float, float]:
    raw = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(path), "-frames:v", "1",
            "-vf", "scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout  # fmt: skip
    return raw[0], raw[1], raw[2]


def test_render_face_track(tmp_path: Path) -> None:
    """The 9:16 window follows the crop keys and jumps at a cut: a red left half, then (after
    the keys jump at 1.5 s) the blue right half fills the whole frame."""
    media_root = tmp_path / "media"
    (media_root / "src").mkdir(parents=True)
    _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=1280x720:r=30:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-vf", "drawbox=x=640:y=0:w=640:h=720:color=blue:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(media_root / "src" / "halves.mp4"),
        ]
    )  # fmt: skip
    crop = [(0.0, 0.25), (1.5, 0.25), (1.5, 0.75), (2.9, 0.75)]
    edl = parse_edl(
        {
            "source": {"path": "src/halves.mp4", "in_s": 0.0, "out_s": 3.0},
            "layout": "face_track",
            "crop": [{"t_s": t, "x_center": x} for t, x in crop],
            "progress_bar": False,
        }
    )
    result = render_clip(edl, media_root, tmp_path / "out", timeout_s=600)

    red, blue = _mean_rgb(result.video_path, 0.5), _mean_rgb(result.video_path, 2.5)
    assert red[0] > 200 and red[2] < 40  # all red: no blue, no black bars
    assert blue[2] > 200 and blue[0] < 40
