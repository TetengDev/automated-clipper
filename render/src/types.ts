// TypeScript mirror of clipper.edl.EDL (src/clipper/edl.py). The JSON Schema in
// ../schema/edl.schema.json is generated from pydantic and is what we validate against;
// these types describe the *validated* (defaults applied) props.

export const FPS = 30;
export const WIDTH = 1080;
export const HEIGHT = 1920;

export type Layout = "blurred_fill" | "face_track";
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

/** A kept range (EDL v2), seconds in the source, on the frame grid. */
export type Segment = {
  in_s: number;
  out_s: number;
  /** Generated footage (AI disclosure, CLP-96); not used by the renderer. */
  ai_generated?: boolean;
};

export type CutKind = "silence" | "stutter" | "retake" | "false_start" | "filler";

/** A range cut out (`applied`) or only proposed; a record, not used by the renderer. */
export type Cut = {
  in_s: number;
  out_s: number;
  kind: CutKind;
  confidence: number;
  reason: string;
  applied: boolean;
};

export type Hook = {
  text: string;
  style: HookStyle;
  /** Seconds from the start of the clip (1..3). */
  duration_s: number;
  /** Synthetic voice or text (AI disclosure, CLP-96); not used by the renderer. */
  ai_generated?: boolean;
};

export type Word = {
  text: string;
  /** Seconds in the source timeline. */
  start_s: number;
  /** Seconds in the source timeline. */
  end_s: number;
};

export type CropKey = {
  /** Seconds from the clip start (output timeline). */
  t_s: number;
  /** Crop window centre, share of the source width (0..1). */
  x_center: number;
};

export type Edl = {
  schema_version: 1 | 2;
  source: SourceSegment;
  layout: Layout;
  hook: Hook | null;
  words: Word[];
  captions_enabled: boolean;
  caption_style: CaptionStyleId;
  progress_bar: boolean;
  /** face_track only: sorted keys; two keys at one time are a jump (scene cut). */
  crop: CropKey[] | null;
  /** v2 jump cuts: the kept ranges, spanning `source`; null means the one range `source`. */
  segments: Segment[] | null;
  cuts: Cut[];
};
