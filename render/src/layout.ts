// Frame geometry shared by overlays. Platforms cover the bottom ~20% (caption, buttons)
// and the right edge (like/share rail), so text stays inside SAFE_* bounds.
import { HEIGHT, WIDTH } from "./types";

export const SAFE_SIDE_PX = 90;
export const SAFE_TOP_PX = Math.round(HEIGHT * 0.1);
/** Captions sit above this line (bottom 20% is reserved for platform UI). */
export const SAFE_BOTTOM_PX = Math.round(HEIGHT * 0.2);
/** Caption block baseline: a little above the safe-zone floor. */
export const CAPTION_BOTTOM_PX = Math.round(HEIGHT * 0.28);
export const CONTENT_WIDTH_PX = WIDTH - 2 * SAFE_SIDE_PX;
