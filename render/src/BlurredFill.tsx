// `blurred_fill` layout: the source fit to width over a blurred, zoomed copy of itself.
// The background copy is rendered at quarter size and scaled up, so the CSS blur runs on
// 1/16th of the pixels (blur hides the upscale anyway).
import type React from "react";
import { AbsoluteFill, OffthreadVideo, type VolumeProp } from "remotion";
import { HEIGHT, WIDTH } from "./types";

const BG_SCALE = 4;

export const BlurredFill: React.FC<{ src: string; trimBefore: number; volume?: VolumeProp }> = ({
  src,
  trimBefore,
  volume,
}) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "#000", overflow: "hidden" }}>
      <div
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          width: WIDTH / BG_SCALE,
          height: HEIGHT / BG_SCALE,
          transform: `scale(${BG_SCALE * 1.15})`,
          transformOrigin: "0 0",
          marginLeft: -(WIDTH * 0.075),
          marginTop: -(HEIGHT * 0.075),
        }}
      >
        <OffthreadVideo
          src={src}
          trimBefore={trimBefore}
          muted
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            filter: "blur(6px) brightness(0.6) saturate(1.2)",
          }}
        />
      </div>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <OffthreadVideo
          src={src}
          trimBefore={trimBefore}
          volume={volume}
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
        />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
