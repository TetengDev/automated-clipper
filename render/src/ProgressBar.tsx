// Thin retention bar along the top edge that fills over the clip.
import type React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";

export const PROGRESS_BAR_HEIGHT_PX = 12;

export const ProgressBar: React.FC<{ color?: string }> = ({ color = "#FFD400" }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const progress = durationInFrames <= 1 ? 1 : frame / (durationInFrames - 1);
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: "100%",
          height: PROGRESS_BAR_HEIGHT_PX,
          backgroundColor: "rgba(255,255,255,0.25)",
        }}
      />
      <div
        data-testid="progress-bar"
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: `${Math.min(1, Math.max(0, progress)) * 100}%`,
          height: PROGRESS_BAR_HEIGHT_PX,
          backgroundColor: color,
        }}
      />
    </AbsoluteFill>
  );
};
