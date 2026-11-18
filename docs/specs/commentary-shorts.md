# Commentary Shorts: a daily faceless gaming channel

Status: approved 2026-11-18. Epic: [CLP-142](https://linear.app/tengdev/issue/CLP-142). Each
story below is its own PR into `dev`.

## Problem

The owner wants a faceless YouTube Shorts channel that posts one Short a day about globally
known streamers playing trending games. The existing pipeline clips one creator's long video
into reposts. YouTube's monetization rules do not pay for that format:

- Clips and compilations of other people's streams fall under the reused-content policy.
  Permission or a licence does not change that; substantial commentary or editing does.
- The inauthentic-content policy (renamed from "repetitious content" in July 2025 and
  reorganised in July 2026) targets templated, mass-produced uploads. AI narration alone is
  not banned. An AI persona of a real person on a sensitive topic is.
- Mass re-uploads can also count as spam under the Community Guidelines, which brings strikes.

So the channel posts **original commentary**: a narrated story about a streamer moment, with
short excerpts used as evidence. Clips on campaign accounts (CLP-90) are unchanged and stay on
their own accounts; a hybrid phase comes after this epic.

Sources for the policy summary are third-party explainers, checked on 2026-11-18. The owner
re-reads YouTube's Help Center monetization policies before applying for the Partner Program.

## Owner decisions (2026-11-18)

| Question | Decision |
|---|---|
| Revenue | Original commentary first, aiming at the YouTube Partner Program. Hybrid with paid clipping programs later. |
| Voice | Free local TTS on the worker. No paid TTS account. |
| Discovery | A curated streamer list in YAML, boosted by games trending on Twitch. Not fully open trending. |
| Approval | One tap a day: the worker renders candidates, the owner approves one, the scheduler posts it. No approval, no post. The standing no-auto-approve rule holds. |
| Audience | US Gen Z, English, one post a day, times in `America/New_York` (the CLP-90 audience). |

## Decisions

| Area | Decision | Why |
|---|---|---|
| Where it runs | Inside the existing worker as a new job kind, `commentary`. No new service. | Reuses the job queue, leases, budgets, local Whisper, yt-dlp, Remotion, the review app, the scheduler, the YouTube publisher and Discord alerts. |
| The daily job | The monitor creates one job per commentary account per local day at `run_at` (default 06:00 local) with `platform = 'commentary'`, `item_id = '<account_id>:<YYYY-MM-DD>'`, `creator = <account_id>`. `jobs` is unique on `(platform, item_id)`, so a restart never makes a second job for the same day. | Discovery, fetching and scripting run as stages of that job, so they get the worker's retries, leases and crash-resume for free. |
| Stage chain | `detected` → `discovering` → `fetching_sources` → `transcribing` → `scripting` → `voicing` → `rendering` → `clips_ready`. The worker picks the stage function by job kind and status; clip jobs keep their chain unchanged. | `transcribing` and `rendering` can share code with the clip pipeline; the new statuses keep the commentary steps visible on the status page. |
| Discovery source | Twitch Helix with the existing app access token (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`): **Get Clips** per curated streamer with `broadcaster_id`, `started_at = now − 24 h`, `first = 20` ("descending order by view count" per the Helix reference), **Get Top Games** `first = 20` for the trending boost, and **Get Channel Information** for each streamer's `content_classification_labels`. | Free, no quota units, clips are already short moments. YouTube `search.list` stays unused (100 units a call). |
| Candidate score | `view_count × (1 + trending_boost)` where `trending_boost` is 0.5 when the clip's `game_id` is in today's top 20 games, else 0. The top `candidates_per_day` (default 12) by score go on; ties break on newer `created_at`. | Simple, explainable on the review page, tunable in YAML. |
| Candidate filters | Skip a clip that: was used in an earlier commentary Short (any account); is shorter than 5 s; has `language` other than `en`; comes from a channel whose classification labels include `SexualThemes` or `Gambling`, or whose `game_id` is in `excluded_game_ids` (gambling categories such as Slots by default). | Lowers the risk of age-restricted or advertiser-unfriendly Shorts, and keeps the channel from repeating itself. |
| Fetching excerpts | yt-dlp (its Twitch clips extractor) through the hardened runner in `clipper.pipeline.ytdlp`, video and audio, for the candidates only. | The runner already handles timeouts, environment allowlist and format errors. |
| Transcripts | Local faster-whisper on each candidate's audio, word timestamps, same model settings as clips. | Free; the script may only state what the transcript or metadata shows. |
| Script | One `claude -p` call per day (selector `claude_cli`, the owner's subscription; never `ANTHROPIC_API_KEY`) with a new prompt `prompts/commentary_v1.md`. It returns up to `stories_per_day` (default 3) stories as JSON validated against a pydantic schema: `hook` (≤ 12 words), `script` (60–120 words), `title` (≤ 70 characters, names the streamer and the game), `description`, `hashtags` (3–5), and `excerpts`: an ordered list of `{clip_id, start_s, end_s, after_sentence}`. | One call keeps the subscription use small and lets Claude pick the best stories across all candidates. |
| Transformation guardrails | Enforced in code after the script call; a story that breaks one is dropped with the reason logged: at most 3 excerpts, each ≤ 6 s, total excerpt time ≤ 40 % of the Short's runtime, narration covers ≥ 60 % of the runtime, runtime 20–55 s, every streamer and game name in the script appears in a candidate's transcript, title or metadata. The reviewer checks the remaining claims. | Makes "commentary, not a repost" a property the code checks on every Short, not a hope. |
| Attribution | On screen: "Clip: twitch.tv/&lt;login&gt;" during each excerpt. In the description: one credit line per excerpt with the clip URL. | Credits the streamer; helps a reviewer at YouTube see the commentary format. |
| Voice | **Kokoro-82M** through the `kokoro-onnx` package (onnxruntime, which faster-whisper already brings), one fixed stock English voice (`voice`, default `am_michael`), speed 1.0. Model and voice files are fetched by `clipper.pipeline.tts fetch` and pinned by SHA256 in code, like `local_asr fetch` and `reframe.MODEL_SHA256`. `clipper doctor` checks they are present. | Apache-2.0 model, commercial use allowed, CPU real-time factor around 0.5–0.7, so a 45 s narration takes well under a minute. No account, no per-character cost. Its phonemiser loads espeak-ng (GPL-3) as a library; the image is not distributed, so this is noted, not blocking. |
| Word timings | faster-whisper on the TTS output, aligned to the known script text. | Kokoro ONNX does not return timestamps; the captions component already consumes Whisper words. |
| Synthetic-media flag | Not set. The narration is a stock voice that does not imitate a real person and shows no realistic altered event. | YouTube's disclosure covers realistic altered or synthetic content. If a cloned real voice is ever used, the flag becomes mandatory. |
| Render | New Remotion composition `CommentaryShort`, 1080×1920, 30 fps: a hook card for the first 1–2 s, narration with the existing animated captions, excerpts shown in a framed window over a blurred copy of the same excerpt (narration paused while an excerpt plays with its own audio), between excerpts a still from the excerpt with a slow zoom, the credit line, and the progress bar. Loudness −14 LUFS. Optional background music from `music_dir`, ducked to −20 dB under narration and muted during excerpts. | Reuses `Captions`, `HookCard`, `ProgressBar` and `BlurredFill`. |
| Music | Only files in the configured `music_dir` (the owner fills it from the YouTube Audio Library, tracks that need no attribution). Empty or unset means no music. | No music the code cannot account for. |
| Storage | Each story becomes a `clips` row under the day's job (`rank` 1..3) with its version in `clip_versions` (`start_s = 0`, `end_s` = runtime). A new `commentary_stories` table holds the version's script, excerpt plan and guardrail figures. A new `story_candidates` table holds every discovered Twitch clip (unique on the Twitch clip id) with its score and the clip id of the Short that used it. Migration `0011`. | The review app, export, janitor and posting all key on `clips`; they work with no new path. |
| Review | The clip page for a commentary clip shows the script, each excerpt with a link to its Twitch clip, the guardrail figures, and a checklist (facts match the clips, nothing sensitive, credit lines right). Approve, reject and "regenerate with note" work as for clips; regenerate re-runs `scripting` for that one story with the note. One Discord ping per day: "N commentary candidates ready". | One tap a day from the phone. |
| Posting | `accounts.yaml` gets `content: clips | commentary` (default `clips`). A commentary account has no `creators:` and no `campaign:`. `schedule_for_clip` schedules a commentary clip only to commentary accounts and a clip only to clip accounts. Default plan for the account: `posts_per_day: 1`, the US YouTube windows, and the usual warm-up. | Keeps campaign rules (no AI content) away from this channel, and this channel's TTS away from campaign accounts. |
| Failure | Twitch errors defer the job with backoff. No candidate that passes the filters, or no story that passes the guardrails, skips the job with the reason and sends one alert ("no commentary Short today: …"). TTS or render failures fail the job after the usual retries. | The owner learns about a missed day the same morning. |

## Config

A new file, `config/commentary.yaml`, validated by `clipper check-config`:

```yaml
settings:
  run_at: "06:00"            # local time of each account
  candidates_per_day: 12
  stories_per_day: 3
  trending_boost: 0.5
  excluded_game_ids: []      # added to the gambling categories the code always excludes
  voice: am_michael
  music_dir: null            # e.g. /data/music; null = no music
streamers:
  - name: Kai Cenat
    twitch: kaicenat         # login; user_id filled in by `clipper resolve`
  - name: Jynxzi
    twitch: jynxzi
```

The starting streamer list is the owner's call; the ones above are examples. Discovery reads
Twitch only, so a streamer must stream on Twitch: YouTube-only streamers such as IShowSpeed
are not found until a YouTube discovery source is added (out of scope here). A streamer who
asks to be left out is removed from the list, and their unused candidates are dropped.

## Out of scope

- Hybrid clips on this channel, and any clipping-program campaign for it (later epic).
- Platforms other than YouTube Shorts for commentary (TikTok and Reels later, through the
  existing publishers).
- Kick, YouTube or X as discovery sources.
- Fully open trending discovery, auto-approve, or a cloned voice.
- Custom thumbnails (Shorts use a frame), comments, community posts.

## Testing

- Unit tests with httpx `MockTransport` fixtures for the Helix calls (pagination, an expired
  app token, HTTP errors), scoring, filters and the guardrail checks.
- The scripting stage with a fake selector returning fixed JSON, including invalid and
  guardrail-breaking stories.
- The TTS stage and a `CommentaryShort` render under the existing `slow` and `render` marks,
  so the nightly job runs them with the real Kokoro model and Remotion.
- Review app tests for the commentary clip page and the approve-to-schedule path, plus one
  browser test in the e2e job.
- Live acceptance, by hand: one real day on the owner's channel, from discovery to a public
  Short at its `publishAt` time.

## Stories

Build order: S1, then S2 to S4 in sequence; S5 can run in parallel after S1; S6 needs S4 and S5; then S7 and S8.

1. **Config, accounts and storage.** [CLP-143](https://linear.app/tengdev/issue/CLP-143): `config/commentary.yaml`, `content:` in `accounts.yaml`,
   migration `0011` (`story_candidates`, `commentary_stories`), the new job statuses, and the
   monitor creating the daily job.
2. **Twitch discovery.** [CLP-144](https://linear.app/tengdev/issue/CLP-144): the `discovering` stage: Get Clips, Get Top Games, Get Channel
   Information, scoring and filters, `story_candidates` rows. Extends `clipper resolve` to the
   streamer list.
3. **Fetch and transcribe candidates.** [CLP-145](https://linear.app/tengdev/issue/CLP-145): the `fetching_sources` and `transcribing` stages for
   commentary jobs.
4. **Script stage.** [CLP-146](https://linear.app/tengdev/issue/CLP-146): `prompts/commentary_v1.md`, the JSON schema, the guardrail checks, and
   `commentary_stories` rows.
5. **Local TTS.** [CLP-147](https://linear.app/tengdev/issue/CLP-147): `clipper.pipeline.tts` with Kokoro ONNX, `tts fetch` with pinned hashes, the
   `clipper doctor` check, the `voicing` stage and word alignment. Dockerfile and macOS setup.
6. **CommentaryShort render.** [CLP-148](https://linear.app/tengdev/issue/CLP-148): the Remotion composition, music ducking, loudness, and the
   `rendering` stage for commentary jobs.
7. **Review and posting.** [CLP-149](https://linear.app/tengdev/issue/CLP-149): the commentary clip page, checklist, daily Discord ping,
   regenerate-with-note for one story, and `schedule_for_clip` routing by `content`.
8. **Setup guide and live run.** [CLP-150](https://linear.app/tengdev/issue/CLP-150): the "Commentary channel" section of `docs/DEPLOYMENT.md`
   (accounts below), the nightly slow test, and the first live day.

## Owner setup

Values go into `.env` on the worker host (or the local `.env` for a dev run), never into YAML
or chat.

1. **YouTube channel.** A Brand Account channel for the faceless brand (name, handle, avatar,
   banner), phone-verified.
2. **Google Cloud project.** YouTube Data API v3 enabled; OAuth consent screen published to
   **Production** (a Testing app's refresh tokens expire after 7 days); a Web application OAuth
   client: `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`. A refresh token for the
   channel from the OAuth Playground: `CLIPPER_REFRESH_TOKEN_<ACCOUNT_ID>`. Steps:
   `docs/DEPLOYMENT.md`, "YouTube publishing".
3. **Twitch developer application** at <https://dev.twitch.tv/console> (the Twitch account
   needs two-factor authentication): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`.
4. **Discord.** Two webhooks: `DISCORD_WEBHOOK_URL` (review pings), `DISCORD_ALERTS_WEBHOOK_URL`.
5. **Review app login.** `REVIEW_PASSWORD_HASH` from `clipper hash-password`.
6. **Claude subscription token.** `CLAUDE_CODE_OAUTH_TOKEN` (already set locally).
7. **Streamer list** for `config/commentary.yaml`.
8. **Music** (optional): 10–20 YouTube Audio Library tracks that need no attribution, in one
   folder mounted at `music_dir`.

No TTS account is needed.
