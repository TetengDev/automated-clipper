// Hook card: bold hook text over the first `duration_s` seconds. Fully visible from frame 0
// (it pops in from 85% scale), slides/fades out over the last few frames.
import type React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { fitFontSize } from "./captionTiming";
import { FONT_STACK } from "./fonts";
import { CONTENT_WIDTH_PX, SAFE_SIDE_PX, SAFE_TOP_PX } from "./layout";
import type { Hook, HookStyle } from "./types";

const EXIT_FRAMES = 6;

const THEMES: Record<HookStyle, { bg: string; fg: string; accent: string }> = {
  stakes: { bg: "#FFD400", fg: "#111111", accent: "#E5002B" },
  question: { bg: "#FFFFFF", fg: "#111111", accent: "#2D6BFF" },
  quote: { bg: "rgba(12,12,12,0.88)", fg: "#FFFFFF", accent: "#FFD400" },
};

const decorate = (text: string, style: HookStyle): string => {
  const t = text.trim();
  if (style === "quote") {
    return `“${t.replace(/^["“]|["”]$/g, "")}”`;
  }
  if (style === "question" && !t.endsWith("?")) {
    return `${t}?`;
  }
  return t;
};

export const HookCard: React.FC<{ hook: Hook }> = ({ hook }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const total = Math.max(1, Math.round(hook.duration_s * fps));
  if (frame >= total) {
    return null;
  }
  const theme = THEMES[hook.style];
  const text = decorate(hook.text, hook.style);

  const pop = spring({ frame, fps, config: { damping: 12, stiffness: 180, mass: 0.6 } });
  const scaleIn = interpolate(pop, [0, 1], [0.85, 1]);
  const exitStart = Math.max(0, total - EXIT_FRAMES);
  const exit = interpolate(frame, [exitStart, total], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const opacity = 1 - exit;
  const translateY = interpolate(exit, [0, 1], [0, -60]);
  const rotate = hook.style === "stakes" ? -2 : 0;

  // Hook copy is short (<=120 chars); wrap to at most ~3 lines at a big size.
  const longestLine = Math.min(text.length, 16);
  const fontSize = fitFontSize(longestLine, {
    maxWidth: CONTENT_WIDTH_PX - 80,
    maxSize: text.length > 60 ? 72 : 96,
    minSize: 56,
    charWidthEm: 0.72,
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-start",
        alignItems: "center",
        paddingTop: SAFE_TOP_PX + 80,
        paddingLeft: SAFE_SIDE_PX,
        paddingRight: SAFE_SIDE_PX,
      }}
    >
      <div
        data-testid="hook-card"
        style={{
          maxWidth: CONTENT_WIDTH_PX,
          padding: "36px 44px",
          borderRadius: 28,
          backgroundColor: theme.bg,
          color: theme.fg,
          fontFamily: FONT_STACK,
          fontWeight: 900,
          fontSize,
          lineHeight: 1.08,
          textAlign: "center",
          textTransform: hook.style === "quote" ? "none" : "uppercase",
          letterSpacing: -1,
          boxShadow: "0 18px 48px rgba(0,0,0,0.45)",
          borderBottom: `12px solid ${theme.accent}`,
          opacity,
          transform: `translateY(${translateY}px) scale(${scaleIn}) rotate(${rotate}deg)`,
          overflowWrap: "break-word",
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
