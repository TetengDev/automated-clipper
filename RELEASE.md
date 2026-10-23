# Release notes

Newest release first. How to cut a release (branches, PRs, tag, sync) is described in
[docs/BRANCHING.md](docs/BRANCHING.md#cutting-a-release). Add the new version's section here
in a PR into its `pre-release/vX.Y.Z` branch, before the candidate PR merges. Known issues
and open follow-ups are in [ISSUES.md](ISSUES.md).

## v0.1.0 (2026-10-23)

The first release, on [GitHub](https://github.com/TetengDev/automated-clipper/releases/tag/v0.1.0).
The clip pipeline and the review app work end to end for a URL you enqueue by hand. Automatic
detection of new uploads and the production deployment are not part of this release yet.

### What works

- **Enqueue and worker**: `clipper enqueue <url>`, then `clipper run-worker`, runs each job
  through its stages: fetch audio, transcribe, select, fetch clips, render. `clipper retry <job>`
  re-queues a failed or skipped job.
- **Transcription**: local faster-whisper (`large-v3-turbo`) by default, with word timestamps
  and Taglish support. Groq is optional (`transcriber: groq`). `clipper bench-transcribe`
  compares models on a sample (#23, CLP-35).
- **Highlight selection**: Claude picks highlights as structured output. On long sources, only
  the most engaging windows are sent, ranked by the YouTube heatmap and loudness (#21, CLP-36).
  Prompt v2 uses an editing playbook per content type and flags sensitive clips (#24, CLP-51).
- **Render**: Remotion renders 9:16 clips with animated captions and a hook card.
  - Clip edges snap to scene cuts, and dead air at the edges is trimmed (#22, CLP-68).
  - A face-tracked crop follows the speaker (#25, CLP-58).
  - Jump cuts inside a clip remove silences and stutters (EDL v2 segments) (#27, CLP-73).
  - The render stage turns fetched sections into `pending_review` clips (#19, CLP-63).
- **Quality gates**: every rendered clip is checked before review (#28, CLP-74).
  - Checks: captions match the words, no word cut at a join, speech starts early, loudness
    is -14 LUFS with true peak at or below -1 dBTP, and a tiny Whisper pass over each join.
  - A clip that fails gets the new status `needs_attention`, and the review page shows why.
    It can still be approved or rejected.
- **Review app**: login, queue with filters, clip detail with player, approve or reject
  with a reason, j/k navigation, and an audit trail.
- **Export**: approved clips are copied to `ready/<date>/` with `caption.txt` and
  `clip.json`, with an optional rclone sync to Drive.
- **CI and branching**: lint, tests, the Remotion project, the Docker build, and
  `pip-audit`/`npm audit`. Branches flow dev, then pre-release, then release, then main.
  After a release, the post-release tag and the main → dev sync run automatically (#18).

### Upgrade notes

- **Migrations 0003 to 0005** add columns to `clip_versions`: `playbook_version`, `sensitive`,
  `duration_s` and `gate_reasons`. They only add columns, and they run on start.
- **Local transcription is now the default.** The first run downloads models into
  `CLIPPER_MODELS_DIR`, which is a persistent volume in compose:
  - `large-v3-turbo`, about 1.6 GB;
  - `tiny`, about 75 MB, for the quality gates;
  - the face detector model.
- **Install with uv only.** `pyproject.toml` overrides scenedetect's `opencv-python` with
  `opencv-python-headless`, and pip does not honour that override.

### Not in this release

These commands exist, but they exit with "not implemented" for now:

- `clipper run-monitor` (CLP-24)
- `clipper resolve` (CLP-22)
- `clipper status` (CLP-33)

The VPS, Caddy and the deploy pipeline (epic E5) are not built yet.
