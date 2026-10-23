import { describe, expect, it } from "vitest";
import {
  frameSegments,
  outputWords,
  segmentsOf,
  toOutputTime,
} from "../src/timeline";
import type { Edl } from "../src/types";

const FPS = 30;
const FRAME = 1 / FPS;

const edl = (over: Partial<Edl>): Edl => ({
  schema_version: 2,
  source: { path: "a.mp4", in_s: 0, out_s: 6 },
  layout: "blurred_fill",
  hook: null,
  words: [],
  captions_enabled: true,
  caption_style: "bold_pop",
  progress_bar: true,
  crop: null,
  segments: null,
  cuts: [],
  ...over,
});

describe("segmentsOf", () => {
  it("migrates a v1 range (or a v2 without segments) to one segment", () => {
    expect(segmentsOf(edl({ schema_version: 1 }))).toEqual([{ in_s: 0, out_s: 6 }]);
    expect(segmentsOf(edl({}))).toEqual([{ in_s: 0, out_s: 6 }]);
  });

  it("returns the v2 segments", () => {
    const segments = [
      { in_s: 0, out_s: 2 },
      { in_s: 3, out_s: 6 },
    ];
    expect(segmentsOf(edl({ segments }))).toBe(segments);
  });
});

describe("frameSegments", () => {
  it("trims each segment to whole frames and places it on the output timeline", () => {
    const segs = frameSegments(
      [
        { in_s: 1, out_s: 3 },
        { in_s: 126 / 30, out_s: 6 },
      ],
      FPS,
    );
    expect(segs).toEqual([
      { trimBefore: 30, durationInFrames: 60, from: 0 },
      { trimBefore: 126, durationInFrames: 54, from: 60 },
    ]);
  });

  it("rounds the start and the length half up, like v1 and the Python side", () => {
    expect(frameSegments([{ in_s: 0.25, out_s: 5 }], FPS)).toEqual([
      { trimBefore: 8, durationInFrames: 143, from: 0 },
    ]);
  });
});

describe("toOutputTime", () => {
  const segs = frameSegments(
    [
      { in_s: 0, out_s: 2 },
      { in_s: 3.2, out_s: 6 },
    ],
    FPS,
  );

  it("is the identity before the first cut", () => {
    expect(toOutputTime(1.5, segs, FPS)).toBeCloseTo(1.5, 9);
  });

  it("shifts later times back by the cut", () => {
    expect(toOutputTime(4, segs, FPS)).toBeCloseTo(2.8, 9);
  });

  it("maps a time inside a cut onto the join", () => {
    expect(toOutputTime(2.5, segs, FPS)).toBeCloseTo(2, 9);
  });

  it("clamps to the output range", () => {
    expect(toOutputTime(-1, segs, FPS)).toBe(0);
    expect(toOutputTime(9, segs, FPS)).toBeCloseTo(4.8, 9);
  });
});

describe("outputWords", () => {
  it("moves words onto the output timeline and drops the ones inside a cut", () => {
    // 1.2 s of silence after "see" (2.0 -> 3.2) cut down to a 0.14 s breath on frames.
    const segs = frameSegments(
      [
        { in_s: 0, out_s: 62 / 30 },
        { in_s: 94 / 30, out_s: 5.2 },
      ],
      FPS,
    );
    const words = [
      { text: "see", start_s: 1.5, end_s: 2.0 },
      { text: "uh", start_s: 2.3, end_s: 2.5 }, // inside the cut
      { text: "that", start_s: 3.2, end_s: 3.6 },
      { text: "all", start_s: 4.7, end_s: 5.0 },
    ];
    const out = outputWords(words, segs, FPS);
    expect(out.map((w) => w.text)).toEqual(["see", "that", "all"]);
    const removed = 94 / 30 - 62 / 30;
    expect(removed).toBeCloseTo(1.2 - 0.14, 1);
    // AC: captions stay in sync within one frame of the cut video.
    expect(Math.abs(out[1]!.start_s - (3.2 - removed))).toBeLessThan(FRAME);
    expect(Math.abs(out[2]!.end_s - (5.0 - removed))).toBeLessThan(FRAME);
    expect(out[0]).toEqual({ text: "see", start_s: 1.5, end_s: 2.0 });
  });

  it("clamps a word across a join to the join", () => {
    const segs = frameSegments(
      [
        { in_s: 0, out_s: 2 },
        { in_s: 3, out_s: 5 },
      ],
      FPS,
    );
    const [word] = outputWords([{ text: "long", start_s: 1.5, end_s: 2.4 }], segs, FPS);
    expect(word).toEqual({ text: "long", start_s: 1.5, end_s: 2 });
  });

  it("is a plain copy with one segment from 0", () => {
    const words = [{ text: "hi", start_s: 0.2, end_s: 0.5 }];
    expect(outputWords(words, frameSegments([{ in_s: 0, out_s: 5 }], FPS), FPS)).toEqual(words);
  });
});
