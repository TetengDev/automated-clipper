"""Rebuild the Random Gaming channel icon (../icon.png, 800x800).

The icon is a tilted yellow die showing 5, with a hot-pink extrusion and a cyan ring on a
neon purple disc. Pipeline: SVG in HTML -> headless Chrome screenshot -> ffmpeg crop to
800x800 RGB -> an sRGB chunk is added. Needs Google Chrome and ffmpeg (macOS paths).

Run: python3 assets/brand/random_gaming_yt/source/make.py
"""

import pathlib
import struct
import subprocess
import tempfile
import zlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "icon.png"
FONTS = HERE / "fonts"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

YEL, INK, PINK, CYAN = "#FFD400", "#111111", "#FF2D87", "#22E4FF"
SIZE = 800

FONT_CSS = f"""
@font-face {{ font-family: Anton; src: url('{(FONTS / "Anton-Regular.ttf").as_uri()}'); }}
html, body {{ margin: 0; padding: 0; width: {SIZE}px; height: {SIZE + 200}px;
  overflow: hidden; background: #05030a; }}
svg {{ display: block; }}
"""

DEFS = f"""
<defs>
  <radialGradient id="bg" cx="50%" cy="44%" r="62%">
    <stop offset="0%" stop-color="#5a1fb0"/>
    <stop offset="55%" stop-color="#2a0d66"/>
    <stop offset="100%" stop-color="#14062f"/>
  </radialGradient>
  <filter id="glowcyan" x="-30%" y="-30%" width="160%" height="160%">
    <feGaussianBlur in="SourceAlpha" stdDeviation="28" result="b"/>
    <feFlood flood-color="{CYAN}" flood-opacity="0.85"/>
    <feComposite in2="b" operator="in" result="g"/>
    <feMerge><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
</defs>
"""

FIVE = [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)]


def pips(cx, cy, half, r, fill):
    return "".join(
        f'<circle cx="{cx + dx * half}" cy="{cy + dy * half}" r="{r}" fill="{fill}"/>'
        for dx, dy in FIVE
    )


def icon_svg():
    s, r = 430, 84
    return f"""
<rect width="{SIZE}" height="{SIZE}" fill="url(#bg)"/>
<circle cx="400" cy="400" r="372" fill="none" stroke="{CYAN}" stroke-width="30"/>
<g transform="rotate(-14 400 400)">
  <g filter="url(#glowcyan)">
    <rect x="{400 - s / 2 + 26}" y="{400 - s / 2 + 34}" width="{s}" height="{s}" rx="{r}"
          fill="{PINK}" stroke="{INK}" stroke-width="22"/>
  </g>
  <rect x="{400 - s / 2 - 10}" y="{400 - s / 2 - 14}" width="{s}" height="{s}" rx="{r}"
        fill="{YEL}" stroke="{INK}" stroke-width="22"/>
  {pips(400 - 10, 400 - 14, 122, 54, INK)}
</g>"""


def page(body):
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{FONT_CSS}</style>"
        f"</head><body><svg width='{SIZE}' height='{SIZE}' viewBox='0 0 {SIZE} {SIZE}' "
        f"xmlns='http://www.w3.org/2000/svg'>{DEFS}{body}</svg></body></html>"
    )


def tag_srgb(png_path):
    """Insert an sRGB chunk (perceptual intent) right after IHDR."""
    b = png_path.read_bytes()
    if b"sRGB" in b[:64]:
        return
    ihdr_end = 8 + 4 + 4 + 13 + 4
    body = b"sRGB\x00"
    chunk = struct.pack(">I", 1) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    png_path.write_bytes(b[:ihdr_end] + chunk + b[ihdr_end:])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        html = pathlib.Path(tmp) / "icon.html"
        raw = pathlib.Path(tmp) / "raw.png"
        html.write_text(page(icon_svg()))
        subprocess.run(  # noqa: S603 - fixed argv list, no shell
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--force-color-profile=srgb",
                "--virtual-time-budget=3000",
                f"--window-size={SIZE},{SIZE + 200}",
                f"--screenshot={raw}",
                html.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        # Headless Chrome's viewport is shorter than the window: shoot tall, crop to size.
        subprocess.run(  # noqa: S603 - fixed argv list, no shell
            ["ffmpeg", "-v", "error", "-y", "-i", str(raw)]  # noqa: S607
            + ["-vf", f"crop={SIZE}:{SIZE}:0:0", "-pix_fmt", "rgb24", str(OUT)],
            check=True,
        )
    tag_srgb(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
