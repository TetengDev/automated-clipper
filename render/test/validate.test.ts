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
      schema_version: 1,
      source: { path: "source.mp4", in_s: 0, out_s: 5 },
      layout: "blurred_fill",
      hook: null,
      words: [],
      captions_enabled: true,
      caption_style: "bold_pop",
      progress_bar: true,
    });
    expect(raw).toEqual({ source: { path: "source.mp4", in_s: 0, out_s: 5 } });
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
});
