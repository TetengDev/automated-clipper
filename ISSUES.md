# Known issues

Known issues and limitations as of [v0.1.0](RELEASE.md#v010-2026-10-23). The Linear project
"Automated Clipper" (team CLP) is the source of truth. This file is a snapshot, updated with
each release. To report a problem, open a Linear issue, or a GitHub issue that links to one.

## Limitations of v0.1.0

- **No automatic detection.** New uploads are not picked up; each URL is enqueued by hand.
  Detection is CLP-22 to CLP-26 (YouTube, Twitch, live streams to VOD, the monitor loop).
- **No production deployment.** Provisioning, Caddy with HTTPS, and the release deploy are
  CLP-39 to CLP-41. Health checks, alerts, backups and budgets are CLP-38 and CLP-42 to
  CLP-45.
- **One worker only.** The queue has no leases or backoff yet, so running 2 workers is not
  safe (CLP-34).
- **No Discord notifications** (CLP-30), **no status page** (CLP-33), and **no comments,
  edit or regenerate** in the review app (CLP-27, CLP-28, CLP-32).

## Known issues

### Pipeline

- A clip that keeps failing to render fails the whole job, instead of being quarantined
  on its own (CLP-65).
- After a SIGKILL, the render waits without a bound, and the clip insert is not fenced on
  the job lease (CLP-66).
- The join check in the quality gates is not yet calibrated on Taglish, so it can flag
  good joins as `needs_attention`. Re-renders are not gated yet (CLP-80).
- The local Whisper model store does not self-heal a corrupt download (CLP-75). It also
  has gaps in concurrent access, retries and disk budget (CLP-76).
- The playbook still has gaps in how it identifies victims and minors in sensitive
  content. Check `sensitive` clips with care (CLP-77).
- Face reframe: the model is downloaded at runtime instead of being built into the image,
  and small faces are tracked poorly (CLP-78).
- Jump cuts: there is no check on migration names, a failure shows a noisy traceback, and
  there is no cut timeline in the UI (CLP-79).

### Security hardening

- Media decoders (ffmpeg, yt-dlp, Remotion) run with the same user as the worker and can
  read its secrets (CLP-71).
- Captions do not normalise floods of combining marks or private-use characters (CLP-67).

### Review app

- j/k navigation ignores the active queue filters and sort (CLP-62).
- There are no Playwright end-to-end tests for login, queue, approve and reject (CLP-61).

### CI and release

- There is no nightly job for the slow tests: the real tiny Whisper model, real scene
  detection and a Remotion render (CLP-72). There are also no Remotion snapshot tests
  (CLP-64).
- The Post-release workflow cannot open the main → dev sync PR until `SYNC_PR_TOKEN` is
  added to the `release-sync` environment. Until then, open it by hand with
  `gh pr create --base dev --head main` (see [docs/BRANCHING.md](docs/BRANCHING.md)).
