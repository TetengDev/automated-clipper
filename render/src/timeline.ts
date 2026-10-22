// The output timeline of an EDL with jump cuts (v2 segments). Pure helpers, unit-tested
// with vitest. The video, the captions and the crop all run on the same whole frames.
import type { Edl, Segment, Word } from "./types";

/** One kept segment in frames: where it starts in the source, its length, where it plays. */
export type FrameSegment = {
  trimBefore: number;
  durationInFrames: number;
  from: number;
};

/** The kept ranges; a v1 EDL (or a v2 without segments) is its one `source` range. */
export const segmentsOf = (edl: Edl): readonly Segment[] =>
  edl.segments ?? [{ in_s: edl.source.in_s, out_s: edl.source.out_s }];

/** Each segment trimmed to whole frames (start and length rounded half up, like v1). */
export const frameSegments = (segments: readonly Segment[], fps: number): FrameSegment[] => {
  let from = 0;
  return segments.map((seg) => {
    const out = {
      trimBefore: Math.round(seg.in_s * fps),
      durationInFrames: Math.round((seg.out_s - seg.in_s) * fps),
      from,
    };
    from += out.durationInFrames;
    return out;
  });
};

/** Source seconds -> output seconds. A time inside a cut lands on its join; times outside
 * the kept range clamp to the ends. */
export const toOutputTime = (t: number, segs: readonly FrameSegment[], fps: number): number =>
  segs.reduce(
    (sum, seg) => sum + Math.min(Math.max(t - seg.trimBefore / fps, 0), seg.durationInFrames / fps),
    0,
  );

/** Words on the output timeline. A word that overlaps no kept segment (inside a cut, or
 * outside the clip) is dropped; a word across a join is clamped to it. */
export const outputWords = (
  words: readonly Word[],
  segs: readonly FrameSegment[],
  fps: number,
): Word[] =>
  words
    .filter((word) =>
      segs.some((seg) => {
        const a = seg.trimBefore / fps;
        const b = a + seg.durationInFrames / fps;
        return word.start_s < b && word.end_s > a;
      }),
    )
    .map((word) => ({
      text: word.text,
      start_s: toOutputTime(word.start_s, segs, fps),
      end_s: toOutputTime(word.end_s, segs, fps),
    }));
