// The `Clip` composition: layout + hook card + captions + progress bar, driven by the EDL.
import type React from "react";
import { useMemo } from "react";
import { AbsoluteFill, staticFile, useVideoConfig } from "remotion";
import { BlurredFill } from "./BlurredFill";
import { Captions } from "./Captions";
import { FaceTrack } from "./FaceTrack";
import { toClipWords } from "./captionTiming";
import { HookCard } from "./HookCard";
import { loadFonts } from "./fonts";
import { ProgressBar } from "./ProgressBar";
import type { Edl } from "./types";

loadFonts();

export const Clip: React.FC<Edl> = (edl) => {
  const { fps, durationInFrames } = useVideoConfig();
  // The video is trimmed to whole frames; offset captions by the same amount so both
  // stay on one clock.
  const trimBefore = Math.round(edl.source.in_s * fps);
  const offsetS = trimBefore / fps;
  const clipWords = useMemo(
    () => toClipWords(edl.words, offsetS, durationInFrames / fps),
    [edl.words, offsetS, durationInFrames, fps],
  );
  const src = staticFile(edl.source.path);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {edl.layout === "face_track" && edl.crop !== null ? (
        <FaceTrack src={src} trimBefore={trimBefore} crop={edl.crop} />
      ) : (
        <BlurredFill src={src} trimBefore={trimBefore} />
      )}
      {edl.captions_enabled && clipWords.length > 0 ? (
        <Captions words={clipWords} styleId={edl.caption_style} />
      ) : null}
      {edl.hook !== null ? <HookCard hook={edl.hook} /> : null}
      {edl.progress_bar ? <ProgressBar /> : null}
    </AbsoluteFill>
  );
};
