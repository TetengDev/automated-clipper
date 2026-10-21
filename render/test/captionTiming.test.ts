import { describe, expect, it } from "vitest";
import {
  activeLineIndex,
  activeWordIndex,
  chunkWords,
  fitFontSize,
  frameToSeconds,
  secondsToFrame,
  toClipWords,
  type ClipWord,
} from "../src/captionTiming";

// Evenly spaced words: each 0.3 s long, no gaps.
const steady = (texts: string[], start = 0, step = 0.3): ClipWord[] =>
  texts.map((text, i) => ({ text, start: start + i * step, end: start + (i + 1) * step }));

describe("toClipWords", () => {
  const words = [
    { text: "before", start_s: 8, end_s: 9 },
    { text: "straddle", start_s: 9.8, end_s: 10.3 },
    { text: "inside", start_s: 11, end_s: 11.5 },
    { text: " ", start_s: 11.5, end_s: 11.6 },
    { text: "tail", start_s: 14.8, end_s: 15.4 },
    { text: "after", start_s: 15, end_s: 16 },
  ];

  it("shifts to clip time, drops out-of-range and blank words, clamps edges", () => {
    expect(toClipWords(words, 10, 5)).toEqual([
      { text: "straddle", start: 0, end: expect.closeTo(0.3, 6) },
      { text: "inside", start: 1, end: 1.5 },
      { text: "tail", start: expect.closeTo(4.8, 6), end: 5 },
    ]);
  });

  it("returns nothing for an empty transcript", () => {
    expect(toClipWords([], 0, 10)).toEqual([]);
  });
});

describe("chunkWords", () => {
  it("never exceeds 4 words per line", () => {
    const lines = chunkWords(steady("a b c d e f g h i j".split(" ")));
    expect(lines.map((l) => l.words.length)).toEqual([4, 4, 2]);
    for (const line of lines) expect(line.words.length).toBeLessThanOrEqual(4);
  });

  it("rebalances so a continuing word never dangles alone", () => {
    const lines = chunkWords(steady("one two three four five".split(" ")));
    expect(lines.map((l) => l.words.map((w) => w.text))).toEqual([
      ["one", "two", "three"],
      ["four", "five"],
    ]);
  });

  it("merges a dangling word into the previous line when it fits", () => {
    // "wow" is cut off by the character budget, then fits back after rebalancing fails.
    const words = steady(["absolutely", "unbelievable", "wow"]);
    const lines = chunkWords(words, { maxChars: 30 });
    expect(lines.map((l) => l.words.length)).toEqual([3]);
    const tight = chunkWords(words, { maxChars: 23 });
    expect(tight.map((l) => l.words.map((w) => w.text))).toEqual([
      ["absolutely", "unbelievable"],
      ["wow"],
    ]);
  });

  it("keeps a lone word after a sentence end or pause", () => {
    const lines = chunkWords([
      { text: "It", start: 0, end: 0.2 },
      { text: "ended.", start: 0.2, end: 0.5 },
      { text: "Boom", start: 0.5, end: 0.9 },
    ]);
    expect(lines.map((l) => l.words.length)).toEqual([2, 1]);
  });

  it("breaks on long pauses", () => {
    const words: ClipWord[] = [
      { text: "wait", start: 0, end: 0.3 },
      { text: "for", start: 0.3, end: 0.5 },
      { text: "it", start: 2.0, end: 2.3 },
      { text: "now", start: 2.3, end: 2.6 },
    ];
    const lines = chunkWords(words);
    expect(lines.map((l) => l.words.map((w) => w.text))).toEqual([
      ["wait", "for"],
      ["it", "now"],
    ]);
  });

  it("breaks after sentence-ending punctuation", () => {
    const lines = chunkWords(steady(["I", "won.", "Then", "lost"]));
    expect(lines.map((l) => l.words.map((w) => w.text))).toEqual([
      ["I", "won."],
      ["Then", "lost"],
    ]);
  });

  it("respects the character budget", () => {
    const lines = chunkWords(steady(["extraordinarily", "uncomfortable", "situation", "ok"]));
    for (const line of lines) {
      const chars = line.words.map((w) => w.text).join(" ").length;
      expect(line.words.length === 1 || chars <= 22).toBe(true);
    }
  });

  it("times lines from first word start until the next line (or a short linger)", () => {
    const lines = chunkWords(steady("a b c d e f".split(" ")));
    expect(lines[0]!.start).toBe(0);
    expect(lines[0]!.end).toBeCloseTo(lines[1]!.start, 6);
    const last = lines[lines.length - 1]!;
    expect(last.end).toBeCloseTo(1.8 + 0.4, 6);
  });

  it("does not let a line linger into a pause-separated next line", () => {
    const words: ClipWord[] = [
      { text: "a", start: 0, end: 0.2 },
      { text: "b", start: 0.2, end: 0.4 },
      { text: "c", start: 1.5, end: 1.7 },
      { text: "d", start: 1.7, end: 1.9 },
    ];
    const [first, second] = chunkWords(words);
    expect(first!.end).toBeCloseTo(0.8, 6);
    expect(second!.start).toBe(1.5);
  });

  it("handles empty input", () => {
    expect(chunkWords([])).toEqual([]);
  });
});

describe("active line/word lookup", () => {
  const words: ClipWord[] = [
    { text: "a", start: 0.5, end: 0.8 },
    { text: "b", start: 0.8, end: 1.0 },
    { text: "c", start: 1.1, end: 1.4 },
    { text: "d", start: 3.0, end: 3.3 },
    { text: "e", start: 3.3, end: 3.6 },
  ];
  const lines = chunkWords(words);

  it("finds no line before the first word or in long gaps", () => {
    expect(activeLineIndex(lines, 0)).toBe(-1);
    expect(activeLineIndex(lines, 2.5)).toBe(-1);
    expect(activeLineIndex(lines, 10)).toBe(-1);
  });

  it("finds the line on screen", () => {
    expect(activeLineIndex(lines, 0.5)).toBe(0);
    expect(activeLineIndex(lines, 1.6)).toBe(0);
    expect(activeLineIndex(lines, 3.1)).toBe(1);
  });

  it("highlights the word being spoken and holds it through short gaps", () => {
    const line = lines[0]!;
    expect(activeWordIndex(line, 0.5)).toBe(0);
    expect(activeWordIndex(line, 0.85)).toBe(1);
    expect(activeWordIndex(line, 1.05)).toBe(1); // gap between b and c
    expect(activeWordIndex(line, 1.2)).toBe(2);
    expect(activeWordIndex(line, 0.1)).toBe(-1);
  });

  it("highlight is frame-accurate (within one frame, well under 100 ms)", () => {
    const fps = 30;
    const line = lines[0]!;
    for (const [i, word] of line.words.entries()) {
      const frame = secondsToFrame(word.start, fps);
      // The first frame whose start time is >= word.start shows the word highlighted.
      const firstLit = frameToSeconds(frame, fps) >= word.start - 1e-9 ? frame : frame + 1;
      expect(activeWordIndex(line, frameToSeconds(firstLit, fps))).toBe(i);
      expect(frameToSeconds(firstLit, fps) - word.start).toBeLessThan(1 / fps);
    }
  });
});

describe("frame conversion", () => {
  it("round-trips whole frames", () => {
    for (const f of [0, 1, 29, 30, 899]) {
      expect(secondsToFrame(frameToSeconds(f, 30), 30)).toBe(f);
    }
  });

  it("maps a time to the frame containing it", () => {
    expect(secondsToFrame(1.0, 30)).toBe(30);
    expect(secondsToFrame(1.01, 30)).toBe(30);
    expect(secondsToFrame(0.0333, 30)).toBe(0);
  });
});

describe("fitFontSize", () => {
  const opts = { maxWidth: 900, maxSize: 96, minSize: 56, charWidthEm: 0.75 };
  it("caps short lines at the max size", () => {
    expect(fitFontSize(4, opts)).toBe(96);
    expect(fitFontSize(0, opts)).toBe(96);
  });
  it("shrinks long lines but never below the min", () => {
    expect(fitFontSize(16, opts)).toBe(75);
    expect(fitFontSize(80, opts)).toBe(56);
  });
});
