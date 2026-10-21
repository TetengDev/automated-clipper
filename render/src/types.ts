// TypeScript mirror of clipper.edl.EDL (src/clipper/edl.py). The JSON Schema in
// ../schema/edl.schema.json is generated from pydantic and is what we validate against;
// these types describe the *validated* (defaults applied) props.

export const FPS = 30;
export const WIDTH = 1080;
export const HEIGHT = 1920;

export type Layout = "blurred_fill";
export type HookStyle = "stakes" | "question" | "quote";
export type CaptionStyleId = "bold_pop";

export type SourceSegment = {
  /** Path relative to the media root (served as Remotion's public dir). */
  path: string;
  /** Cut start, seconds in the source. */
  in_s: number;
  /** Cut end, seconds in the source. */
  out_s: number;
};

export type Hook = {
  text: string;
  style: HookStyle;
  /** Seconds from the start of the clip (1..3). */
  duration_s: number;
};

export type Word = {
  text: string;
  /** Seconds in the source timeline. */
  start_s: number;
  /** Seconds in the source timeline. */
  end_s: number;
};

export type Edl = {
  schema_version: 1;
  source: SourceSegment;
  layout: Layout;
  hook: Hook | null;
  words: Word[];
  captions_enabled: boolean;
  caption_style: CaptionStyleId;
  progress_bar: boolean;
};
