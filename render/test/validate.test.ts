// Schema parity: the same fixtures are validated by pydantic in tests/test_edl.py.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { durationInFrames, EdlValidationError, parseEdl } from "../src/validate";

const FIXTURES = join(import.meta.dirname, "fixtures");
const load = (kind: "valid" | "invalid") =>
  readdirSync(join(FIXTURES, kind))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => [f, JSON.parse(readFileSync(join(FIXTURES, kind, f), "utf8"))] as const);

describe("parseEdl", () => {
  it("has fixtures", () => {
    expect(load("valid").length).toBeGreaterThan(0);
    expect(load("invalid").length).toBeGreaterThan(0);
  });

  it.each(load("valid"))("accepts valid/%s", (_name, props) => {
    expect(() => parseEdl(props)).not.toThrow();
  });

  it.each(load("invalid"))("rejects invalid/%s", (_name, props) => {
    expect(() => parseEdl(props)).toThrow(EdlValidationError);
  });

  it("applies schema defaults without mutating the input", () => {
    const raw = { source: { path: "source.mp4", in_s: 0, out_s: 5 } };
    const edl = parseEdl(raw);
    expect(edl).toEqual({
      schema_version: 2,
      source: { path: "source.mp4", in_s: 0, out_s: 5 },
      layout: "blurred_fill",
      hook: null,
      words: [],
      captions_enabled: true,
      caption_style: "bold_pop",
      progress_bar: true,
      crop: null,
      segments: null,
      cuts: [],
    });
    expect(raw).toEqual({ source: { path: "source.mp4", in_s: 0, out_s: 5 } });
  });

  it("caps crop keys at 2000", () => {
    const keys = (n: number) => Array.from({ length: n }, (_, i) => ({ t_s: i / 100, x_center: 0.5 }));
    const props = (n: number) => ({
      source: { path: "a.mp4", in_s: 0, out_s: 30 },
      layout: "face_track",
      crop: keys(n),
    });
    expect(parseEdl(props(2000)).crop).toHaveLength(2000);
    expect(() => parseEdl(props(2001))).toThrow(EdlValidationError);
  });

  it("names the crop rule that failed", () => {
    expect(() =>
      parseEdl({ source: { path: "a.mp4", in_s: 0, out_s: 5 }, layout: "face_track" }),
    ).toThrow(/crop is required when layout is face_track/);
  });

  it("applies hook defaults", () => {
    const edl = parseEdl({
      source: { path: "a.mp4", in_s: 0, out_s: 10 },
      hook: { text: "Wait for it" },
    });
    expect(edl.hook).toEqual({ text: "Wait for it", style: "stakes", duration_s: 2 });
  });

  it("reports every problem", () => {
    try {
      parseEdl({ source: { path: "/abs.mp4", in_s: -1, out_s: 5 }, layout: "nope" });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(EdlValidationError);
      expect((err as EdlValidationError).errors.length).toBeGreaterThanOrEqual(3);
      // One line, error paths and messages only; never the offending values.
      const { message } = err as Error;
      expect(message).toMatch(/^invalid clip props: \/layout: .+; \/source\/in_s: must be >= 0; /);
      expect(message).not.toContain("\n");
      expect(message).not.toContain("abs.mp4");
      expect(message).not.toContain("nope");
    }
  });

  it.each([Infinity, -Infinity, NaN])("rejects non-finite number %s", (bad) => {
    const base = () => ({
      source: { path: "a.mp4", in_s: 0, out_s: 5 },
      hook: { text: "x", duration_s: 2 },
      words: [{ text: "w", start_s: 0.5, end_s: 1 }],
    });
    const cases = [
      (p: ReturnType<typeof base>) => (p.source.in_s = bad),
      (p: ReturnType<typeof base>) => (p.source.out_s = bad),
      (p: ReturnType<typeof base>) => (p.hook.duration_s = bad),
      (p: ReturnType<typeof base>) => (p.words[0]!.start_s = bad),
      (p: ReturnType<typeof base>) => (p.words[0]!.end_s = bad),
    ];
    for (const mutate of cases) {
      const props = base();
      mutate(props);
      expect(() => parseEdl(props)).toThrow(EdlValidationError);
    }
  });

  it("computes duration in frames (round half up)", () => {
    const edl = parseEdl({ source: { path: "a.mp4", in_s: 12.5, out_s: 42.5 } });
    expect(durationInFrames(edl, 30)).toBe(900);
    const odd = parseEdl({ source: { path: "a.mp4", in_s: 0, out_s: 1.05 } });
    expect(durationInFrames(odd, 30)).toBe(32);
  });

  it("sums whole-frame segments for an EDL v2", () => {
    const edl = parseEdl({
      source: { path: "a.mp4", in_s: 1, out_s: 6 },
      segments: [
        { in_s: 1, out_s: 3 },
        { in_s: 126 / 30, out_s: 6 },
      ],
    });
    expect(durationInFrames(edl, 30)).toBe(60 + 54);
  });

  it("keeps v1 EDLs (one range) valid", () => {
    const edl = parseEdl({ schema_version: 1, source: { path: "a.mp4", in_s: 0.25, out_s: 5 } });
    expect(edl.schema_version).toBe(1);
    expect(durationInFrames(edl, 30)).toBe(143);
  });

  it.each([
    [[{ in_s: 0, out_s: 2 }, { in_s: 1.5, out_s: 5 }], /sorted and must not overlap/],
    [[{ in_s: 0, out_s: 2 }, { in_s: 3, out_s: 3.2 }], /at least 0.3/],
    [[{ in_s: 0, out_s: 2 }, { in_s: 3.01, out_s: 5 }], /frame grid/],
    [[{ in_s: 0, out_s: 0.4 }, { in_s: 1, out_s: 1.4 }], /between 1 and 180/],
  ])("names the segment rule that failed (%#)", (segments, message) => {
    const source = { path: "a.mp4", in_s: segments[0]!.in_s, out_s: segments.at(-1)!.out_s };
    expect(() => parseEdl({ source, segments })).toThrow(message);
  });

  it("rejects segments that do not span the source", () => {
    expect(() =>
      parseEdl({
        source: { path: "a.mp4", in_s: 0, out_s: 6 },
        segments: [{ in_s: 0, out_s: 2 }, { in_s: 3, out_s: 5 }],
      }),
    ).toThrow(/span source/);
  });
});
