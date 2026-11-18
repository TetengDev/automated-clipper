# Deployment Plan

## Infrastructure

- **Server:** Hetzner CPX41 (8 vCPU, 16 GB RAM; sized for Remotion's headless Chromium renders) running Ubuntu 24.04 LTS, with a 200 GB volume mounted at `/srv/clipper`.
- **DNS:** an A record for `clips.<domain>` pointing at the server.
- **Services (Docker Compose):**

| Service | Role |
|---|---|
| `monitor` | Polls YouTube and Twitch, enqueues jobs, runs the janitor and health checks |
| `worker` (1–2) | Runs the stage state machine (audio → transcript → selection → EDL → sections → Remotion render) |
| `review` | FastAPI review app |
| `caddy` | Reverse proxy, automatic HTTPS, basic_auth |

The services share a SQLite database (WAL mode) and a media volume. All of them use `restart: unless-stopped` and Docker healthchecks. Logs are JSON written to stdout.

## Provisioning (`scripts/provision.sh`, idempotent)

- Create a non-root `deploy` user. Allow SSH key login only; disable root login and password authentication.
- Configure `ufw` to allow only ports 22, 80, and 443. Install `fail2ban` and `unattended-upgrades`.
- Install Docker Engine and the compose plugin. Set the timezone to UTC.
- Create the directory layout under `/srv/clipper`: `data/`, `data/models/`, `output/`, `config/`, `.env` (with `chmod 600`).
- Run `chown -R 10001:10001 data output`. The containers run as uid 10001 and need write access. `data/models/` must exist before the first start: compose mounts it read-only into `review` and `monitor` (only the worker writes models), and Docker would create a missing one owned by root, which the worker could not write.
- `data/models/` (or `CLIPPER_MODELS_DIR`) holds the face-detection model. The worker fetches it once from Google's storage and checks its pinned SHA-256; without it, clips keep the blurred-fill layout.

## Secrets

`/srv/clipper/.env` exists only on the server and is never committed. It holds:

- `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
- `CLAUDE_CODE_OAUTH_TOKEN`, with `selector: claude_cli` (see [Highlight selection](#highlight-selection))
- `ANTHROPIC_API_KEY`, only with `selector: api`; `PEXELS_API_KEY`, `PIXABAY_API_KEY`
- `GROQ_API_KEY`, only with `transcriber: groq` (transcription runs locally by default; see [Local transcription](#local-transcription))
- `DISCORD_WEBHOOK_URL`, `DISCORD_ALERTS_WEBHOOK_URL`, `HEALTHCHECK_URL_*`
- `REVIEW_PASSWORD_HASH`, `SESSION_SECRET`, `RCLONE_REMOTE`
- `CLIPPER_ACCESS_TOKEN_<ACCOUNT_ID>` and `CLIPPER_REFRESH_TOKEN_<ACCOUNT_ID>` for each posting account (see [Posting accounts](#posting-accounts))
- `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET` for the YouTube publisher (see [YouTube publishing](#youtube-publishing))
- the restic credentials

GitHub Actions secrets contain only the SSH key, host, and user.

## Highlight selection

The select stage asks Claude for highlights in one of two ways (`selector` in
`config/creators.yaml`; it can be set per creator):

- **`claude_cli`**: runs the Claude Code CLI (`claude -p`, pinned in the image) on a Claude
  subscription. Calls are not billed per token. They count against the plan's usage limits.
  - On the server, run `claude setup-token` once on any machine where you are logged in.
    Put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. Locally, an existing `claude`
    login is used when the token is empty.
  - Each call gets no tools and loads no settings, plugins, MCP servers or CLAUDE.md. It
    runs in an empty temp dir, gets the prompt on stdin, and its env holds only the token
    (no other secrets).
  - When the plan's usage limit is hit, the job waits until the reset time the CLI reports
    (or retries hourly, for up to 48 h). The `spend` table records these calls with
    `usd = 0` and their tokens.
  - A rejected login fails the job. Run `claude setup-token` again, then `clipper retry <job>`.
  - Check your plan's terms before running this unattended on a server.
- **`api`**: the paid Messages API with `ANTHROPIC_API_KEY`, capped by `daily_llm_usd_cap`.

## yt-dlp

yt-dlp needs a JavaScript runtime to solve YouTube's player challenges; without one it
falls back to a client that is deprecated and misses formats. The dependency is
`yt-dlp[default,pin-deno]`, so `uv sync` (and the image) installs:

- `yt-dlp-ejs`, the challenge solver scripts.
- Deno, from the `deno` wheel that the Deno organisation publishes on PyPI. It lands in the
  venv's `bin/` (`/opt/venv/bin/deno` in the image), which yt-dlp searches before `PATH`.
  Nothing extra to install on macOS or the server.

yt-dlp pins the exact version of both, so `uv lock --upgrade-package yt-dlp` moves all three
together. That matters for Deno: its V8 runs JavaScript downloaded from YouTube, so it should
not fall behind on security fixes.

yt-dlp counts any Deno stderr output as a failed solve. In the image, `DENO_DIR=/tmp/deno`
puts Deno's cache on the tmpfs (the root filesystem is read-only; Deno 2.9.8 tolerates an
unwritable cache silently, but a later version might warn) and `DENO_NO_UPDATE_CHECK=1`
stops the update check and its notice. CI runs Deno in the read-only container and fails
on any stderr. Both names are on the child-process allowlist in
`pipeline/ytdlp.py`; no other `DENO_*` variable reaches yt-dlp or Deno.

yt-dlp's own cache defaults to `~/.cache/yt-dlp`, which is read-only in the container, so
every write failed there. The image sets `CLIPPER_YTDLP_CACHE_DIR=/tmp/yt-dlp`, which
becomes `--cache-dir` on every yt-dlp call: the cache lives on the tmpfs, is empty after a
restart, and never touches the data volume. Unset (macOS, dev), yt-dlp keeps its default.
It is a flag rather than `XDG_CACHE_HOME` because that variable would also move the caches
of Hugging Face, matplotlib, fontconfig and the Claude CLI, in the main process and its
children.

What the cache buys with yt-dlp 2026.10.21 is small. Only solved signature functions
(`youtube-sigfuncs`, about 450 bytes each, keyed by player and signature length) are
stored on disk. The `n` challenge results are kept in memory for one call only, and
yt-dlp has turned off its on-disk player cache, so Deno still runs once per probe and per
download ([CLP-130](https://linear.app/tengdev/issue/CLP-130)). A cache hit only drops the
signature part of that Deno run. Check with `-v`: `Loading youtube-sigfuncs... from cache`
followed by `Solving JS challenges using deno`.

Optional settings in `.env`:

- `CLIPPER_YTDLP_COOKIES`: path to a `cookies.txt` for members-only or age-gated videos
  (see `allow_gated_content`).
- `CLIPPER_YTDLP_PROXY`: proxy URL for every yt-dlp call. Its password is masked in logs.

Security: yt-dlp runs Deno with no `--allow-*` flags, so the challenge script gets no file,
network, environment or subprocess access; `--no-prompt` turns a permission request into an
error, `--no-config` ignores config files, and `--no-remote --no-npm --cached-only` stop it
from fetching code. yt-dlp puts the proxy URL, credentials included, in Deno's
`HTTP(S)_PROXY`; without `--allow-env` the script cannot read it. The residual risk is a V8 escape running as uid 10001, the same as
ffmpeg decoding untrusted media ([CLP-71](https://linear.app/tengdev/issue/CLP-71)).

Downloads failing with "Requested format is not available" across many videos usually means
the runtime is broken: run `docker compose run --rm --no-deps worker doctor` first (it runs
the real solver scripts through Deno in the worker's sandbox), then yt-dlp with `-v` and
look for `Solving JS challenges using deno`.
The slow test `CLIPPER_SLOW_TESTS=1 uv run pytest -k real_probe` checks this against a live
video. A breakage that hits every video is retried until it is fixed. The monitor gives up
on a video (records it `unavailable`) after it has had format errors in 3 polls where other
videos from the same creator probed fine: an upload with no audio, for example. To fetch
such a video anyway, enqueue it by hand. That count is kept in memory only, so a monitor
restart (a deploy) sets it back to 0. Only a new upload probed in the same poll counts as
a sibling that probed fine, so the count grows with the creator's upload rate: about 3
weeks for a weekly uploader.

The bitrate cap on the audio download's last fallback leaves one gap. A video whose only
formats are muxed, all above 480p and 3000 kbps, passes the probe, but its audio download
finds no format. The worker defers it like a player breakage and fails it after 48 h,
which costs about 96 attempts, each a yt-dlp probe and download.

## Local transcription

The worker transcribes with faster-whisper on CPU (`transcriber: local`, the default).

- **Models**: only the pinned names in `clipper.config.WHISPER_MODELS` are accepted (tiny,
  base, small, medium, large-v3, large-v3-turbo), each at a fixed Hugging Face commit. The
  first job downloads the snapshot into `CLIPPER_MODELS_DIR` (`/app/data/models` on the
  data volume in compose), which needs outbound HTTPS to huggingface.co once. Download
  size: tiny 75 MB, medium 1.5 GB, large-v3-turbo 1.6 GB, large-v3 3.1 GB. After that,
  transcription runs offline (`HF_HUB_OFFLINE=1`).
- **Isolation**: each 10 min chunk runs in its own child process (`python -I -m
  clipper.pipeline.local_asr`), one at a time, with a hard timeout, an allowlisted
  environment (no API keys) and a 12 GiB address-space limit. The model is loaded per chunk
  (a few seconds from the page cache) and its memory is freed when the child exits.
  `HF_ENDPOINT`, `HF_TOKEN` and proxy variables from `.env` never reach the download
  child; it always uses `https://huggingface.co` without a token, and the pinned commit
  is checked by the Hub client.
- **RAM** (peak RSS of one child, int8, measured in the image): tiny 0.3 GB, medium 2.9 GB,
  large-v3-turbo 2.6 GB, large-v3 5.1 GB. The worker's `mem_limit: 8g` fits any of them.
- **CPU**: `whisper_cpu_threads` (default 4) threads per child. Keep it at or below the
  worker's `cpus: 6` quota.
- **Speed** (real-time factor = wall time / audio length, model load included, int8,
  4 threads, 61 s of English speech, measured 2026-10-22):

  | Model | Apple M-series, native | Same Mac, Docker (arm64) |
  |---|---|---|
  | tiny | 0.03 | 0.03 |
  | medium | 0.34 | n/a |
  | large-v3-turbo | 0.30 | 0.23 |
  | large-v3 | 0.64 | 0.49 |

  Expect a shared VPS vCPU to be roughly 2–3x slower (an estimate, not measured). **Use `large-v3-turbo` on
  the VPS**: large-v3 can approach real time there. Each 10 min chunk may run for
  `max(15 min, 3 x chunk length)` = 30 min (`LOCAL_RTF_BUDGET`), within the stage's
  100 min budget; a run that hits the budget resumes from the cached chunks.
- **Downloads are verified**: every file of the snapshot is pinned by size and SHA-256
  (`WHISPER_MODELS`). After the fetch all of them must match; only then is
  `.clipper-complete` written into the snapshot. A snapshot without that marker is fetched
  again, and one that fails verification is discarded and fetched once more before the job
  retries later. A file with the wrong bytes also loses its blob, so the next fetch
  downloads it instead of linking the same bytes again.
- **Self-heal**: before each job a marked snapshot is checked cheaply (every file's size,
  the small files' hashes, model.bin's blob name, no unpinned files, no links out of the
  models dir), so a truncated blob or a planted file is fetched again. If CTranslate2
  still fails to load the weights, the worker removes the marker and the next attempt
  fetches and fully hashes the snapshot. A setup that can never load (such as a compute
  type the CPU lacks) fails the job without touching the marker. Model files that are
  missing when a chunk starts are retried the same way.
- **One process at a time**: checking, fetching, healing and marking a snapshot run under
  an exclusive `flock` on `<models dir>/.lock`, so the worker and an operator's
  `clipper bench-transcribe` never fetch or delete under each other. The second one waits
  (up to its fetch budget, then the job retries later) and then finds the snapshot ready.
  `flock` only works on a local filesystem: keep the models dir on a local disk (or a
  Docker volume or bind mount of one), never on NFS shared across hosts.
- **Old revisions are pruned**: when a pin moves to a new commit, the first job after the
  deploy fetches it and, still under the lock, deletes every snapshot no pin names, then
  the blobs only those snapshots used. Pruning runs in the worker's transcribe stage, after
  a verified fetch, not in the janitor: the monitor mounts the models dir read-only, so
  only the worker can write it. `bench-transcribe` and the quality gate never prune, so a
  run from a checkout with other pins cannot delete the worker's model. A model dropped
  from `WHISPER_MODELS` stays on disk until the next fetch of any model. Pruning never
  follows a symlink, never touches anything
  outside the models dir, and logs and skips a path it cannot delete. If it cannot read a
  snapshot or blobs dir, it deletes no blobs that run.
- **Needs symlinks**: the snapshot files are symlinks into the Hub client's blob store, and
  the cheap check relies on them. Keep the models dir on a filesystem with symlinks (ext4,
  APFS, a Docker volume or bind mount of one). Without them the Hub client copies the
  files, the check never passes, and every job downloads the model again.
- **Image**: faster-whisper and its native wheels (CTranslate2, onnxruntime, PyAV) add
  about 320 MB to the image.

## Render failures

A clip whose render fails 3 times (`MAX_RENDER_FAILURES`: a Remotion or ffmpeg timeout, a
corrupt or missing section file) is left out. The job still reaches `clips_ready` with
the other clips, and the worker log lists each left-out clip with its last error. If no
clip renders, the job fails.

- Each failure is counted in `data/media/<job>/clips/<rank>/render_failures.json`. Clip
  failures use up these counts, not the job's attempts.
- `clipper retry <job>` deletes the job's counts, so every clip gets fresh tries. It finds
  them under `--media-root` (default `data/media`, or `CLIPPER_MEDIA_DIR`), so pass the
  worker's media directory if it is not the default. `clipper enqueue --retry` keeps the
  counts: delete `render_failures.json` by hand to give a left-out clip fresh tries. When no
  clip rendered, the job drops its counts itself.
- Outage cap: when every render fails (Remotion broken, disk full), a job keeps trying for
  up to about 3 x clips x `CLIP_WORST_CASE_S` (the worst case of one clip). With a single
  worker, every job queued behind it waits too. So a job whose renders still fail 48 h
  into an episode of failed renders fails. `.render_failing_since` in its media directory
  holds when the episode began and its last failed render. After 48 h without a failed
  render the episode is over, so a job picked up late by a stopped worker, or requeued
  later, starts a new one. A run in which every render works is never stopped by the cap.
  Watch the worker log for repeated `clip N failed` lines across jobs.
- Two workers on the same clip (one of them after losing its lease) can overwrite each
  other's update to `render_failures.json`, so the count can come out one too low. That
  only means an extra retry; the number of tries is still bounded.

## Budgets

- `daily_llm_usd_cap` and `daily_transcribe_usd_cap` cap the paid API calls per UTC day.
- `max_source_hours_per_day` caps the source hours each creator downloads per UTC day
  (a creator's `overrides` can change it). Each job counts its source length once, before
  the audio download, as a free `source` row in the `spend` table. A source whose length
  the probe does not report is counted after its download, once ffprobe has measured it.
- Over a cap the stage waits until the next UTC midnight and does not use up an attempt. A
  single call or source larger than the whole cap can never fit, so its job is skipped.
- Hitting a cap sends no alert yet. Alerting on it is tracked in
  [CLP-30](https://linear.app/tengdev/issue/CLP-30).

## Janitor

The `monitor` runs the janitor after a poll cycle, at most once an hour. It deletes only
inside the media dir (`data/media`, or `--media-dir` / `CLIPPER_MEDIA_DIR`, the same as
the worker's).

- Clips still waiting for review `review_expiry_days` (default 7) after their current
  version was created become `archived`. A re-render that returns a clip to review starts
  the wait over.
- `clips_ready` jobs lose their source media `media_retention_hours` (default 48) after
  they became ready: `audio_16k.flac`, leftover downloads, and each clip's downloaded
  section (`clips/<rank>/source.mp4`). Rendered clips, transcripts and the other small
  files stay.
- Rejected and archived clips lose their clip dir `rejected_retention_days` (default 14)
  after the decision. Their database rows and history stay.
- The disk budget counts the media dir plus the models dir (`CLIPPER_MODELS_DIR`), so set
  `disk_budget_gb` with room for the models too (large-v3 is about 3 GB, large-v3-turbo
  about 1.6 GB). The janitor only reads the models dir (compose mounts it read-only in
  `monitor`) and never deletes models; the worker prunes old model revisions (see
  [Local transcription](#local-transcription)).
- When the media dir and models dir together reach 80% of `disk_budget_gb`, an alert goes
  to the alerts webhook,
  once per crossing; it is sent again only after usage drops below 80% (or after a
  monitor restart). Above the budget, the source media of `clips_ready` jobs is deleted
  too, oldest job first, until usage is back under the budget.
- Never deleted: the files of `approved`, `exported`, `changes_requested` and
  `re_rendering` clips, any file a post uses, and anything of a job that is not
  `clips_ready` (a worker may be using it). Failed and skipped jobs keep their files;
  `clipper retry` reuses them.
- Nothing outside the media dir is deleted. The janitor never follows a symlink and never
  deletes one, and it removes only regular files and empty dirs.

## Sensitive clips and the review app

Every clip of a `public_service` creator, of a creator without a `content_type`, or of a
creator not in `config/creators.yaml` (e.g. a manual `clipper enqueue`) is sensitive. The
reviewer must tick "I checked faces of minors and victims" before approving it, because the
model sees only the transcript.

- The `review` service reads `config/creators.yaml` once, at startup, and does not start
  without it. The `worker` also reads it once, at startup. After editing a creator's
  `content_type`, restart both (`docker compose restart worker review`).
- Until `review` is restarted, a creator newly set to `public_service` gates only clips
  stored as sensitive (by a restarted worker). Its older clips need the confirmation only
  once `review` has been restarted.

## Posting accounts

`config/accounts.yaml` lists the accounts that approved clips are posted to. Each account
names one platform (`tiktok`, `instagram`, `youtube` or `facebook`), its handle, its market
and timezone, the creators whose clips it may post, and its posting plan. Set
`CLIPPER_ACCOUNTS` to read the file from somewhere else; by default it sits next to
`creators.yaml`. Without the file nothing is posted and the rest of the pipeline runs as
before.

To add an account:

1. Add an entry to `config/accounts.yaml`. The `id` is a lower-case slug (letters, digits,
   underscores) that never changes, because the token names are derived from it. No id may
   end another id after an underscore (`clips_tt` next to `mrbeast_clips_tt`), since their
   token names would be ambiguous.
2. Choose the plan. `posts_per_day`, `min_gap_minutes` and `windows` can be left out for
   the US and PH markets, and then the research defaults from
   [CLP-90](https://linear.app/tengdev/issue/CLP-90) apply. Other markets must set all
   three. Fields that are set are kept, and only the missing ones are filled in. Windows
   are `HH:MM-HH:MM` in the account's own timezone, may not overlap, and a post may go out
   at any minute from a window's start through its end. The plan must fit:
   `posts_per_day` posts, `min_gap_minutes` apart, have to fit inside the windows of one
   local day.
   `posts_per_day` may not exceed the platform cap (TikTok 15, Facebook 30, Instagram 50,
   YouTube 100). `warmup_start` ramps a new account from 1 post a day up to
   `posts_per_day` over `warmup_days` (14 by default).
3. Check the licence rule. A creator whose `content_type` is `public_service` or
   `vlog_lifestyle`, or who has no `content_type`, can only be assigned to an account that
   sets `licence:` to a reference to the signed licence. Without it, `clipper
   check-config` fails and names the creator and the account. One licence covers one
   creator, so an account can hold at most one creator that needs a licence. Raffy Tulfo in Action, Ivana
   Alawi and Cong TV are paused until they are licensed.
   Quote country codes and names that YAML could read as something else, such as
   `market: "NO"` (a bare `NO` is read as false).
4. Put the tokens in `/srv/clipper/.env`, never in YAML. The names are
   `CLIPPER_<KIND>_<ACCOUNT_ID>`, upper-cased. For the account `mrbeast_clips_tt`:

   ```sh
   CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT=...
   CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT=...
   ```

   Any other value a platform needs follows the same pattern, for example
   `CLIPPER_CLIENT_SECRET_MRBEAST_CLIPS_TT`. Only exact upper-case names count, and the
   app's own `CLIPPER_*` settings are never read as tokens; an account id that would make
   one of them look like a token (such as `id` or `proxy`) is refused. The values are read
   into secret strings, so they never appear in logs or error messages.
5. Run `clipper check-config` (`docker compose run --rm worker clipper check-config` on the
   server). It validates the file and prints one line per account with its platform,
   handle, market, posts a day, windows and timezone. For each token it prints only `set`
   or `missing`, never the value. It exits non-zero on any error.

The tokens sit in `.env` like the other keys. Encrypting refresh tokens at rest is a
planned follow-up.

## YouTube publishing

The worker posts to YouTube itself, through the free YouTube Data API v3
(`src/clipper/posting/youtube.py`, [CLP-93](https://linear.app/tengdev/issue/CLP-93)). It
registers the YouTube publisher when `accounts.yaml` has at least one enabled `youtube`
account. If `accounts.yaml` is invalid, the worker stops at start with the error (exit
code 1); fix the file and start it again. Only clips a reviewer approved are ever
scheduled.

How it posts:

- Each due post is uploaded as `private` with `status.publishAt` set to the post's
  scheduled time, so YouTube makes it public. A late retry has a `publishAt` in the past,
  and YouTube publishes such a video right away. Vertical videos up to 3 minutes become
  Shorts automatically.
- The title is capped at 100 characters. The description is the caption plus up to 5
  hashtags, within 5000 bytes. The category is 24 (Entertainment), the video is declared
  not made for kids, and `containsSyntheticMedia` is set when the clip has an AI element.
  Hashtags the caption already ends with are not added twice.
  Each upload carries a hidden `clipper-<idempotency key>` tag. After a timeout or a crash,
  the worker uses that tag to find the earlier upload and records it instead of uploading
  the video twice.
- A post that needs the paid-partnership disclosure (a campaign with `paid_partnership:
  true`) is refused before upload and turns `post_failed` with an alert: the worker does
  not set YouTube's paid-promotion disclosure. Post such a clip by hand in YouTube Studio
  with the disclosure on.
- Quota: one upload costs 1 unit of the separate "Video Uploads" bucket, which allows 100
  calls a day. A `quotaExceeded` or `uploadLimitExceeded` error moves the post to just
  after the next quota reset (midnight Pacific time). It does not count as a failed attempt.
- Tokens: the worker turns the account's refresh token into a short-lived access token
  and refreshes it when it expires. If Google rejects the refresh token (`invalid_grant`,
  for example because it was revoked) or the OAuth client, the account is **paused**. Its
  posts are pushed back an hour at a time without counting attempts, and one alert goes
  out. Put a new refresh token in `.env` and restart the worker
  (`docker compose up -d worker`). Posting resumes on the next pass.
- Posts for a YouTube account that is set to `enabled: false`, or that is no longer in
  `accounts.yaml`, are also pushed back an hour at a time, with one alert. When no
  YouTube account is enabled at all, the worker registers no YouTube publisher, and those
  posts stay `scheduled`. Cancel them (`clipper posts cancel`) or enable the account again.

### One-time setup (the owner)

1. **Google Cloud project.** At <https://console.cloud.google.com>, create a project (for
   example `clipper-publisher`). Under *APIs & Services → Library*, enable **YouTube Data
   API v3**.
2. **OAuth consent screen** (*Google Auth Platform → Branding / Audience / Data access*):
   - User type **External**. Enter the app name, the support email, the developer contact
     email, the home page and the **privacy policy URL**. You need both pages later for
     verification.
   - Under *Data access*, add the scopes `https://www.googleapis.com/auth/youtube.upload`
     (upload) and `https://www.googleapis.com/auth/youtube.readonly` (the duplicate-upload
     check lists the channel's recent uploads). `youtube.readonly` can read the whole
     account, including private videos and playlists. It is the narrowest scope that lets
     the worker list its own uploads, and without it a timed-out upload could be posted
     twice.
   - Under *Audience*, click **Publish app** to move it from *Testing* to **In
     production**. A *Testing* app's refresh tokens expire after 7 days, and the accounts
     would then pause every week. A production app that is not yet verified still works
     for the owner: the consent screen shows "Google hasn't verified this app", and you
     continue through *Advanced → Go to … (unsafe)*.
3. **OAuth client.** Under *Clients → Create client*, pick type **Web application** and
   add the authorised redirect URI `https://developers.google.com/oauthplayground`. Put
   the client id and secret in `/srv/clipper/.env`:

   ```sh
   YOUTUBE_OAUTH_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   YOUTUBE_OAUTH_CLIENT_SECRET=GOCSPX-...
   ```

4. **Refresh token, once per YouTube account.**
   1. Open <https://developers.google.com/oauthplayground>. Under the gear icon, tick
      **Use your own OAuth credentials** and paste the client id and secret. Tokens minted
      with your own client do not expire the way the Playground's default client's do.
   2. Under *Input your own scopes*, enter
      `https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly`
      and click **Authorize APIs**.
   3. Sign in with the Google account that owns the channel. If it manages several
      channels (brand accounts), pick the channel of this `accounts.yaml` entry.
   4. Click **Exchange authorization code for tokens** and copy the **refresh token**.
      Put it in `.env` under the account's id, for example for `mrbeast_clips_yt`:

      ```sh
      CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_YT=1//0g...
      ```

   5. Under the gear icon, remove your client credentials from the Playground and close
      the tab. The refresh token was on that page, so clear the browser history for it.
   6. After the last account has its token, go back to the OAuth client and **delete the
      Playground redirect URI**. Refresh tokens keep working without it. While it is
      registered, anyone who gets hold of the client secret could use the Playground to
      show this app's consent screen to a channel owner and collect a token. Add it again
      only for the next re-authorisation.
   7. Run `clipper check-config`: the account should show `refresh_token=set`. Then
      restart the worker.

   An access token is not needed: the worker mints its own. A changed OAuth client id or
   secret also needs a worker restart.
5. **Verification, in parallel.** In *Google Auth Platform → Verification centre*,
   submit the app for verification of the sensitive scopes above. You need the home page
   and privacy policy on a domain you verified in Search Console, a written reason for
   each scope ("uploads the channel owner's approved clips; reads the channel's recent
   uploads to avoid duplicates"), and an unlisted YouTube demo video. The video shows the
   consent screen with the app name and the scopes, then an upload as it appears on the
   channel. Until verification is done, the owner's own channels work as described in
   step 2, but no more than 100 users can authorise the app.
6. **First live upload: check it by hand.** The current `videos.insert` docs say videos
   uploaded from unverified API projects are *not* restricted to private. Older revisions
   said they were locked private. After the first scheduled post, check in YouTube Studio
   that the video turns public at its `publishAt` time. If it stays private with a
   "locked" notice, pause posting to YouTube (`enabled: false` on the account) until
   verification or the API audit is done.

Never enable an error reporter that records local variables in the worker (Sentry's
`include_local_variables`, rich's `show_locals`). While a token request runs, the refresh
token and the client secret are local variables inside httpx.

To revoke access, remove the app at <https://myaccount.google.com/permissions> for that
Google account. Its account then pauses on the next post, with an alert.

## Campaigns

Paid clipping campaigns live in `config/campaigns.yaml` (or the file named by
`CLIPPER_CAMPAIGNS`), one entry per campaign brief. An account joins one with
`campaign: <id>` in `accounts.yaml`; only the campaign creator's clips count for it.

1. Copy the rules from the live brief on the program's site: length, required hashtags,
   forbidden elements and keywords, the audience rule, the submission deadline, the
   payout terms and the end date. The shipped entries are placeholders from the epic's
   research notes, marked `OWNER TODO`. Do not guess a rule the brief does not state.
2. Set `brief_verified: true` only after that check. While it is false, every post for
   the campaign is **held**.
3. Run `clipper check-config`. It prints one line per campaign. It fails when an account
   cannot be assigned to its campaign: the platform is not one the campaign takes, the
   account does not post the campaign's creator, or the account's `market` is not one of
   the audience rule's markets (a PH account cannot deliver a US audience).

When a clip is approved, each campaign account's post goes through a pre-flight: the
platform limits, the brief's length range, its required hashtags (added to the post
automatically), its forbidden keywords, the paid-partnership toggle, the end date, and
AI content when the brief forbids it. AI content is read from the render sidecar; a clip
without one is held under an AI ban, because nothing shows it is free of AI. A post that
fails is `held` with the reason on the review app's Scheduled page and in
`clipper posts list`, and it is never published. Logos, negative framing and reposts
cannot be seen by the pipeline: the clip page lists them for each campaign an enabled
account posts the creator for (a disabled campaign included), and the reviewer must tick
"I checked these in the video" before approving. The approval's history row records them.

A post for an account outside any campaign goes through the platform part of the same
check, at scheduling and again just before it is published: the platform's length, title,
caption and hashtag limits, generic hashtags, disclosure-like, malformed or mixed-script
tags on its hashtag list, and generic, disclosure-like (`#ad`, `#sponsored`) or
mixed-script tags written into the title or caption. A post that fails is held the same
way. Links, mentions and too few hashtags hold only campaign posts. Such inline tags are
also stripped from the model's titles and captions, from the per-platform text and from
the export package, so in practice the check catches only text stored before that cleaning
existed. A `#` inside a URL (`https://site.com/page#fyp`) is not stripped: the URL is left
whole and judged by the link rules, which hold campaign posts only, but a disclosure-like
one (`https://site.com/page#ad`) still holds a plain post for a person to look at. After a
mention or a bare domain (`@bob/#ad`, `site.com/#ad`) a `#` starts a tag as usual.

A bare domain counts as a link only when it ends in a real top-level domain from the IANA
root-zone list written in ASCII, in any case (`Scam.Rocks`, `scam.xn--p1ai`), so a sentence
break or a file name such as `Node.js` or `index.html` is not flagged. A TLD in another
script (`.рф`, `.みんな`) counts only in a link written with `https://` or `www.`, or after
a deliberately obfuscated dot (`name (dot) онлайн`, `name[.]みんな`, `name d0t рф`): Chinese
and Japanese end sentences with `。`, which reads as a dot, and words such as 中国 or みんな
are TLDs, so `我爱你。中国！` must not read as a domain. A capitalised TLD word glued after a
dot, as in `this.So`, is flagged; that is the cost of catching `Scam.Rocks`. A file name
whose extension is also a TLD (`notes.md`, `clip.mov`, `setup.zip`) is flagged, since it is
a working domain too; a campaign that needs one lists it in `allowed_links`. A spoken "dot"
counts only before a TLD that is not an everyday word (`name dot com`, but not
`scam dot rocks`). Warnings show the link as it was typed, so the reviewer can find it in
the text. The list is vendored in `src/clipper/tld_data.py`; refresh it with
`uv run python scripts/gen_tld_data.py` when IANA adds TLDs (it refuses a list older than
the vendored one).

Generic and disclosure tags are also caught as lookalikes written in one script, such as
all-Cyrillic `#аԁ`, the small capitals `#ᴀᴅ` or `#ꜰʏᴘ`, Greek capitals `#ϜΥΡ`, accented
`#àd`, digits, underscores or a capital I (`#v1ral`, `#co11ab`, `#f_y_p`, `#viraI`), or a
Latin tag padded with Japanese or Chinese (`#ad一`). The comparison uses the Unicode
confusables table (UTS #39) and the Unicode script data, vendored in `src/clipper/hashtag_data.py` for
Unicode 15.0 (Python 3.12). After a Python upgrade, regenerate it with
`uv run python scripts/gen_hashtag_data.py`; a test fails until the versions match.

The worker runs the same check again just before it publishes, with the config as it is
on disk (reloaded when `creators.yaml`, `accounts.yaml` or `campaigns.yaml` changes), so a
campaign that was disabled, ended or tightened after scheduling (or a post rescheduled
past the campaign's end) is held instead of posted. If the config is invalid, due
campaign posts are held until it is fixed. A held post keeps its file reserved on its platform, so
no other account on that platform gets the same file meanwhile.

After fixing the config, `clipper posts recheck` runs the pre-flight again, on the clip's
current version, and schedules the held posts that now pass; the others keep a hold with
the current reason. Otherwise cancel them on the Scheduled page or with
`clipper posts cancel <id>`. Cancelling is final for that clip on that account.

### Link submission

Vyro, Reach and Whop pay only for links submitted on their sites, and Reach wants them
within 30 minutes of posting. None of them documents a free submission API, so the
submit itself is a manual step: the clipper never scrapes these sites or logs in to them.

1. When a campaign post goes live, the worker records a submission and pings
   `DISCORD_WEBHOOK_URL` with the link, the campaign, the deadline (posted time plus the
   brief's `submit_within_minutes`) and the command to mark it done.
2. Submit the link on the program's site.
3. Mark it submitted on the review app's Campaigns page or with
   `clipper campaigns submit <id>`. The submission time is recorded, and a submission
   after the deadline is flagged late.

The worker sends a reminder 10 minutes before the deadline. When the deadline passes
without a submission, it sends one alert to `DISCORD_ALERTS_WEBHOOK_URL`, or to the ping
webhook when no alerts webhook is set. Without either webhook, the messages go to the
worker log. When Discord is unreachable, rate-limits the webhook (429) or returns a 5xx,
the send is retried with backoff, waiting as long as Discord's `Retry-After` asks, up to
10 minutes. A 401, 403, 404 or a redirect means the webhook itself is broken (revoked,
deleted or misrouted): the worker logs an error asking you to fix the webhook URL and
keeps every message queued, retrying with the same backoff. When Discord refuses one
message with any other 4xx (such as 400), that message is not retried: the worker logs
an error, and the link stays on the Campaigns page.

When a program reports approved views and the payout for a submission, record them with
`clipper campaigns earnings <id> --views N --payout 12.50`. `clipper campaigns rpm` and
the Campaigns page show RPM (payout per 1,000 approved views) per account and per
campaign. `clipper campaigns list` lists every submission.

## CI/CD

- **`ci.yml`** runs on every PR and on pushes to main: ruff, pytest (unit and integration against fixtures), Docker build, and Playwright tests for the review app.
- **`release.yml`** runs on a `v*` tag:
  1. Build the image and push `ghcr.io/<user>/clipper:<tag>`.
  2. SSH to the server and run `scripts/deploy.sh <tag>`.
- **`deploy.sh`**:
  1. Record the previous tag.
  2. Pin the new tag.
  3. Run `docker compose pull && up -d`.
  4. Poll `/healthz` and container health for 120 s.
  5. If the checks fail, redeploy the previous tag and exit non-zero.
- **`nightly-rebuild.yml`**:
  1. Rebuild the image with the latest yt-dlp.
  2. Smoke-test a metadata fetch and a 10 s section download.
  3. If the smoke test passes, auto-tag a patch release. If it fails, send a Discord alert.
- **`nightly-slow.yml`** runs the `slow` and `render` tests that normal CI skips, every night against dev, on manual dispatch, and on a PR that changes the workflow file:
  1. Install ffmpeg, Node, the render dependencies and Chrome headless shell.
  2. Fetch the pinned `tiny` Whisper model and the face model.
  3. Run the tests. Any skipped test fails the run. The live YouTube probe is left out, because YouTube often blocks GitHub runner IPs.
  4. On failure, GitHub sends its failed-run notification. For a scheduled run, GitHub emails only the person who last edited the workflow's cron line.
- **Migrations** run when a service starts. They are additive only, so rolling back is always safe.

## Environments

- **local:** `docker compose` with an override file and a fixture mode. Use `clipper enqueue <url>` for manual runs.
- **prod:** the VPS. There is no staging environment in v1. New features roll out behind config flags, one creator at a time.

## Rollout sequence

1. Run the monitor only, in baseline mode, for 24 h. Verify that detection works.
2. Enable the worker for MrBeast only for 2 days. Review the clips and tune the prompt.
3. Add the PH creators (Taglish validation), then the streamers (Kai Cenat, IShowSpeed), with budgets in place.
4. Run a 48 h soak with all 6 creators, then 7 days unattended. v1 launches after that.

## Monitoring & alerts

- **Heartbeats:** monitor and worker each ping Healthchecks.io every 5 minutes, with a 15-minute grace period before an alert.
- **Discord alerts** fire on:
  - a job reaching `failed`
  - the `DownloadBlocked` signature
  - the media and models dirs at 80% of `disk_budget_gb` (see [Janitor](#janitor))
  - YouTube quota above 80%
  - Twitch auth failure
  - export failure

  Alerts are deduplicated: each alert key fires at most once per 30 minutes. The disk
  alert fires once per crossing of 80%.

  A budget cap being hit sends no alert yet; the deferred job's reason is in the worker
  log and on the job. Alerting on it is tracked in
  [CLP-30](https://linear.app/tengdev/issue/CLP-30).

## Backups

- **Nightly:** take a SQLite `.backup`, then run `restic` to a Hetzner Storage Box or S3. The backup covers the database, `config/`, and `cookies.txt` (encrypted).
- **Retention:** 14 daily and 8 weekly snapshots.
- **Not backed up:** raw media, because it can be regenerated. Approved exports are synced to Drive.
- **Restore drill:** restore onto a laptop and confirm that `clipper status` reports the expected counts.

## Rollback

`scripts/deploy.sh <previous-tag>`. Because migrations are additive, the older code still runs against the newer schema.

## Runbook

Run `docker compose run --rm --no-deps worker doctor` after every image update and before
filing a download bug (`--no-deps`: do not start the `migrate` service first). It needs no
config or database and prints one line per tool (ffmpeg, ffprobe, the ffmpeg filters the
pipeline uses, yt-dlp, yt-dlp-ejs with yt-dlp's version and hash checks, Deno with a real
challenge-solver run, node), then exits 1 if any check failed. Locally: `uv run clipper
doctor`.

`run-worker` and `run-monitor` also run the yt-dlp-ejs and Deno checks once at boot
([CLP-129](https://linear.app/tengdev/issue/CLP-129)). A failure is logged at ERROR with the
same hint `doctor` prints and sent to the alerts webhook (`DISCORD_ALERTS_WEBHOOK_URL`, else
`DISCORD_WEBHOOK_URL`); the service keeps running, because yt-dlp still downloads without the
solver, just with fewer formats. A passing check logs one INFO line. A malformed webhook URL
stops the worker but not the monitor, which logs the variable name and keeps detecting.

See `docs/RUNBOOK.md` (written in E5-S8). It covers YouTube bot blocks, quota, stuck jobs, a full disk, budgets, Twitch auth, key rotation, rollback, restore, and adding a creator.
