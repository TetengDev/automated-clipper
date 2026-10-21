// Word-by-word captions: one 2–4 word line at a time, active word highlighted and popped.
// Positioned above the bottom 20% of the frame (platform UI safe zone).
import type React from "react";
import { useMemo } from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { CAPTION_STYLES } from "./captionStyles";
import {
  activeLineIndex,
  activeWordIndex,
  chunkWords,
  fitFontSize,
  frameToSeconds,
  secondsToFrame,
  type ClipWord,
} from "./captionTiming";
import { FONT_STACK } from "./fonts";
import { CAPTION_BOTTOM_PX, CONTENT_WIDTH_PX, SAFE_SIDE_PX } from "./layout";
import type { CaptionStyleId } from "./types";

export const Captions: React.FC<{ words: ClipWord[]; styleId: CaptionStyleId }> = ({
  words,
  styleId,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const lines = useMemo(() => chunkWords(words), [words]);
  const style = CAPTION_STYLES[styleId];

  const t = frameToSeconds(frame, fps);
  const lineIdx = activeLineIndex(lines, t);
  const line = lines[lineIdx];
  if (line === undefined) {
    return null;
  }
  const wordIdx = activeWordIndex(line, t);
  const lineStartFrame = secondsToFrame(line.start, fps);
  const enter = spring({
    frame: frame - lineStartFrame,
    fps,
    config: { damping: 14, stiffness: 220, mass: 0.5 },
  });
  const chars = line.words.reduce((n, w) => n + w.text.length, 0) + line.words.length - 1;
  const fontSize = fitFontSize(chars, {
    maxWidth: CONTENT_WIDTH_PX,
    maxSize: style.maxFontSize,
    minSize: style.minFontSize,
    charWidthEm: style.charWidthEm,
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: CAPTION_BOTTOM_PX,
        paddingLeft: SAFE_SIDE_PX,
        paddingRight: SAFE_SIDE_PX,
      }}
    >
      <div
        data-testid="caption-line"
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          columnGap: fontSize * 0.28,
          maxWidth: CONTENT_WIDTH_PX,
          fontFamily: FONT_STACK,
          fontSize,
          lineHeight: 1.1,
          textAlign: "center",
          transform: `scale(${interpolate(enter, [0, 1], [0.8, 1])})`,
          opacity: interpolate(enter, [0, 1], [0.4, 1]),
        }}
      >
        {line.words.map((word, i) => {
          const isActive = i === wordIdx;
          const pop = isActive
            ? spring({
                frame: frame - secondsToFrame(word.start, fps),
                fps,
                config: { damping: 10, stiffness: 260, mass: 0.4 },
                durationInFrames: 6,
              })
            : 0;
          const scale = isActive
            ? interpolate(pop, [0, 0.6, 1], [1, style.activeScale, 1.04])
            : 1;
          return (
            <span
              key={`${word.start}-${i}`}
              style={{
                ...style.base,
                ...(isActive ? style.active : {}),
                display: "inline-block",
                transform: `scale(${scale})`,
                whiteSpace: "nowrap",
              }}
            >
              {word.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
