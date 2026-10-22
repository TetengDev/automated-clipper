// `face_track` layout: the source scaled to fill the frame height, shifted so the crop
// centre (a share of the source width, from the EDL's crop keys) sits mid-frame. The
// keys are clamped in Python so the 9:16 window never leaves the source.
import type React from "react";
import { AbsoluteFill, OffthreadVideo, useCurrentFrame, useVideoConfig } from "remotion";
import { cropXAt } from "./crop";
import type { CropKey } from "./types";

export const FaceTrack: React.FC<{ src: string; trimBefore: number; crop: CropKey[] }> = ({
  src,
  trimBefore,
  crop,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const x = cropXAt(crop, frame / fps);
  return (
    <AbsoluteFill style={{ backgroundColor: "#000", overflow: "hidden" }}>
      <OffthreadVideo
        src={src}
        trimBefore={trimBefore}
        style={{
          position: "absolute",
          top: 0,
          left: "50%",
          height: "100%",
          width: "auto",
          maxWidth: "none",
          // A percentage of the video's own rendered width: x of the source to mid-frame.
          transform: `translateX(${-x * 100}%)`,
        }}
      />
    </AbsoluteFill>
  );
};
