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

## YouTube publishing

The worker posts to YouTube itself, through the free YouTube Data API v3
(`src/clipper/posting/youtube.py`, [CLP-93](https://linear.app/tengdev/issue/CLP-93)). It
registers the YouTube publisher when `accounts.yaml` has at least one enabled `youtube`
account. If `accounts.yaml` is invalid, the worker logs `posting is off` and runs the
rest of the pipeline. Only clips a reviewer approved are ever scheduled.

How it posts:

- Each due post is uploaded as `private` with `status.publishAt` set to the post's
  scheduled time, so YouTube makes it public. A late retry has a `publishAt` in the past,
  and YouTube publishes such a video right away. Vertical videos up to 3 minutes become
  Shorts automatically.
- The title is capped at 100 characters. The description is the caption plus up to 5
  hashtags, within 5000 bytes. The category is 24 (Entertainment), the video is declared
  not made for kids, and `containsSyntheticMedia` is set when the clip has an AI element.
  Each upload carries a hidden `clipper-<idempotency key>` tag. After a timeout or a crash,
  the worker uses that tag to find the earlier upload and records it instead of uploading
  the video twice.
- Quota: one upload costs 1 unit of the separate "Video Uploads" bucket, which allows 100
  calls a day. A `quotaExceeded` or `uploadLimitExceeded` error moves the post to just
  after the next quota reset (midnight Pacific time). It does not count as a failed attempt.
- Tokens: the worker turns the account's refresh token into a short-lived access token
  and refreshes it when it expires. If Google rejects the refresh token (`invalid_grant`,
  for example because it was revoked) or the OAuth client, the account is **paused**. Its
  posts are pushed back an hour at a time without counting attempts, and one alert goes
  out. Put a new refresh token in `.env` and restart the worker
  (`docker compose up -d worker`). Posting resumes on the next pass.

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
     check lists the channel's recent uploads).
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

   5. Under the gear icon, remove your client credentials from the Playground. Run
      `clipper check-config`: the account should show `refresh_token=set`. Then restart
      the worker.
   An access token is not needed: the worker mints its own.
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

To revoke access, remove the app at <https://myaccount.google.com/permissions> for that
Google account. Its account then pauses on the next post, with an alert.

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

See `docs/RUNBOOK.md` (written in E5-S8). It covers YouTube bot blocks, quota, stuck jobs, a full disk, budgets, Twitch auth, key rotation, rollback, restore, and adding a creator.
