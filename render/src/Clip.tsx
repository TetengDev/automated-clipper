// The `Clip` composition: layout + hook card + captions + progress bar, driven by the EDL.
// With jump cuts (EDL v2) the video is a <Series> of the kept segments, each trimmed to
// whole frames from the one pre-cut file (never a pre-cut per segment: lip-sync drift).
// The audio fades at each join are sample-level, in the ffmpeg finalize pass
// (clipper.render). Everything else runs on the output timeline.
import type React from "react";
import { useMemo } from "react";
import { AbsoluteFill, Series, staticFile, useVideoConfig } from "remotion";
import { BlurredFill } from "./BlurredFill";
import { Captions } from "./Captions";
import { FaceTrack } from "./FaceTrack";
import { toClipWords } from "./captionTiming";
import { HookCard } from "./HookCard";
import { loadFonts } from "./fonts";
import { ProgressBar } from "./ProgressBar";
import { frameSegments, outputWords, segmentsOf } from "./timeline";
import type { Edl } from "./types";

loadFonts();

export const Clip: React.FC<Edl> = (edl) => {
  const { fps, durationInFrames } = useVideoConfig();
  // The video is trimmed to whole frames; captions map onto the same frames so both stay
  // on one clock.
  const segs = useMemo(() => frameSegments(segmentsOf(edl), fps), [edl, fps]);
  const clipWords = useMemo(
    () => toClipWords(outputWords(edl.words, segs, fps), 0, durationInFrames / fps),
    [edl.words, segs, durationInFrames, fps],
  );
  const src = staticFile(edl.source.path);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Series>
        {segs.map((seg) => (
          <Series.Sequence key={seg.from} durationInFrames={seg.durationInFrames}>
            {edl.layout === "face_track" && edl.crop !== null ? (
              <FaceTrack src={src} trimBefore={seg.trimBefore} crop={edl.crop} from={seg.from} />
            ) : (
              <BlurredFill src={src} trimBefore={seg.trimBefore} />
            )}
          </Series.Sequence>
        ))}
      </Series>
      {edl.captions_enabled && clipWords.length > 0 ? (
        <Captions words={clipWords} styleId={edl.caption_style} />
      ) : null}
      {edl.hook !== null ? <HookCard hook={edl.hook} /> : null}
      {edl.progress_bar ? <ProgressBar /> : null}
    </AbsoluteFill>
  );
};
