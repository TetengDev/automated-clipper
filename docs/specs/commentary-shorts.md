# Commentary Shorts: a daily faceless gaming channel

Status: approved 2026-11-18; amended 2026-11-19 (game focus with a trending-streamer scan, pinned YouTube streamers, research notes on timings, licences, music and quality gates). Epic: [CLP-142](https://linear.app/tengdev/issue/CLP-142). Each
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
| Discovery | Game focus with an automatic scan (changed 2026-11-19, replacing "a curated streamer list only"). Each channel names the games it covers, the first being Grand Theft Auto VI, and discovery finds that game's trending and viral streamers on Twitch every day. The owner still curates: pinned streamers (on Twitch, or on YouTube such as IShowSpeed) are always scanned and get a score boost, and excluded streamers are never used. Not open trending across all games. |
| Approval | One tap a day: the worker renders candidates, the owner approves one, the scheduler posts it. No approval, no post. The standing no-auto-approve rule holds. |
| Audience | US Gen Z, English, one post a day, times in `America/New_York` (the CLP-90 audience). |

## Decisions

| Area | Decision | Why |
|---|---|---|
| Where it runs | Inside the existing worker as a new job kind, `commentary`. No new service. | Reuses the job queue, leases, budgets, local Whisper, yt-dlp, Remotion, the review app, the scheduler, the YouTube publisher and Discord alerts. |
| The daily job | The monitor creates one job per commentary account per local day at `run_at` (default 06:00 local) with `platform = 'commentary'`, `item_id = '<account_id>:<YYYY-MM-DD>'`, `creator = <account_id>`. `jobs` is unique on `(platform, item_id)`, so a restart never makes a second job for the same day. | Discovery, fetching and scripting run as stages of that job, so they get the worker's retries, leases and crash-resume for free. |
| Stage chain | `detected` → `discovering` → `fetching_sources` → `transcribing` → `scripting` → `voicing` → `rendering` → `clips_ready`. The worker picks the stage function by job kind and status; clip jobs keep their chain unchanged. | `transcribing` and `rendering` can share code with the clip pipeline; the new statuses keep the commentary steps visible on the status page. |
| Discovery source | Twitch Helix with the existing app access token (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`), for clips made in the last 24 h: **Get Clips** with each of the channel's games' `game_id` (`first = 100`, up to 5 pages, "descending order by view count" per the Helix reference), **Get Clips** with each pinned Twitch streamer's `broadcaster_id` (`first = 20`), **Get Top Games** `first = 20` for the trending boost, and **Get Channel Information** for each streamer's `content_classification_labels`. A pinned streamer's clips count only when the clip's game is one of the channel's `games`: pinning boosts a streamer, it does not widen the game focus. `clipper resolve` fills in each game's `game_id` and each pinned Twitch streamer's `user_id`. | Free, no quota units, clips are already short moments. Scanning by game finds streamers nobody listed. YouTube `search.list` stays unused (100 units a call). |
| Trending streamers | Discovery groups the scanned Twitch clips by streamer and ranks the streamers by view velocity: the sum, over their clips, of `view_count / max(age_hours, 1)`, where `age_hours` is the time since the clip was made. The top `max_streamers_scanned` (default 50) streamers, plus every pinned streamer, are the day's trending streamers; only their clips become candidates. A streamer in `exclude_streamers` is dropped before ranking, even when trending: the exclude list always wins. The day's ranking is stored in `streamer_rankings` and shown on the review page. | Surfaces the streamers who are blowing up today, not only the biggest names. The owner sees why a streamer was picked, and pins or excludes them. |
| YouTube pinned streamers | A pinned streamer on YouTube (for example IShowSpeed, who streams on YouTube, not Twitch) is scanned by its own story, [CLP-152](https://linear.app/tengdev/issue/CLP-152), after CLP-144. It reuses the existing YouTube source (the channel's RSS feed and yt-dlp metadata; no `search.list`), keeps the videos of the last 24 h whose title or description names one of the channel's games or their `aliases` (such as "GTA 6"), takes the "most replayed" heatmap peaks as moments, and cuts excerpts with the existing section download. The on-screen credit is "Clip: youtube.com/@&lt;handle&gt;". Until CLP-152 ships, YouTube pins are listed but not scanned. | Lets the owner include streamers who are not on Twitch without opening up YouTube trending. |
| Candidate score | View velocity times the boosts: `view_count / max(age_hours, 1) × (1 + trending_boost + pinned_boost)`, where `trending_boost` (default 0.5) applies when the clip's game is in today's top 20 games and `pinned_boost` (default 0.5) when the clip's streamer is pinned; each is 0 otherwise. The top `candidates_per_day` (default 12) by score go on; ties break on newer `created_at`. | Rewards a clip that is taking off today over an older one with more total views. Explainable on the review page, tunable in YAML. |
| Candidate filters | Skip a clip that: was used in an earlier commentary Short (any account); is shorter than 5 s; has fewer than `min_clip_views` (default 1000) views; is not of one of the channel's games; has `language` other than `en`; comes from a streamer in `exclude_streamers`; comes from a channel whose classification labels include `SexualThemes` or `Gambling`, or whose `game_id` is in `excluded_game_ids` (gambling categories such as Slots by default). | Lowers the risk of age-restricted or advertiser-unfriendly Shorts, keeps low-signal clips out, and keeps the channel from repeating itself. |
| Fetching excerpts | yt-dlp (its Twitch clips extractor) through the hardened runner in `clipper.pipeline.ytdlp`, video and audio, for the candidates only. | The runner already handles timeouts, environment allowlist and format errors. |
| Transcripts | Local faster-whisper on each candidate's audio, word timestamps, same model settings as clips. | Free; the script may only state what the transcript or metadata shows. |
| Script | One `claude -p` call per day (selector `claude_cli`, the owner's subscription; never `ANTHROPIC_API_KEY`) with the channel's prompt (the first one is `prompts/commentary_gaming_v1.md`). It returns up to `stories_per_day` (default 3) stories as JSON validated against a pydantic schema: `hook` (≤ 12 words), `script` (60–120 words), `title` (≤ 70 characters, names the streamer and the game), `description`, `hashtags` (3–5), and `excerpts`: an ordered list of `{clip_id, start_s, end_s, after_sentence}`. | One call keeps the subscription use small and lets Claude pick the best stories across all candidates. |
| Transformation guardrails | Enforced in code after the script call; a story that breaks one is dropped with the reason logged: at most 3 excerpts, each ≤ 6 s, total excerpt time ≤ 40 % of the Short's runtime, narration covers ≥ 60 % of the runtime, runtime 20–55 s, every streamer and game name in the script appears in a candidate's transcript, title or metadata. The reviewer checks the remaining claims. | Makes "commentary, not a repost" a property the code checks on every Short, not a hope. |
| Quality gates | Built in CLP-146 and CLP-149. `better-profanity` checks the script, title and description. A nudity check runs on 2–4 frames per excerpt with `onnx-community/nsfw_image_detection-ONNX` on onnxruntime (the licence of that ONNX copy is confirmed before use; NudeNet is rejected, AGPL-3.0 on GitHub). `imagehash` perceptual hashes skip excerpts that nearly duplicate one in an earlier Short. YouTube's limits are enforced before upload: title at most 100 characters and no `<` or `>`, description at most 5000 bytes and no `<` or `>`, tags at most 500 characters in total. A story that fails a gate is dropped with the reason logged. | Catches what the guardrails cannot: words, images and repeats that would hurt the channel, and metadata YouTube would reject. |
| Attribution | On screen: "Clip: twitch.tv/&lt;login&gt;" (or "Clip: youtube.com/@&lt;handle&gt;" for a YouTube excerpt) during each excerpt. In the description: one credit line per excerpt with the clip URL. | Credits the streamer; helps a reviewer at YouTube see the commentary format. |
| Voice | **Kokoro-82M** through the `kokoro-onnx` package (onnxruntime, which faster-whisper already brings), one fixed stock English voice (`voice`, default `am_michael`), speed 1.0. Model and voice files are fetched by `clipper.pipeline.tts fetch` and pinned by SHA256 in code, like `local_asr fetch` and `reframe.MODEL_SHA256`. `clipper doctor` checks they are present. | Apache-2.0 model, commercial use allowed, CPU real-time factor around 0.5–0.7, so a 45 s narration takes well under a minute. No account, no per-character cost. It also pulls `phonemizer` (GPL-3) and loads espeak-ng (GPL-3) as a library; the image is not distributed, so this is noted, not blocking. Fallback voice: Piper (`piper-tts`, GPL-3 engine) with a voice from its COMMERCIAL_USE list, such as the CC0 `en_US-joe`. |
| Word timings | `kokoro-onnx` 0.6.1 returns timings itself: `Kokoro.create_timed()` gives per-phoneme `Timing(phoneme, start, end)` with the model files of the `model-files-v1.1` release (whose ONNX outputs are `waveform` and `duration`). The phonemes are grouped into words and matched to the script with a normalised word match. faster-whisper word timestamps on the TTS audio are the fallback. The SHA256 pins in CLP-147 are for the `model-files-v1.1` files. | Exact timings from the voice itself, without a second transcription; the captions component consumes the same word list. |
| Synthetic-media flag | Not set. The narration is a stock voice that does not imitate a real person and shows no realistic altered event. | YouTube's disclosure covers realistic altered or synthetic content. If a cloned real voice is ever used, the flag becomes mandatory. |
| Render | New Remotion composition `CommentaryShort`, 1080×1920, 30 fps: a hook card for the first 1–2 s, narration with the existing animated captions, excerpts shown in a framed window over a blurred copy of the same excerpt (narration paused while an excerpt plays with its own audio), between excerpts a still from the excerpt with a slow zoom, the credit line, and the progress bar. Loudness −14 LUFS. Optional background music from `music_dir`, ducked to −20 dB under narration and muted during excerpts. | Reuses `Captions`, `HookCard`, `ProgressBar` and `BlurredFill`. |
| Music | Only files in the configured `music_dir`, which the owner fills from the YouTube Audio Library with the "Attribution not required" filter. These are used on YouTube only, until reuse on other platforms is checked. Ducking is a per-frame volume curve in Remotion (`interpolate`); loudness stays the existing two-pass ffmpeg `loudnorm`. Empty or unset means no music. | No music the code cannot account for. |
| Dependencies | No dependency that needs PyTorch. Models run on onnxruntime, which faster-whisper already brings. | PyTorch adds about 1 GB to the CPU image. |
| Storage | Each story becomes a `clips` row under the day's job (`rank` 1..3) with its version in `clip_versions` (`start_s = 0`, `end_s` = runtime). A new `commentary_stories` table holds the version's script, excerpt plan and guardrail figures. A new `story_candidates` table holds every discovered Twitch clip (unique on the Twitch clip id) with its score, whether its streamer is pinned, and the clip id of the Short that used it. A new `streamer_rankings` table holds each day's trending-streamer ranking. Migration `0011`. YouTube moments (CLP-152) get their own storage in a later migration. | The review app, export, janitor and posting all key on `clips`; they work with no new path. |
| Review | The clip page for a commentary clip shows the script, each excerpt with a link to its Twitch clip, the guardrail figures, the day's trending-streamer ranking, and a checklist (facts match the clips, nothing sensitive, credit lines right). Approve, reject and "regenerate with note" work as for clips; regenerate re-runs `scripting` for that one story with the note. One Discord ping per day: "N commentary candidates ready". | One tap a day from the phone. |
| Posting | `accounts.yaml` gets `content: clips | commentary` (default `clips`). A commentary account has no `creators:` and no `campaign:`. `schedule_for_clip` schedules a commentary clip only to commentary accounts and a clip only to clip accounts. Default plan for the account: `posts_per_day: 1`, the US YouTube windows, and the usual warm-up. | Keeps campaign rules (no AI content) away from this channel, and this channel's TTS away from campaign accounts. |
| Channel check | `accounts.yaml` gets an optional `channel_id` for YouTube accounts (set for every commentary account). Before an upload, the publisher compares it with the channel the refresh token belongs to (`channels.list mine=true`, which the duplicate check already calls; 1 quota unit, cached with the access token). A mismatch pauses the account with one alert, like a rejected token. | With several channels under one Google account, the OAuth Playground makes it easy to authorise the wrong channel; this stops a Short from going to the wrong channel. |
| Failure | Twitch errors defer the job with backoff. No candidate that passes the filters, or no story that passes the guardrails, skips the job with the reason and sends one alert ("no commentary Short today: …"). TTS or render failures fail the job after the usual retries. | The owner learns about a missed day the same morning. |

## Config

The first channel is **Random Gaming** (`UCcMYw3Ae2crqXE2yzFBSUig`). The owner will add more
channels with other content later, so every commentary setting is per channel.

The channel's entry in `config/accounts.yaml`:

```yaml
  - id: random_gaming_yt
    platform: youtube
    content: commentary
    channel_id: UCcMYw3Ae2crqXE2yzFBSUig
    handle: "@RandomGaming-f3w"
    market: US
    timezone: America/New_York
```

A new file, `config/commentary.yaml`, validated by `clipper check-config`:

```yaml
defaults:                    # apply to every channel unless the channel overrides them
  run_at: "06:00"            # local time of the channel's account
  candidates_per_day: 12
  stories_per_day: 3
  trending_boost: 0.5        # the clip's game is in Twitch's top 20 today
  pinned_boost: 0.5          # the clip's streamer is pinned
  min_clip_views: 1000       # skip clips with fewer views
  max_streamers_scanned: 50  # trending streamers kept from each day's scan
  excluded_game_ids: []      # added to the gambling categories the code always excludes
  voice: am_michael
  music_dir: null            # e.g. /data/music; null = no music
channels:
  - account: random_gaming_yt   # an accounts.yaml id with content: commentary
    source: twitch_clips        # discovery source; the only one in this epic
    prompt: commentary_gaming_v1
    games:                      # at least one
      - name: Grand Theft Auto VI
        aliases: [GTA 6, GTA VI]  # how YouTube titles name it (CLP-152)
        game_id: "1867279146"     # Twitch's id; `clipper resolve` confirms it
    streamers:                  # pinned: always scanned, pinned_boost; may be empty
      - name: Kai Cenat
        twitch: kaicenat        # login; user_id filled in by `clipper resolve`
      - name: IShowSpeed
        youtube:                # a YouTube streamer, scanned by CLP-152
          handle: "@IShowSpeed"
          channel_id: UCWsDFcIhY2DBi3GB5uykGXA
    exclude_streamers: []       # Twitch logins or YouTube handles never used
```

Each channel names its account, its discovery `source`, its script `prompt` and that
source's inputs, and may override any default (for example another `voice` or `run_at`). For
`twitch_clips` the inputs are `games` (the Twitch categories the channel covers, at least
one, each with optional `aliases` and its Twitch `game_id`), `streamers` (pinned streamers,
optional, each with exactly one of a Twitch login or a YouTube channel) and
`exclude_streamers` (Twitch logins or YouTube handles never used). A new channel with other
content is a new `channels` entry and, if it needs one, a new source or prompt; the stages
stay the same. check-config rejects a channel whose account is missing, is not
`content: commentary`, or is used by two channels; a `twitch_clips` channel with no game; a
pinned streamer with both or neither of `twitch` and `youtube`; duplicate games, aliases,
game ids, logins, handles or channel ids; a login that is not a valid Twitch login; and a
streamer who is both pinned and excluded.

The games and the pinned list are the owner's call; the first game is Grand Theft Auto VI and
the full pinned list is in `config/commentary.yaml`. A pinned Twitch streamer is found through
Twitch. A pinned YouTube streamer, such as IShowSpeed, is found by
[CLP-152](https://linear.app/tengdev/issue/CLP-152) through the channel's own uploads (see
"YouTube pinned streamers"); until it ships, check-config notes that the pin is not scanned
yet. Either way, only the streamer's moments of the channel's games count. A streamer who
asks to be left out is added to `exclude_streamers`, and their unused candidates are dropped.

## Out of scope

- Hybrid clips on this channel, and any clipping-program campaign for it (later epic).
- Platforms other than YouTube Shorts for commentary (TikTok and Reels later, through the
  existing publishers).
- Kick or X as discovery sources. YouTube is a discovery source only for pinned YouTube
  streamers (CLP-152): no open YouTube trending, no `search.list`.
- Open trending discovery across all games (the scan covers each channel's games only),
  auto-approve, or a cloned voice.
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

Build order: S1, then S2 to S4 in sequence; S9 after S2; S5 can run in parallel after S1; S6 needs S4 and S5; then S7 and S8.

1. **Config, accounts and storage.** [CLP-143](https://linear.app/tengdev/issue/CLP-143): `config/commentary.yaml` (per channel), `content:` and `channel_id` in `accounts.yaml`,
   migration `0011` (`story_candidates`, `streamer_rankings`, `commentary_stories`), the new job statuses, and the
   monitor creating the daily job.
2. **Twitch discovery.** [CLP-144](https://linear.app/tengdev/issue/CLP-144): the `discovering` stage: the game scan (Get Clips by `game_id`), pinned Twitch streamers' clips of the channel's games, the trending-streamer ranking, Get Top Games, Get Channel Information, velocity scoring and filters, `story_candidates` and `streamer_rankings` rows. Extends `clipper resolve` to the channels' games and pinned streamers.
3. **Fetch and transcribe candidates.** [CLP-145](https://linear.app/tengdev/issue/CLP-145): the `fetching_sources` and `transcribing` stages for
   commentary jobs.
4. **Script stage.** [CLP-146](https://linear.app/tengdev/issue/CLP-146): `prompts/commentary_gaming_v1.md` (prompt per channel), the JSON schema, the guardrail checks,
   `commentary_stories` rows and the profanity gate.
5. **Local TTS.** [CLP-147](https://linear.app/tengdev/issue/CLP-147): `clipper.pipeline.tts` with Kokoro ONNX, `tts fetch` with pinned hashes, the
   `clipper doctor` check, the `voicing` stage and word timings from `create_timed` (faster-whisper as fallback), pinned to the `model-files-v1.1` files. Dockerfile and macOS setup.
6. **CommentaryShort render.** [CLP-148](https://linear.app/tengdev/issue/CLP-148): the Remotion composition, music ducking, loudness, and the
   `rendering` stage for commentary jobs.
7. **Review and posting.** [CLP-149](https://linear.app/tengdev/issue/CLP-149): the commentary clip page, checklist, daily Discord ping,
   regenerate-with-note for one story, `schedule_for_clip` routing by `content`, the
   publisher's channel check, and the nudity, near-duplicate and YouTube metadata gates.
8. **Setup guide and live run.** [CLP-150](https://linear.app/tengdev/issue/CLP-150): the "Commentary channel" section of `docs/DEPLOYMENT.md`
   (accounts below), the nightly slow test, and the first live day.
9. **YouTube pinned streamers.** [CLP-152](https://linear.app/tengdev/issue/CLP-152), after CLP-144:
   discovery for pinned YouTube streamers through the existing YouTube source (RSS and yt-dlp
   metadata), game matching on titles and descriptions with the games' aliases, "most
   replayed" heatmap peaks as moments, excerpts from the existing section download, the
   "Clip: youtube.com/@&lt;handle&gt;" credit, and their storage.

## Owner setup

The channel is Random Gaming (`UCcMYw3Ae2crqXE2yzFBSUig`), owned through lbfilao@gmail.com.
Values go into `.env` on the worker host (or the local `.env` for a dev run), never into YAML
or chat.

1. **YouTube channel.** A Brand Account channel for the faceless brand (name, handle, avatar,
   banner), phone-verified.
2. **Google Cloud project.** YouTube Data API v3 enabled; OAuth consent screen published to
   **Production** (a Testing app's refresh tokens expire after 7 days); a Web application OAuth
   client: `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`. A refresh token for the
   channel from the OAuth Playground: `CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT`. One OAuth client
   serves every channel; each channel has its own refresh token. Steps:
   `docs/DEPLOYMENT.md`, "YouTube publishing".
3. **Twitch developer application** at <https://dev.twitch.tv/console> (the Twitch account
   needs two-factor authentication): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`.
4. **Discord.** Two webhooks: `DISCORD_WEBHOOK_URL` (review pings), `DISCORD_ALERTS_WEBHOOK_URL`.
5. **Review app login.** `REVIEW_PASSWORD_HASH` from `clipper hash-password`.
6. **Claude subscription token.** `CLAUDE_CODE_OAUTH_TOKEN` (already set locally).
7. **Games and streamers** for `config/commentary.yaml`: the games to cover (first: Grand
   Theft Auto VI), the pinned streamers (the owner's top ten on Twitch plus IShowSpeed on
   YouTube, chosen 2026-11-19) and any streamers to exclude.
8. **Music** (optional): 10–20 YouTube Audio Library tracks that need no attribution, in one
   folder mounted at `music_dir`.

No TTS account is needed.
