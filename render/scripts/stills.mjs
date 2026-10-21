#!/usr/bin/env node
// Render still snapshots of the Clip composition at the hook, mid-caption and the end.
//
//   node scripts/stills.mjs [--out out/stills] [--props props.json --public-dir media/]
//
// Without --props, a 6 s fixture (ffmpeg testsrc2 + sine) and matching props are generated
// in a temp dir. Snapshot comparison is manual/optional; the PNGs are review artifacts.
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { bundle } from "@remotion/bundler";
import { renderStill, selectComposition } from "@remotion/renderer";

const root = resolve(import.meta.dirname, "..");
const { values } = parseArgs({
  options: {
    out: { type: "string", default: join(root, "out", "stills") },
    props: { type: "string" },
    "public-dir": { type: "string" },
  },
});

const FIXTURE_PROPS = {
  schema_version: 1,
  source: { path: "source.mp4", in_s: 0.5, out_s: 5.5 },
  layout: "blurred_fill",
  hook: { text: "He bet everything on one hand", style: "stakes", duration_s: 2 },
  words: [
    ["I", 0.6, 0.8], ["put", 0.8, 1.1], ["it", 1.1, 1.3], ["all", 1.3, 1.7],
    ["in.", 1.7, 2.2], ["Every", 2.9, 3.2], ["last", 3.2, 3.5], ["chip.", 3.5, 4.2],
  ].map(([text, start_s, end_s]) => ({ text, start_s, end_s })),
  captions_enabled: true,
  caption_style: "bold_pop",
  progress_bar: true,
};

let tempDir = null;
let props;
let publicDir;
if (values.props) {
  props = JSON.parse(readFileSync(values.props, "utf8"));
  publicDir = resolve(values["public-dir"] ?? ".");
} else {
  tempDir = mkdtempSync(join(tmpdir(), "clipper-stills-"));
  publicDir = tempDir;
  execFileSync("ffmpeg", [
    "-v", "error", "-y",
    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    join(tempDir, "source.mp4"),
  ]);
  props = FIXTURE_PROPS;
}

try {
  const serveUrl = await bundle({ entryPoint: join(root, "src", "index.ts"), publicDir });
  const composition = await selectComposition({ serveUrl, id: "Clip", inputProps: props });
  const fps = composition.fps;
  const last = composition.durationInFrames - 1;
  const hookFrame = props.hook ? Math.floor((props.hook.duration_s * fps) / 2) : 0;
  // First word spoken after the hook card is gone, so the caption is unobstructed.
  const hookEnd = props.hook ? props.hook.duration_s : 0;
  const firstWord = (props.words ?? []).find((w) => w.start_s - props.source.in_s >= hookEnd);
  const captionFrame = firstWord
    ? Math.min(last, Math.round((firstWord.start_s - props.source.in_s) * fps) + 6)
    : Math.floor(last / 2);
  const shots = { hook: hookFrame, "mid-caption": captionFrame, end: last };

  mkdirSync(values.out, { recursive: true });
  for (const [name, frame] of Object.entries(shots)) {
    const output = join(values.out, `${name}.png`);
    await renderStill({ composition, serveUrl, inputProps: props, frame, output });
    console.log(`${name}: frame ${frame} -> ${output}`);
  }
  writeFileSync(join(values.out, "props.json"), JSON.stringify(props, null, 2) + "\n");
} finally {
  if (tempDir) rmSync(tempDir, { recursive: true, force: true });
}
