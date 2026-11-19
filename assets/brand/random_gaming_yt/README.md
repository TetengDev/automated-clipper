# Random Gaming brand assets

Brand files for the Random Gaming commentary channel (`random_gaming_yt`, [CLP-155](https://linear.app/tengdev/issue/CLP-155)).

| File | What it is |
|---|---|
| `icon.png` | Channel icon, 800×800. A yellow die showing 5 with a cyan ring on purple. The burned-in watermark and the thumbnails use it too. |
| `banner.png` | Channel banner, 2560×1440. The die and the "RANDOM GAMING" wordmark, with the tagline "GAME NEWS • STREAMER MOMENTS • NEW SHORTS DAILY" in a pill below. All content sits in the 1546×423 centre area that every device shows. |
| `source/make.py` | Rebuilds `icon.png` (needs Google Chrome and ffmpeg). |
| `source/banner.py` | Rebuilds `banner.png` (needs Google Chrome and ffmpeg). It exits with an error if any content leaves the 1546×423 centre area. |
| `source/fonts/` | Anton (SIL Open Font License 1.1, see `Anton-OFL.txt`). |

Palette: yellow `#FFD400`, ink `#111111`, hot pink `#FF2D87`, cyan `#22E4FF`, purple `#5a1fb0` to `#14062f`.

YouTube cannot set the profile picture through the API. Upload `icon.png` in Studio under Customisation > Branding > Picture.

Upload `banner.png` in Studio under Customisation > Branding > Banner image.

Only commentary accounts use these files. Campaign accounts never carry a logo or watermark, because campaign rules forbid logos.
