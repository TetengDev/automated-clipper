// Validates composition props against the JSON Schema generated from pydantic, plus the
// cross-field rules pydantic enforces in model validators (JSON Schema cannot express them).
// Keep `semanticErrors` in sync with the `_check` validators in src/clipper/edl.py.
import Ajv2020 from "ajv/dist/2020.js";
import schema from "../schema/edl.schema.json";
import type { Edl } from "./types";

const MIN_CLIP_SECONDS = 1;
const MAX_CLIP_SECONDS = 180;

export class EdlValidationError extends Error {
  readonly errors: string[];

  constructor(errors: string[]) {
    // One line, paths and rule messages only (never values): the Python side reads it
    // from stderr as the reason a clip was left out.
    super(`invalid clip props: ${errors.join("; ")}`);
    this.name = "EdlValidationError";
    this.errors = errors;
  }
}

// ajv's CJS default export shape differs between bundlers; normalise it.
type AjvCtor = typeof Ajv2020;
const AjvClass: AjvCtor =
  (Ajv2020 as unknown as { default?: AjvCtor }).default ?? Ajv2020;

const ajv = new AjvClass({ allErrors: true, useDefaults: true, strict: true });
const validateSchema = ajv.compile(schema);

const semanticErrors = (edl: Edl): string[] => {
  const errors: string[] = [];
  const { source, hook, words, crop } = edl;
  if (source.path.split("/").includes("..")) {
    errors.push("source.path must not contain '..' segments");
  }
  const duration = source.out_s - source.in_s;
  if (duration < MIN_CLIP_SECONDS || duration > MAX_CLIP_SECONDS) {
    errors.push(
      `source.out_s - source.in_s must be between ${MIN_CLIP_SECONDS} and ${MAX_CLIP_SECONDS} seconds`,
    );
  }
  let previous = -1;
  words.forEach((word, index) => {
    if (word.end_s < word.start_s) {
      errors.push(`words[${index}]: end_s must be >= start_s`);
    }
    if (word.start_s < previous) {
      errors.push(`words must be sorted by start_s (index ${index})`);
    }
    previous = word.start_s;
  });
  if (hook !== null && hook.duration_s > duration) {
    errors.push("hook.duration_s must not exceed the clip duration");
  }
  if ((edl.layout === "face_track") !== (crop !== null)) {
    errors.push(
      crop === null
        ? "crop is required when layout is face_track"
        : "crop is only allowed when layout is face_track",
    );
  }
  crop?.forEach((key, index) => {
    if (index > 0 && key.t_s < crop[index - 1]!.t_s) {
      errors.push(`crop must be sorted by t_s (index ${index})`);
    }
  });
  return errors;
};

/** Validate raw props; returns a deep copy with schema defaults applied. Throws on error. */
export const parseEdl = (input: unknown): Edl => {
  const candidate: unknown = structuredClone(input);
  if (!validateSchema(candidate)) {
    const errors = (validateSchema.errors ?? []).map(
      (e) => `${e.instancePath || "<root>"}: ${e.message ?? "invalid"}`,
    );
    throw new EdlValidationError(errors);
  }
  const edl = candidate as Edl;
  const errors = semanticErrors(edl);
  if (errors.length > 0) {
    throw new EdlValidationError(errors);
  }
  return edl;
};

export const durationInFrames = (edl: Edl, fps: number): number =>
  Math.max(1, Math.round((edl.source.out_s - edl.source.in_s) * fps));
