"""End-to-end render smoke test, meant to run inside the built image.

    docker run --rm --read-only --tmpfs /tmp --user 10001:10001 \\
      -v "$PWD/scripts:/smoke:ro" --entrypoint python clipper:ci /smoke/render_smoke.py

Generates a 5 s lavfi fixture in a temp dir, renders it through render_clip and checks the
output with ffprobe. Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from clipper.render import render_clip


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clipper-smoke-") as tmp:
        root = Path(tmp)
        media = root / "media"
        media.mkdir()
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(media / "src.mp4"),
            ],
            check=True,
            timeout=120,
        )  # fmt: skip
        words = [
            {"text": w, "start_s": 0.6 + i * 0.4, "end_s": 0.95 + i * 0.4}
            for i, w in enumerate(["smoke", "test", "render", "works", "fine", "🔥"])
        ]
        edl = {
            "source": {"path": "src.mp4", "in_s": 0.5, "out_s": 5.5},
            "hook": {"text": "Smoke test", "style": "stakes", "duration_s": 1.5},
            "words": words,
        }
        started = time.monotonic()
        result = render_clip(edl, media, root / "out", timeout_s=300)
        elapsed = time.monotonic() - started
        probe = json.loads(
            subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "ffprobe", "-v", "error", "-show_streams", "-show_format",
                    "-of", "json", str(result.video_path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
        )  # fmt: skip
        streams = {s["codec_type"]: s for s in probe["streams"]}
        video, audio = streams["video"], streams["audio"]
        duration = float(probe["format"]["duration"])
        checks = {
            "video h264": video["codec_name"] == "h264",
            "1080x1920": (video["width"], video["height"]) == (1080, 1920),
            "30 fps": video["r_frame_rate"] == "30/1",
            "audio aac": audio["codec_name"] == "aac",
            "duration 5.0±0.2": abs(duration - 5.0) <= 0.2,
            "thumbnail": result.thumbnail_path.stat().st_size > 0,
            "sidecar": json.loads(result.sidecar_path.read_text())["duration_s"] == 5.0,
        }
        for name, ok in checks.items():
            print(f"{'ok  ' if ok else 'FAIL'} {name}")  # noqa: T201
        print(f"render took {elapsed:.1f}s, duration {duration:.3f}s")  # noqa: T201
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
