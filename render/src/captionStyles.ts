// Caption style registry, keyed by EDL `caption_style`. Add new looks here (S4b).
import type React from "react";
import type { CaptionStyleId } from "./types";

export type CaptionStyleDef = {
  maxFontSize: number;
  minFontSize: number;
  /** Average glyph width in em for the uppercase display font (for fitting). */
  charWidthEm: number;
  base: React.CSSProperties;
  active: React.CSSProperties;
  /** Scale applied to the active word at the peak of its pop. */
  activeScale: number;
};

const OUTLINE = "#000000";

export const CAPTION_STYLES: Record<CaptionStyleId, CaptionStyleDef> = {
  bold_pop: {
    maxFontSize: 104,
    minFontSize: 56,
    charWidthEm: 0.74,
    base: {
      color: "#FFFFFF",
      fontWeight: 900,
      textTransform: "uppercase",
      letterSpacing: -0.5,
      WebkitTextStroke: `10px ${OUTLINE}`,
      paintOrder: "stroke fill",
      textShadow: "0 8px 18px rgba(0,0,0,0.55)",
    },
    active: { color: "#FFE14D" },
    activeScale: 1.12,
  },
};
