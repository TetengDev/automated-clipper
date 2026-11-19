"""Rebuild the Random Gaming channel banner (../banner.png, 2560x1440).

The banner is a tilted yellow die and the one-line "RANDOM GAMING" wordmark, with the tagline
in a dark cyan-bordered pill below. All of it sits inside the YouTube safe area (the centre
1546x423 that every device shows). The background (pip-grid wallpaper, ghost dice, neon
streaks, pink and cyan glows) fills the TV and desktop crops.

Pipeline: text is measured with headless Chrome canvas -> SVG in HTML -> headless Chrome
screenshot -> ffmpeg crop to 2560x1440 RGB -> an sRGB chunk is added. Before the banner is
written, the content layer is rendered alone on a chroma-green page and its ink box (strokes,
drop and die extrusion included, glow excluded) must sit inside the safe area, or the script
exits with an error. Needs Google Chrome and ffmpeg (macOS paths).

Run: python3 assets/brand/random_gaming_yt/source/banner.py
"""

import json
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "banner.png"
FONTS = HERE / "fonts"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

YEL, INK, RED, PINK, CYAN = "#FFD400", "#111111", "#E5002B", "#FF2D87", "#22E4FF"
PURPLE_HI, PURPLE_MID, PURPLE_LO = "#5a1fb0", "#2a0d66", "#14062f"
WHITE = "#FFFFFF"

W, H = 2560, 1440
CX, CY = W / 2, H / 2
# YouTube safe area (all devices): centre 1546x423 -> x 507..2053, y 508.5..931.5.
SAFE_W, SAFE_H = 1546, 423
SAFE_X0, SAFE_Y0 = (W - SAFE_W) // 2, (H - SAFE_H) // 2  # 507, 508
SAFE = (SAFE_X0, SAFE_Y0 + 1, SAFE_X0 + SAFE_W, SAFE_Y0 + SAFE_H)  # inner pixel box

TAGLINE = ["GAME NEWS", "STREAMER MOMENTS", "NEW SHORTS DAILY"]
PILL_SIZE, PILL_LS, PILL_PAD = 60, 1, (24, 40)  # 60px -> 15.1px at 390px phone width

FONT_CSS = f"""
@font-face {{ font-family: Anton; src: url('{(FONTS / "Anton-Regular.ttf").as_uri()}'); }}
html, body {{ margin: 0; padding: 0; width: {W}px; height: {H + 200}px; overflow: hidden;
  background: #05030a; }}
svg {{ display: block; }}
"""

DEFS = f"""
<defs>
  <radialGradient id="bg" gradientUnits="userSpaceOnUse" cx="{CX}" cy="{CY}" r="1500">
    <stop offset="0%" stop-color="{PURPLE_HI}"/>
    <stop offset="42%" stop-color="{PURPLE_MID}"/>
    <stop offset="100%" stop-color="{PURPLE_LO}"/>
  </radialGradient>
  <radialGradient id="fade" gradientUnits="userSpaceOnUse" cx="{CX}" cy="{CY}" r="1250"
                  gradientTransform="translate({CX} {CY}) scale(1 0.62) translate({-CX} {-CY})">
    <stop offset="0%" stop-color="#000"/>
    <stop offset="52%" stop-color="#000"/>
    <stop offset="88%" stop-color="#fff"/>
  </radialGradient>
  <mask id="edges"><rect width="{W}" height="{H}" fill="url(#fade)"/></mask>
  <linearGradient id="streakL" x1="0" x2="1" y1="0" y2="0">
    <stop offset="0%" stop-color="#fff" stop-opacity="0"/>
    <stop offset="100%" stop-color="#fff" stop-opacity="1"/>
  </linearGradient>
  <linearGradient id="streakR" x1="1" x2="0" y1="0" y2="0">
    <stop offset="0%" stop-color="#fff" stop-opacity="0"/>
    <stop offset="100%" stop-color="#fff" stop-opacity="1"/>
  </linearGradient>
  <pattern id="pipgrid" width="200" height="200" patternUnits="userSpaceOnUse"
           patternTransform="rotate(-14)">
    <rect x="34" y="34" width="132" height="132" rx="28" fill="none" stroke="#fff"
          stroke-width="5"/>
    <circle cx="70" cy="70" r="11" fill="#fff"/><circle cx="130" cy="70" r="11" fill="#fff"/>
    <circle cx="100" cy="100" r="11" fill="#fff"/>
    <circle cx="70" cy="130" r="11" fill="#fff"/><circle cx="130" cy="130" r="11" fill="#fff"/>
  </pattern>
  <filter id="blur" x="-50%" y="-50%" width="200%" height="200%">
    <feGaussianBlur stdDeviation="110"/>
  </filter>
  <filter id="glowcyan" x="-30%" y="-30%" width="160%" height="160%">
    <feGaussianBlur in="SourceAlpha" stdDeviation="24" result="b"/>
    <feFlood flood-color="{CYAN}" flood-opacity="0.85"/>
    <feComposite in2="b" operator="in" result="g"/>
    <feMerge><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <filter id="glowpink" x="-10%" y="-40%" width="120%" height="180%">
    <feGaussianBlur in="SourceAlpha" stdDeviation="18" result="b"/>
    <feFlood flood-color="{PINK}" flood-opacity="0.75"/>
    <feComposite in2="b" operator="in" result="g"/>
    <feMerge><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
</defs>
"""

PIPS = {
    "5": [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)],
    "3": [(-1, -1), (0, 0), (1, 1)],
    "2": [(1, -1), (-1, 1)],
    "1": [(0, 0)],
}

_METRICS = {}


def chrome(*args):
    return subprocess.run(  # noqa: S603 - fixed argv list, no shell
        [CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=3000", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def measure(tmp, items):
    """Return {(text, size, letter_spacing): (advance, cap_ascent)} from Chrome canvas.

    measureText runs on the real Anton face, so the layout matches the render exactly.
    """
    todo = [i for i in items if i not in _METRICS]
    if todo:
        html = tmp / "measure.html"
        html.write_text(f"""<!doctype html><html><head><meta charset='utf-8'>
<style>{FONT_CSS}</style></head><body><pre id='out'></pre><script>
document.fonts.load('100px Anton').then(() => {{
  const c = document.createElement('canvas').getContext('2d');
  const res = {json.dumps(todo)}.map(([t, s, ls]) => {{
    c.font = s + 'px Anton'; c.letterSpacing = ls + 'px';
    const m = c.measureText(t);
    return [m.width, m.actualBoundingBoxAscent];
  }});
  document.getElementById('out').textContent = 'M=' + JSON.stringify(res);
}});
</script></body></html>""")
        dom = chrome("--dump-dom", html.as_uri())
        res = json.loads(re.search(r"M=(\[.*?\]\])", dom).group(1))
        for k, v in zip(todo, res, strict=True):
            _METRICS[k] = tuple(v)
    return {i: _METRICS[i] for i in items}


class Text:
    """Anton metrics, measured once per (text, size, spacing) through a temp dir."""

    def __init__(self, tmp):
        self.tmp = tmp

    def adv(self, text, size, ls=0):
        return measure(self.tmp, [(text, size, ls)])[(text, size, ls)][0]

    def cap(self, size):
        return measure(self.tmp, [("H", size, 0)])[("H", size, 0)][1]


def pips(face, cx, cy, half, r, fill):
    return "".join(
        f'<circle cx="{cx + dx * half:.1f}" cy="{cy + dy * half:.1f}" r="{r:.1f}" fill="{fill}"/>'
        for dx, dy in PIPS[face]
    )


def die(cx, cy, s, rot=-14, glow=True):
    """The icon die (yellow face, ink pips, pink extrusion, cyan halo) scaled to side s."""
    k = s / 430
    sw, rx = 22 * k, 84 * k
    back = (
        f'<rect x="{cx - s / 2 + 26 * k:.1f}" y="{cy - s / 2 + 34 * k:.1f}" width="{s}" '
        f'height="{s}" rx="{rx:.1f}" fill="{PINK}" stroke="{INK}" stroke-width="{sw:.1f}"/>'
    )
    if glow:
        back = f'<g filter="url(#glowcyan)">{back}</g>'
    fx, fy = cx - 10 * k, cy - 14 * k
    return f"""
<g transform="rotate({rot} {cx} {cy})">
  {back}
  <rect x="{fx - s / 2:.1f}" y="{fy - s / 2:.1f}" width="{s}" height="{s}" rx="{rx:.1f}"
        fill="{YEL}" stroke="{INK}" stroke-width="{sw:.1f}"/>
  {pips("5", fx, fy, 122 * k, 54 * k, INK)}
</g>"""


def ghost_die(cx, cy, s, rot, color, opacity, face="5"):
    """Background-only outline die (TV and desktop fill, never content)."""
    k = s / 430
    return f"""
<g transform="rotate({rot} {cx} {cy})" opacity="{opacity}">
  <rect x="{cx - s / 2}" y="{cy - s / 2}" width="{s}" height="{s}" rx="{84 * k:.1f}"
        fill="none" stroke="{color}" stroke-width="{max(6, 18 * k):.1f}"/>
  {pips(face, cx, cy, 122 * k, 46 * k, color)}
</g>"""


def big_word(txt, x, y, size):
    """Thumbnail-style word (icon treatment): red hard drop, ink stroke, yellow fill."""
    stroke, drop = size * 0.075, size * 0.06
    common = (
        f'x="{x:.1f}" y="{y:.1f}" font-family="Anton" font-size="{size}" '
        f'text-anchor="start" letter-spacing="0"'
    )
    return f"""
  <text {common} transform="translate(0,{drop:.1f})" fill="{RED}" stroke="{RED}"
        stroke-width="{stroke:.1f}" stroke-linejoin="round">{txt}</text>
  <text {common} fill="{YEL}" stroke="{INK}" stroke-width="{stroke:.1f}"
        stroke-linejoin="round" paint-order="stroke fill">{txt}</text>"""


def tag_pill(tx, cx, py, glow=True):
    """Tagline pill: ink rounded pill, cyan border, white Anton, yellow pip separators.

    Separators are drawn circles, not a bullet glyph, so there is no font fallback.
    Centred on cx, top at py.
    """
    size, ls = PILL_SIZE, PILL_LS
    pad_y, pad_x = PILL_PAD
    gap, r = size * 0.42, size * 0.10
    tcap = tx.cap(size)
    pill_h = tcap + 2 * pad_y
    tw = sum(tx.adv(t, size, ls) - ls for t in TAGLINE) + (len(TAGLINE) - 1) * (2 * gap + 2 * r)
    pw = tw + 2 * pad_x
    base = py + pad_y + tcap
    out, x = [], cx - tw / 2
    for i, t in enumerate(TAGLINE):
        if i:
            x += gap
            out.append(
                f'<circle cx="{x + r:.1f}" cy="{base - tcap / 2:.1f}" r="{r:.1f}" fill="{YEL}"/>'
            )
            x += 2 * r + gap
        out.append(
            f'<text x="{x:.1f}" y="{base:.1f}" font-family="Anton" font-size="{size}" '
            f'letter-spacing="{ls}" fill="{WHITE}">{t}</text>'
        )
        x += tx.adv(t, size, ls) - ls  # trailing letter-spacing is not ink
    return f"""
<g {'filter="url(#glowpink)"' if glow else ""}>
  <rect x="{cx - pw / 2:.1f}" y="{py:.1f}" width="{pw:.1f}" height="{pill_h:.1f}"
        rx="{pill_h / 2:.1f}" fill="{INK}" stroke="{CYAN}" stroke-width="6"/>
</g>
{"".join(out)}"""


def background():
    # Pip-grid wallpaper fading out over the safe area, pink/cyan glows, ghost dice in the
    # TV-only zones and at the desktop band ends, neon motion streaks into the band.
    streaks = []
    for y, c, w, o in (
        (612, CYAN, 6, 0.55),
        (650, PINK, 10, 0.7),
        (790, PINK, 6, 0.45),
        (828, CYAN, 10, 0.6),
    ):
        streaks.append(
            f'<rect x="40" y="{y}" width="420" height="{w}" rx="{w / 2}" fill="{c}" '
            f'opacity="{o}" mask="url(#sl)"/>'
        )
        streaks.append(
            f'<rect x="{W - 460}" y="{y + 18}" width="420" height="{w}" rx="{w / 2}" '
            f'fill="{c}" opacity="{o}" mask="url(#sr)"/>'
        )
    glows = "".join(
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}" opacity="{o}" filter="url(#blur)"/>'
        for x, y, r, c, o in (
            (260, 1180, 420, PINK, 0.55),
            (2330, 260, 420, CYAN, 0.40),
            (CX, CY, 700, "#7a2fe0", 0.45),
            (2400, 1300, 300, PINK, 0.35),
            (150, 160, 300, CYAN, 0.30),
        )
    )
    return f"""
<defs>
  <mask id="sl"><rect x="40" y="0" width="420" height="{H}" fill="url(#streakL)"/></mask>
  <mask id="sr"><rect x="{W - 460}" y="0" width="420" height="{H}" fill="url(#streakR)"/></mask>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/>
{glows}
<rect width="{W}" height="{H}" fill="url(#pipgrid)" opacity="0.10" mask="url(#edges)"/>
{ghost_die(250, 300, 300, 18, CYAN, 0.45)}
{ghost_die(2320, 1160, 340, -20, PINK, 0.5, "3")}
{ghost_die(700, 1220, 170, -8, YEL, 0.30, "2")}
{ghost_die(1900, 230, 190, 12, YEL, 0.28, "1")}
{ghost_die(1350, 1300, 130, 25, CYAN, 0.25, "3")}
{ghost_die(1180, 140, 120, -18, PINK, 0.30, "2")}
{ghost_die(235, 720, 210, -16, PINK, 0.32, "3")}
{ghost_die(2330, 720, 210, 14, CYAN, 0.32, "2")}
{"".join(streaks)}"""


def content(tx, glow=True):
    # Die left + one-line wordmark as one lockup; the tagline pill (~1393px wide) sits on its
    # own row under the whole lockup, centred on it. Lockup and pill share the safe height.
    s = 188  # die side -> rotated ink ~245px (largest element)
    die_half = s * 0.655  # half the rotated ink width incl. extrusion
    gap = 36
    nsize = 186
    name = "RANDOM GAMING"
    row_w = 2 * die_half + gap + tx.adv(name, nsize)
    x0 = CX - row_w / 2
    pill_h = tx.cap(PILL_SIZE) + 2 * PILL_PAD[0]
    row_h = 2 * die_half
    g = 26
    top = CY - (row_h + g + pill_h) / 2 - 5  # optical: balance the drop/glow-free ink box
    row_cy = top + row_h / 2
    nb = row_cy + (tx.cap(nsize) - nsize * 0.06) / 2  # centre wordmark ink (cap + drop) on die
    return f"""
{die(x0 + die_half, row_cy, s, glow=glow)}
{big_word(name, x0 + 2 * die_half + gap, nb, nsize)}
{tag_pill(tx, CX, top + row_h + g, glow)}"""


def page(body, bg="#05030a"):
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{FONT_CSS}"
        f"body{{background:{bg}}}</style></head><body><svg width='{W}' height='{H}' "
        f"viewBox='0 0 {W} {H}' xmlns='http://www.w3.org/2000/svg'>{DEFS}{body}</svg>"
        f"</body></html>"
    )


def shoot(tmp, html, png):
    raw = tmp / "raw.png"
    chrome(
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--force-color-profile=srgb",
        f"--window-size={W},{H + 200}",
        f"--screenshot={raw}",
        html.as_uri(),
    )
    # Headless Chrome's viewport is shorter than the window: shoot tall, crop to size.
    subprocess.run(  # noqa: S603 - fixed argv list, no shell
        ["ffmpeg", "-v", "error", "-y", "-i", str(raw)]  # noqa: S607
        + ["-vf", f"crop={W}:{H}:0:0", "-pix_fmt", "rgb24", "-compression_level", "9"]
        + [str(png)],
        check=True,
    )
    tag_srgb(png)


def tag_srgb(png_path):
    """Insert an sRGB chunk (perceptual intent) right after IHDR."""
    b = png_path.read_bytes()
    if b"sRGB" in b[:64]:
        return
    ihdr_end = 8 + 4 + 4 + 13 + 4
    body = b"sRGB\x00"
    chunk = struct.pack(">I", 1) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    png_path.write_bytes(b[:ihdr_end] + chunk + b[ihdr_end:])


def ink_bounds(png):
    """Bounding box of every pixel that is not the chroma-green page (tolerance 48)."""
    raw = subprocess.run(  # noqa: S603 - fixed argv list, no shell
        ["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],  # noqa: S607
        check=True,
        capture_output=True,
    ).stdout
    green_row = bytes((0, 255, 0)) * W
    x0, y0, x1, y1 = W, H, -1, -1
    for y in range(H):
        row = raw[y * W * 3 : (y + 1) * W * 3]
        if row == green_row:
            continue
        xs = [x for x in range(W) if row[3 * x] + abs(row[3 * x + 1] - 255) + row[3 * x + 2] > 48]
        if xs:
            x0, x1 = min(x0, xs[0]), max(x1, xs[-1])
            y0, y1 = min(y0, y), max(y1, y)
    return x0, y0, x1, y1


def check_safe(tmp, tx):
    """Render the content alone (no glow) on chroma green; exit if its ink leaves the safe area."""
    html, png = tmp / "ink.html", tmp / "ink.png"
    green = f'<rect width="{W}" height="{H}" fill="#00ff00"/>'
    html.write_text(page(green + content(tx, glow=False), bg="#00ff00"))
    shoot(tmp, html, png)
    b = ink_bounds(png)
    ok = b[0] >= SAFE[0] and b[1] >= SAFE[1] and b[2] < SAFE[2] and b[3] < SAFE[3]
    margins = (b[0] - SAFE[0], b[1] - SAFE[1], SAFE[2] - 1 - b[2], SAFE[3] - 1 - b[3])
    print(
        f"content ink x{b[0]}..{b[2]} y{b[1]}..{b[3]}  "
        f"margins L{margins[0]} T{margins[1]} R{margins[2]} B{margins[3]}"
    )
    if not ok:
        sys.exit(f"content outside the {SAFE_W}x{SAFE_H} safe area; {OUT} not written")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        tx = Text(tmp)
        check_safe(tmp, tx)
        html = tmp / "banner.html"
        html.write_text(page(background() + content(tx)))
        shoot(tmp, html, OUT)
    print(OUT)


if __name__ == "__main__":
    main()
