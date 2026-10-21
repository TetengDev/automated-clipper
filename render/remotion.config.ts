// Remotion CLI config (`npx remotion render|studio`). Python passes per-render options
// (props file, public dir, output) as CLI flags; only project-wide defaults live here.
import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setJpegQuality(90);
Config.setCodec("h264");
Config.setPixelFormat("yuv420p");
Config.setOverwriteOutput(true);
