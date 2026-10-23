import type React from "react";
import { type CalculateMetadataFunction, Composition } from "remotion";
import { Clip } from "./Clip";
import { FPS, HEIGHT, WIDTH, type Edl } from "./types";
import { durationInFrames, parseEdl } from "./validate";

// Preview props for `remotion studio` (expects public/sample.mp4); real renders always pass
// --props from Python.
const defaultProps: Edl = {
  schema_version: 2,
  source: { path: "sample.mp4", in_s: 0, out_s: 5 },
  layout: "blurred_fill",
  hook: { text: "Nobody expected this", style: "stakes", duration_s: 2 },
  words: [
    { text: "This", start_s: 0.2, end_s: 0.5 },
    { text: "is", start_s: 0.5, end_s: 0.7 },
    { text: "the", start_s: 0.7, end_s: 0.9 },
    { text: "moment", start_s: 0.9, end_s: 1.4 },
    { text: "everything", start_s: 1.6, end_s: 2.2 },
    { text: "changed.", start_s: 2.2, end_s: 2.9 },
  ],
  captions_enabled: true,
  caption_style: "bold_pop",
  progress_bar: true,
  crop: null,
  segments: null,
  cuts: [],
};

// Runs before any frame is rendered: invalid props abort the render here.
const calculateMetadata: CalculateMetadataFunction<Edl> = ({ props }) => {
  const edl = parseEdl(props);
  return { durationInFrames: durationInFrames(edl, FPS), props: edl };
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Clip"
      component={Clip}
      width={WIDTH}
      height={HEIGHT}
      fps={FPS}
      durationInFrames={150}
      defaultProps={defaultProps}
      calculateMetadata={calculateMetadata}
    />
  );
};
