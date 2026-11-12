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
- Create the directory layout under `/srv/clipper`: `data/`, `output/`, `config/`, `.env` (with `chmod 600`).
- Run `chown -R 10001:10001 data output`. The containers run as uid 10001 and need write access.
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
video.

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
- **Downloads are verified**: after the fetch, the required files must be present and
  model.bin must match the pinned SHA-256; only then is `.clipper-complete` written into the
  snapshot. A snapshot without that marker is fetched again, and one that fails
  verification is discarded and fetched once more before the job retries later.
- **Image**: faster-whisper and its native wheels (CTranslate2, onnxruntime, PyAV) add
  about 320 MB to the image.

## Render failures

A clip whose render fails 3 times (`MAX_RENDER_FAILURES`: a Remotion or ffmpeg timeout, a
corrupt or missing section file) is left out. The job still reaches `clips_ready` with
the other clips, and the worker log lists each left-out clip with its last error. If no
clip renders, the job fails.

- Each failure is counted in `data/media/<job>/clips/<rank>/render_failures.json`. Clip
  failures use up these counts, not the job's attempts.
- When no clip rendered, the counts are dropped, so `clipper retry <job>` tries every clip
  again. A job that failed for another reason (e.g. its worker kept crashing) keeps them on
  `clipper retry`. To give such a clip fresh tries, delete its `render_failures.json` first.
- Outage worst case: when every render fails (Remotion broken, disk full), a job keeps
  trying for up to about 3 x clips x `CLIP_WORST_CASE_S` (the worst case of one clip)
  before it fails. With a single worker, every job queued behind it waits that long too.
  Watch the worker log for repeated `clip N failed` lines across jobs.

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
cannot be seen by the pipeline: the reviewer checks them before approving.

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
worker log. A failed send is retried with backoff.

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
  - disk above 80%
  - a budget cap being hit
  - YouTube quota above 80%
  - Twitch auth failure
  - export failure

  Alerts are deduplicated: each alert key fires at most once per 30 minutes.

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

See `docs/RUNBOOK.md` (written in E5-S8). It covers YouTube bot blocks, quota, stuck jobs, a full disk, budgets, Twitch auth, key rotation, rollback, restore, and adding a creator.
