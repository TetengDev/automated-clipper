// Pure caption timing helpers (unit-tested with vitest; no React/Remotion imports).
import type { Word } from "./types";

/** A word on the clip timeline (seconds from the first frame of the clip). */
export type ClipWord = {
  text: string;
  start: number;
  end: number;
};

/** One on-screen caption line: 2–4 words shown together, highlighted word by word. */
export type CaptionLine = {
  words: ClipWord[];
  start: number;
  end: number;
};

export type ChunkOptions = {
  /** Hard cap on words per line. */
  maxWords: number;
  /** Lines shorter than this are merged into a neighbour when timing allows. */
  minWords: number;
  /** Soft cap on characters per line (spaces included) so a line fits on a phone. */
  maxChars: number;
  /** A silence longer than this always starts a new line. */
  maxGapS: number;
  /** How long a line lingers after its last word when nothing follows. */
  lingerS: number;
};

export const DEFAULT_CHUNK_OPTIONS: ChunkOptions = {
  maxWords: 4,
  minWords: 2,
  maxChars: 22,
  maxGapS: 0.6,
  lingerS: 0.4,
};

const SENTENCE_END = /[.!?…]["')\]]?$/;

/**
 * Map source-timeline words onto the clip timeline: keep words that overlap
 * [offsetS, offsetS + durationS), shift them so the clip starts at 0, clamp to the clip.
 */
export const toClipWords = (
  words: readonly Word[],
  offsetS: number,
  durationS: number,
): ClipWord[] => {
  const out: ClipWord[] = [];
  for (const word of words) {
    const start = word.start_s - offsetS;
    const end = word.end_s - offsetS;
    if (end <= 0 || start >= durationS) {
      continue;
    }
    const text = word.text.trim();
    if (text.length === 0) {
      continue;
    }
    out.push({
      text,
      start: Math.max(0, start),
      end: Math.min(durationS, Math.max(end, start)),
    });
  }
  return out;
};

const lineChars = (words: readonly ClipWord[]): number =>
  words.reduce((n, w) => n + w.text.length, 0) + Math.max(0, words.length - 1);

/** Group words into lines of up to `maxWords` words, breaking on pauses and sentence ends. */
export const chunkWords = (
  words: readonly ClipWord[],
  options: Partial<ChunkOptions> = {},
): CaptionLine[] => {
  const opts = { ...DEFAULT_CHUNK_OPTIONS, ...options };
  const groups: ClipWord[][] = [];
  let current: ClipWord[] = [];

  for (const word of words) {
    const prev = current[current.length - 1];
    const breakHere =
      prev !== undefined &&
      (current.length >= opts.maxWords ||
        word.start - prev.end > opts.maxGapS ||
        lineChars([...current, word]) > opts.maxChars ||
        (SENTENCE_END.test(prev.text) && current.length >= opts.minWords));
    if (breakHere) {
      groups.push(current);
      current = [];
    }
    current.push(word);
  }
  if (current.length > 0) {
    groups.push(current);
  }

  // A dangling single word that continues the previous line (no pause, no sentence end):
  // merge it in if the line still fits, otherwise borrow the previous line's last word
  // so both lines have >= minWords ([a b c d][e] -> [a b c][d e]).
  const merged: ClipWord[][] = [];
  for (const group of groups) {
    const prev = merged[merged.length - 1];
    const first = group[0];
    const prevLast = prev?.[prev.length - 1];
    const continues =
      prev !== undefined &&
      first !== undefined &&
      prevLast !== undefined &&
      group.length < opts.minWords &&
      first.start - prevLast.end <= opts.maxGapS &&
      !SENTENCE_END.test(prevLast.text);
    if (
      continues &&
      prev.length + group.length <= opts.maxWords &&
      lineChars([...prev, ...group]) <= opts.maxChars
    ) {
      prev.push(...group);
    } else if (
      continues &&
      prev.length > opts.minWords &&
      lineChars([prevLast, ...group]) <= opts.maxChars
    ) {
      prev.pop();
      merged.push([prevLast, ...group]);
    } else {
      merged.push([...group]);
    }
  }

  return merged.map((group, index) => {
    const first = group[0]!;
    const last = group[group.length - 1]!;
    const next = merged[index + 1]?.[0];
    const lingerEnd = last.end + opts.lingerS;
    const end = next === undefined ? lingerEnd : Math.min(lingerEnd, next.start);
    return { words: group, start: first.start, end: Math.max(end, last.end) };
  });
};

/** Index of the line on screen at time `t` (seconds), or -1 between lines. */
export const activeLineIndex = (lines: readonly CaptionLine[], t: number): number => {
  let lo = 0;
  let hi = lines.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const line = lines[mid]!;
    if (t < line.start) {
      hi = mid - 1;
    } else if (t >= line.end) {
      lo = mid + 1;
    } else {
      return mid;
    }
  }
  return -1;
};

/**
 * Index of the highlighted word in `line` at time `t`. The last word that has started
 * stays highlighted through short gaps, so the highlight never flickers off mid-line.
 */
export const activeWordIndex = (line: CaptionLine, t: number): number => {
  let active = -1;
  line.words.forEach((word, index) => {
    if (t >= word.start) {
      active = index;
    }
  });
  return active;
};

/** Seconds -> frame index (frame f covers [f/fps, (f+1)/fps)). */
export const secondsToFrame = (seconds: number, fps: number): number =>
  Math.floor(seconds * fps + 1e-6);

/** Start time (seconds) of frame `frame`. */
export const frameToSeconds = (frame: number, fps: number): number => frame / fps;

/** Font size (px) that keeps a line of `chars` characters within `maxWidth`. */
export const fitFontSize = (
  chars: number,
  { maxWidth, maxSize, minSize, charWidthEm }: {
    maxWidth: number;
    maxSize: number;
    minSize: number;
    charWidthEm: number;
  },
): number => {
  if (chars <= 0) {
    return maxSize;
  }
  const size = maxWidth / (chars * charWidthEm);
  return Math.round(Math.min(maxSize, Math.max(minSize, size)));
};
