import { describe, expect, it } from "vitest";
import { cropXAt } from "../src/crop";

const keys = [
  { t_s: 0, x_center: 0.3 },
  { t_s: 1, x_center: 0.5 },
  // A scene cut at 2 s: two keys at the same time, the jump is not eased.
  { t_s: 2, x_center: 0.5 },
  { t_s: 2, x_center: 0.8 },
  { t_s: 3, x_center: 0.7 },
];

describe("cropXAt", () => {
  it("interpolates linearly between keys", () => {
    expect(cropXAt(keys, 0)).toBe(0.3);
    expect(cropXAt(keys, 0.5)).toBeCloseTo(0.4);
    expect(cropXAt(keys, 1)).toBe(0.5);
    expect(cropXAt(keys, 2.5)).toBeCloseTo(0.75);
  });

  it("jumps at a cut with no easing", () => {
    expect(cropXAt(keys, 1.999)).toBeCloseTo(0.5);
    expect(cropXAt(keys, 2)).toBe(0.8);
  });

  it("jumps when one key sits just after the previous one", () => {
    const near = [
      { t_s: 0, x_center: 0.3 },
      { t_s: 1, x_center: 0.3 },
      { t_s: 1 + 1e-4, x_center: 0.7 },
    ];
    expect(cropXAt(near, 1 / 1)).toBe(0.3);
    expect(cropXAt(near, 1 + 1 / 30)).toBe(0.7);
  });

  it("clamps before the first and after the last key", () => {
    expect(cropXAt(keys, -1)).toBe(0.3);
    expect(cropXAt(keys, 10)).toBe(0.7);
    expect(cropXAt([{ t_s: 0.5, x_center: 0.6 }], 0)).toBe(0.6);
  });

  it("finds the segment in long key lists", () => {
    const many = Array.from({ length: 2000 }, (_, i) => ({ t_s: i / 5, x_center: (i % 2) * 0.5 }));
    expect(cropXAt(many, 100.1)).toBeCloseTo(0.25);
    expect(cropXAt(many, 100.2)).toBe(0.5);
  });
});
