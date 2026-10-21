// Bundled font (no network at render time): Montserrat Black from @fontsource (OFL-1.1).
import { continueRender, delayRender } from "remotion";
import latin from "@fontsource/montserrat/files/montserrat-latin-900-normal.woff2";
import latinExt from "@fontsource/montserrat/files/montserrat-latin-ext-900-normal.woff2";

export const FONT_FAMILY = "ClipperDisplay";
// Emoji fall back to Noto Color Emoji (installed in the Docker image; local machines use
// whatever emoji font the OS has).
export const FONT_STACK = `${FONT_FAMILY}, "Arial Black", "Helvetica Neue", Arial, "Noto Color Emoji", "Apple Color Emoji", sans-serif`;

const LATIN_RANGE =
  "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, " +
  "U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD";
const LATIN_EXT_RANGE =
  "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, " +
  "U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, " +
  "U+2113, U+2C60-2C7F, U+A720-A7FF";

let loaded = false;

export const loadFonts = (): void => {
  if (loaded || typeof document === "undefined") {
    return;
  }
  loaded = true;
  const handle = delayRender("Loading caption font");
  const faces = [
    new FontFace(FONT_FAMILY, `url(${latin}) format("woff2")`, {
      weight: "900",
      unicodeRange: LATIN_RANGE,
    }),
    new FontFace(FONT_FAMILY, `url(${latinExt}) format("woff2")`, {
      weight: "900",
      unicodeRange: LATIN_EXT_RANGE,
    }),
  ];
  Promise.all(faces.map((face) => face.load()))
    .then((ready) => {
      for (const face of ready) {
        document.fonts.add(face);
      }
    })
    .catch((err: unknown) => {
      // Fall back to the system stack rather than failing the render.
      console.warn("caption font failed to load", err);
    })
    .finally(() => continueRender(handle));
};
