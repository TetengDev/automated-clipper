// Crop window position for the face_track layout: linear between keys, clamped at the
// ends. Two keys at the same time are a scene cut: from that time on the later key wins,
// so the crop jumps instead of easing across the cut.
import { interpolate } from "remotion";
import type { CropKey } from "./types";

export const cropXAt = (keys: readonly CropKey[], t: number): number => {
  // Binary search: the number of keys at or before t.
  let lo = 0;
  let hi = keys.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (keys[mid]!.t_s <= t) lo = mid + 1;
    else hi = mid;
  }
  const a = keys[Math.max(lo - 1, 0)]!;
  const b = keys[lo];
  if (lo === 0 || b === undefined) return a.x_center;
  return interpolate(t, [a.t_s, b.t_s], [a.x_center, b.x_center], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
};
