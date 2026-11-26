# Commentary Shorts: a faceless gaming channel

Status: approved 2026-11-18; amended 2026-11-19 (game focus with a trending-streamer scan,
pinned streamers, research notes on timings, licences, music and quality gates); amended
2026-11-20 (face zoom and SFX density) and 2026-11-24 (approval is the only manual step,
Twitch-only go-live, virality bar, scoring, clips first, editing look, render sidecar and AI
flag, selector, render spike, always-on gaps, posting for commentary accounts, build order).
Epic: [CLP-142](https://linear.app/tengdev/issue/CLP-142). Each story below is its own PR
into `dev`.

## Problem

The owner wants a faceless YouTube Shorts channel that posts up to three Shorts a day about
globally known streamers playing trending games. The existing pipeline clips one creator's
long video into reposts. YouTube's monetization rules do not pay for that format:

- Clips and compilations of other people's streams fall under the reused-content policy.
  Permission or a licence does not change that; substantial commentary or editing does.
- The inauthentic-content policy (renamed from "repetitious content" in July 2025 and
  reorganised in July 2026) targets templated, mass-produced uploads. AI narration alone is
  not banned. An AI persona of a real person on a sensitive topic is.
- The Shorts originality update of 2026-10-01 demotes Shorts that show other people's clips
  with nothing of the channel's own.
- Mass re-uploads can also count as spam under the Community Guidelines, which brings strikes.

So the channel posts **clips with original commentary**: the streamer's clip fills most of
the Short, and our own commentary (spoken lines or caption callouts) lands every few seconds
on top of it. Clips on campaign accounts (CLP-90) are unchanged and stay on their own
accounts; a hybrid phase comes after this epic.

Sources for the policy summary are third-party explainers, checked on 2026-11-18. The owner
re-reads YouTube's Help Center monetization policies before applying for the Partner Program.

## Owner decisions

| Question | Decision |
|---|---|
| Revenue | Original commentary first, aiming at the YouTube Partner Program. Hybrid with paid clipping programs later. (2026-11-18) |
| Voice | Free local TTS on the worker. No paid TTS account. (2026-11-18) |
| Discovery | Game focus with an automatic scan (2026-11-19). Each channel names the games it covers, the first being Grand Theft Auto VI, and discovery finds that game's trending and viral streamers on Twitch every day. The owner still curates: pinned streamers are always scanned and their viral clips rank above all others (see "Settled during the 2026-11-24 review"), and excluded streamers are never used. Kai Cenat is the first Twitch pin. Not open trending across all games. |
| Sources at go-live | Twitch only ([CLP-144](https://linear.app/tengdev/issue/CLP-144)). YouTube live and pinned YouTube streamers ([CLP-152](https://linear.app/tengdev/issue/CLP-152)) and Kick ([CLP-153](https://linear.app/tengdev/issue/CLP-153)) come after go-live, not before. (2026-11-24) |
| Virality bar | A clip is a candidate only with at least 10,000 views, or a high views-per-hour rate (see "Candidate filters"). On a day when nothing qualifies, the channel makes no Short. That is a normal outcome, not an alert. (2026-11-24) |
| Approval | **Approval is the only manual step** (2026-11-24). Everything else runs on its own: discovery, fetch and transcribe, script (with the SEO rules for title, description and hashtags), Kokoro voice, Remotion render, custom thumbnail, slot pick on approve, upload with `publishAt`, `thumbnails.set`, and insights tags at approval. The rules that stay: no auto-approve; at most 3 Shorts a day per channel, at least 2 h apart; one-tap approve; one Discord ping when Shorts are ready. No approval, no post. |
| Audience | US Gen Z, English, times in `America/New_York` (the CLP-90 audience). |

## Decisions

| Area | Decision | Why |
|---|---|---|
| Where it runs | Inside the existing worker as a new job kind, `commentary`. No new service. Rendering may move to a render worker on the Mac host if the phase 1 render spike fails (see "Phase 1 render spike"). | Reuses the job queue, leases, budgets, local Whisper, yt-dlp, Remotion, the review app, the scheduler, the YouTube publisher and Discord alerts. |
| The daily job | The monitor creates one job per commentary account per local day at `run_at` (default 06:00 local) with `platform = 'commentary'`, `item_id = '<account_id>:<YYYY-MM-DD>'`, `creator = <account_id>` (`src/clipper/commentary/daily.py`). `jobs` is unique on `(platform, item_id)`, so a restart never makes a second job for the same day. | Discovery, fetching and scripting run as stages of that job, so they get the worker's retries, leases and crash-resume for free. |
| Stage chain | `detected` → `discovering` → `fetching_sources` → `transcribing` → `scripting` → `voicing` → `rendering` → `clips_ready`. The worker picks the stage function by job kind and status; clip jobs keep their chain unchanged. Until a stage's story ships, its placeholder in `src/clipper/pipeline/commentary.py` ends the day's job as `skipped`. | `transcribing` and `rendering` can share code with the clip pipeline; the new statuses keep the commentary steps visible on the status page. |
| Discovery source | Twitch Helix with the existing app access token (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`), for clips made in the last 24 h: **Get Clips** with each of the channel's games' `game_id` (`first = 100`, up to 5 pages, "descending order by view count" per the Helix reference), **Get Clips** with each pinned Twitch streamer's `broadcaster_id` (`first = 20`), **Get Top Games** `first = 20` for the trending boost, and **Get Channel Information** for each streamer's `content_classification_labels`. A pinned streamer's clips count only when the clip's game is one of the channel's `games`: pinning boosts a streamer, it does not widen the game focus. `clipper resolve` fills in each game's `game_id` and each pinned Twitch streamer's `user_id`. | Free, no quota units, clips are already short moments. Scanning by game finds streamers nobody listed. YouTube `search.list` stays unused (100 units a call). |
| Trending streamers | Discovery groups the scanned Twitch clips by streamer and ranks the streamers by view velocity: the sum, over their usable clips, of `view_count / max(age_hours, 1)`, where `age_hours` is the time since the clip was made. A usable clip passes the hard filters of "Candidate filters": one of the channel's games, `language` `en`, at least 5 s, a channel without the `SexualThemes` or `Gambling` label, a game outside the gambling categories and `excluded_game_ids`, and a streamer not in `exclude_streamers`. The virality bar, the score floor and an earlier use do not change the ranking (owner, 2026-11-26). This rule supersedes the wording of the `streamer_rankings` comment in migration `0011`, which is not edited. The top `max_streamers_scanned` (default 50) streamers, plus every pinned streamer, are the day's trending streamers; only their clips become candidates. A streamer in `exclude_streamers` is dropped before ranking, even when trending: the exclude list always wins. The day's ranking is stored in `streamer_rankings` and shown on the review page. | Surfaces the streamers who are blowing up today, not only the biggest names. The owner sees why a streamer was picked, and pins or excludes them. |
| Pinned YouTube streamers | After go-live. A pinned streamer on YouTube (for example IShowSpeed, who streams on YouTube, not Twitch) is scanned by [CLP-152](https://linear.app/tengdev/issue/CLP-152). It reuses the existing YouTube source (the channel's RSS feed and yt-dlp metadata; no `search.list`), keeps the videos of the last 24 h whose title or description names one of the channel's games or their `aliases` (such as "GTA 6"), takes the "most replayed" heatmap peaks as moments, and cuts clips with the existing section download. The on-screen credit is "Clip: youtube.com/@&lt;handle&gt;". Until CLP-152 ships, YouTube pins are listed but not scanned. | Lets the owner include streamers who are not on Twitch, without delaying go-live. |
| Kick | After go-live: [CLP-153](https://linear.app/tengdev/issue/CLP-153) adds Kick discovery. | Not needed for go-live. |
| Candidate filters | A clip is a candidate only when it passes the **virality bar**: `view_count ≥ min_clip_views` (default 10,000) **or** views per hour `≥ min_views_per_hour` (default 400), where views per hour is `view_count / max(age_hours, 1)`. The default 400 is 10,000 views over the 24 h scan window (about 417 an hour), rounded down: a clip on pace to pass 10,000 views within its first day qualifies before it gets there. Discovery also skips a clip that: was used in an earlier commentary Short (any account); is shorter than 5 s; is not of one of the channel's games; has `language` other than `en`; comes from a streamer in `exclude_streamers`; comes from a channel whose classification labels include `SexualThemes` or `Gambling`, or whose `game_id` is in `excluded_game_ids` (gambling categories such as Slots by default). | Only clips that are already proven or taking off become Shorts. Lowers the risk of age-restricted or advertiser-unfriendly Shorts, and keeps the channel from repeating itself. |
| Candidate score | View velocity times the boosts: `view_count / max(age_hours, 1) × (1 + trending_boost + pinned_boost)`, where `trending_boost` (default 0.5) applies when the clip's game is in today's top 20 games and `pinned_boost` (default 0.5) when the clip's streamer is pinned; each is 0 otherwise. A clip whose score is below `min_score` is dropped (the floor's default is an open question). The top `candidates_per_day` (default 12) by score go on; ties break on newer `created_at`. | Rewards a clip that is taking off today over an older one with more total views. The boost alone does not put every pinned clip above every other clip; whether pinned clips sort first outright is an open question. Explainable on the review page, tunable in YAML. |
| No candidate | When no clip passes the virality bar, the filters and the score floor, the job ends as `skipped` with the reason ("no clip passed the virality bar today"). No alert and no Discord ping. The status page shows the reason. | A quiet day is normal; an alert would train the owner to ignore alerts. |
| Moment hints | After fetching (CLP-145), each candidate's audio gets loudness peaks from the existing `loudness_peaks` helper in `src/clipper/pipeline/fetch_audio.py`. The script prompt gets, per candidate, its score, its rank, its loudness peaks and its transcript. CLP-148 may use the same peaks to place the face zoom. | Tells Claude where the reaction is without a second model. |
| Learning from decisions | The owner's approve and reject decisions on past commentary Shorts go into the script prompt as examples, through the existing `feedback_examples` parameter (`build_user_prompt` and `select_highlights` in `src/clipper/pipeline/select.py`; no caller passes examples yet). View and retention feedback ([CLP-98](https://linear.app/tengdev/issue/CLP-98)) waits until about 20 commentary Shorts are published. | The owner's taste steers the script from the first week; view numbers mean little on fewer than about 20 Shorts. |
| Fetching clips | yt-dlp (its Twitch clips extractor) through the hardened runner in `clipper.pipeline.ytdlp`, video and audio, for the candidates only. A Twitch clip is fetched in its vertical format (yt-dlp formats `portrait-360` to `portrait-900`, the highest one available). The landscape format is fetched only when the clip has no vertical version. | The runner already handles timeouts, the environment allowlist and format errors. A vertical clip fills the 9:16 frame without a crop. |
| Transcripts | Local faster-whisper on each candidate's audio, word timestamps, same model settings as clips. | Free; the script may only state what the transcript or metadata shows. |
| Script | One `claude -p` call per day with the channel's prompt (the first one is `prompts/commentary_gaming_v1.md`). The call goes through the `claude_cli` selector on the owner's Claude subscription (see "Selector"). It returns up to `stories_per_day` (default 3) stories as JSON validated against a pydantic schema: `hook` (≤ 12 words), `lines` (our commentary lines, each with its time and whether it is spoken or a caption callout), `title` (≤ 70 characters, names the streamer and the game), `description`, `hashtags` (3–5), `clips` (an ordered list of `{clip_id, start_s, end_s}`), and `format` (`hook_type`, `structure`, `topic`; the rendering stage adds `palette`). Title, description and hashtags follow the SEO rules in `src/clipper/pipeline/prompts/playbook/universal.md` ("Titles, captions and hashtags per platform": lead with the searched keyword, 3–5 specific hashtags, no generic reach tags). | One call keeps the subscription use small and lets Claude pick the best stories across all candidates. The SEO pass is part of the script, so it needs no manual step. |
| Clips first | Enforced in code after the script call; a story that breaks one rule is dropped with the reason logged: streamer clips fill the frame for ≥ 70 % of the runtime; script cards and stills take ≤ 20 %; our commentary appears at least every 4 s (a spoken line over the clip or a caption callout); runtime 28–38 s; every streamer and game name in the script appears in a candidate's transcript, title or metadata. The reviewer checks the remaining claims. | Clips are what viewers came for. A line of ours every 4 s keeps each Short original under the 2026-10-01 Shorts originality update, which demotes clips with nothing of the channel's own. |
| Rotation | Hook, structure and palette rotate against the channel's last 5 Shorts. The script prompt gets the `format` of those 5 Shorts. How strictly code enforces the rotation (which repeats drop a story) is an open question. The format is stored in `commentary_stories.format` (migration `0012`), which the insights tags read. | Keeps the channel from looking templated (the inauthentic-content policy). |
| Quality gates | Built in CLP-146 and CLP-149. `better-profanity` checks the script, title and description. A nudity check runs on 2–4 frames per clip with `onnx-community/nsfw_image_detection-ONNX` on onnxruntime (the licence of that ONNX copy is confirmed before use; NudeNet is rejected, AGPL-3.0 on GitHub). `imagehash` perceptual hashes skip clips that nearly duplicate one in an earlier Short. YouTube's limits are enforced before upload: title at most 100 characters and no `<` or `>`, description at most 5000 bytes and no `<` or `>`, tags at most 500 characters in total. A story that fails a gate is dropped with the reason logged. | Catches what the clips-first rules cannot: words, images and repeats that would hurt the channel, and metadata YouTube would reject. |
| Attribution | Every clip is credited. On screen: "Clip: twitch.tv/&lt;login&gt;" (or "Clip: youtube.com/@&lt;handle&gt;" for a YouTube clip) while the clip plays. In the description: one credit line per clip with the clip URL. | Credits the streamer; helps a reviewer at YouTube see the commentary format. |
| Voice | **Kokoro-82M** through the `kokoro-onnx` package (onnxruntime, which faster-whisper already brings), the stock English voice `am_fenrir`, speed 1.0. Model and voice files are fetched by `clipper.pipeline.tts fetch` and pinned by SHA256 in code, like `local_asr fetch` and `reframe.MODEL_SHA256`. `clipper doctor` checks they are present. | Apache-2.0 model, commercial use allowed, CPU real-time factor around 0.5–0.7. No account, no per-character cost. It also pulls `phonemizer` (GPL-3) and loads espeak-ng (GPL-3) as a library; the image is not distributed, so this is noted, not blocking. Fallback voice: Piper (`piper-tts`, GPL-3 engine) with a voice from its COMMERCIAL_USE list, such as the CC0 `en_US-joe`. |
| Word timings | `kokoro-onnx` 0.6.1 returns timings itself: `Kokoro.create_timed()` gives per-phoneme `Timing(phoneme, start, end)` with the model files of the `model-files-v1.1` release (whose ONNX outputs are `waveform` and `duration`). The phonemes are grouped into words and matched to the script with a normalised word match. faster-whisper word timestamps on the TTS audio are the fallback. The SHA256 pins in CLP-147 are for the `model-files-v1.1` files. | Exact timings from the voice itself, without a second transcription; the captions component consumes the same word list. |
| Render sidecar | The render stage writes a valid render sidecar EDL next to the video (`<basename>.json`, as `clipper.render` does for clips). Since [CLP-121](https://linear.app/tengdev/issue/CLP-121) (lands on `dev` with [#97](https://github.com/TetengDev/automated-clipper/pull/97)), a post whose sidecar is missing, unreadable or invalid is held on every platform, at scheduling and at publish, because the AI disclosure flag is sent from the sidecar's EDL. | Without the sidecar, nothing shows whether the Short needs the AI label, so the post must not go out. |
| AI disclosure flag | YouTube's `containsSyntheticMedia` follows [YouTube's policy](https://support.google.com/youtube/answer/14328491). The generic Kokoro narration does **not** set it: generic TTS narration that does not imitate a real person needs no label. Any cloned real voice, and any realistic synthetic footage or image of a real person, **must** set it. Nothing may suppress a required label: no config key, override, review action or code path clears the flag when the sidecar EDL says it is required. | Labels what YouTube asks to be labelled, and only that. |
| Render | New Remotion composition `CommentaryShort`, 1080×1920, 30 fps, runtime 28–38 s. Speech (our line or the streamer's) starts by 0.3 s. A vertical Twitch clip plays full-frame; a landscape clip, used only when no vertical version exists, plays framed over a blurred copy of itself (`BlurredFill`). Our spoken lines play over the clip, with the clip's audio ducked under them. Captions sit at 55–70 % of the frame height: the streamer's speech in white, our lines in yellow. At reactions the frame zooms onto the streamer's face. Sound effects run at about 6 per 10 s. A short hook card opens the Short; script cards and stills stay within the 20 % share. The credit line, a rotating anti-copy watermark (built from the channel's icon, `assets/brand/random_gaming_yt/icon.png`, [CLP-155](https://linear.app/tengdev/issue/CLP-155); CLP-148 defines how it rotates), and the progress bar are on screen. Loudness −13 LUFS. Optional background music from `music_dir`, ducked to −20 dB under speech. | Reuses `Captions`, `HookCard`, `ProgressBar`, `BlurredFill` and `FaceTrack` in `render/src/`. Face zoom and SFX at about 6 per 10 s are the owner's choice (2026-11-20) and override the editing playbook's cap of at most 1 SFX and 1 zoom per 10 s for this channel. |
| Thumbnail | The render stage also makes a custom 9:16 thumbnail. Its design is CLP-148's to propose. After the upload, the publisher sends it with the Data API's `thumbnails.set`. | A custom cover for the Shorts feed and the channel page, made with no manual step. |
| Music | Only files in the configured `music_dir`, which the owner fills from the YouTube Audio Library with the "Attribution not required" filter. These are used on YouTube only, until reuse on other platforms is checked. Ducking is a per-frame volume curve in Remotion (`interpolate`); loudness stays the existing two-pass ffmpeg `loudnorm`, with a −13 LUFS target for commentary Shorts. Empty or unset means no music. | No music the code cannot account for. |
| Dependencies | No dependency that needs PyTorch. Models run on onnxruntime, which faster-whisper already brings. | PyTorch adds about 1 GB to the CPU image. |
| Storage | Each story becomes a `clips` row under the day's job (`rank` 1..3) with its version in `clip_versions` (`start_s = 0`, `end_s` = runtime). `commentary_stories` holds the version's script, clip plan, clips-first figures and `format`. `story_candidates` holds every discovered Twitch clip (unique on the Twitch clip id) with its score, whether its streamer is pinned, and the clip id of the Short that used it. `streamer_rankings` holds each day's trending-streamer ranking. These tables are in migrations `0011` and `0012`. New fields (for example loudness peaks or the thumbnail path) go in a later migration. YouTube moments (CLP-152) get their own storage in a later migration. | The review app, export, janitor and posting all key on `clips`; they work with no new path. |
| Review | The clip page for a commentary clip shows the script, each clip with a link to its Twitch clip, its score and rank, the clips-first figures, the day's trending-streamer ranking, and a checklist (facts match the clips, nothing sensitive, credit lines right). Approve is one tap. Reject and "regenerate with note" work as for clips; regenerate re-runs `scripting` for that one story with the note. One Discord ping per day when the Shorts are ready: "N commentary Shorts ready". There is no auto-approve. | The owner's one manual step, from the phone. |
| Insights tags | At approval, the review app tags the post with `tag_post` (`src/clipper/insights/tags.py`, [CLP-154](https://linear.app/tengdev/issue/CLP-154)) from the story's `format`, clips and slot. | The insights page compares each Short with the channel's median, with no manual tagging. |
| Selector | Scripting runs `claude -p` on the owner's Claude subscription: the `claude_cli` selector (`Selector = Literal["api", "claude_cli"]` in `src/clipper/config.py`; the global default is `"api"`). The commentary path always uses `claude_cli`, whatever the global `selector` says. It never requires or suggests `ANTHROPIC_API_KEY`. The image already installs a pinned `claude` binary (`Dockerfile`). **Open risk:** the CLI must run headless inside the worker container, authenticated with `CLAUDE_CODE_OAUTH_TOKEN`; this is not yet proven for the commentary prompt. | The subscription has no per-call cost. |
| Posting | See "Posting for commentary accounts (CLP-149)". | |
| Channel check | `accounts.yaml` has an optional `channel_id` for YouTube accounts (set for every commentary account). Before an upload, the publisher compares it with the channel the refresh token belongs to (`channels.list mine=true`, which the duplicate check `_find_tagged` in `src/clipper/posting/youtube.py` already calls; 1 quota unit, cached with the access token). A mismatch pauses the account with one alert, like a rejected token. Built in phase 5 (see "Always-on requirements"). | With several channels under one Google account, the OAuth Playground makes it easy to authorise the wrong channel; this stops a Short from going to the wrong channel. |
| Failure | Twitch errors defer the job with backoff. No candidate is a normal skip (see "No candidate"). When candidates exist but no story passes the clips-first rules or the quality gates, the job is skipped with the reason and one alert ("no commentary Short today: …"). TTS or render failures fail the job after the usual retries. | The owner learns about a broken day the same morning, and is not alerted on a quiet one. |

## Posting for commentary accounts (CLP-149)

Today three places keep commentary out of posting. CLP-149 lifts each one for commentary
accounts only:

1. `src/clipper/posting/scheduler.py`: `_clip_problem` refuses every account whose `content`
   is not `clips`, and its caller `schedule_for_clip` also requires
   `clip.creator in a.creators`. CLP-149 schedules a commentary clip only to the commentary
   account whose id is the job's `creator` (the daily job sets `creator = <account_id>`), and
   a creator's clip only to clip accounts, as today.
2. `src/clipper/posting/publishers.py`: `build_publishers` registers the YouTube publisher
   only for the accounts `_clip_accounts` returns, which are `content == "clips"`. CLP-149
   registers enabled commentary accounts too.
3. `src/clipper/posting/youtube.py`: `YouTubePublisher.publish()` refuses any account that is
   not in its `account_ids`. Item 2 puts the commentary accounts in that list.

CLP-149 keeps the CLP-115/CLP-120 behaviour: the approved slot is kept in `posts.publish_at`
(migration `0013`), YouTube uploads ahead as private with `publishAt` set to the slot, and a
post never goes live before its slot. On approve, the scheduler picks the slot. After the
upload, the publisher sends the custom thumbnail with `thumbnails.set`.

A commentary account posts at most 3 Shorts a day, at least 2 h apart: `posts_per_day: 3`,
`min_gap_minutes: 120`. Today a commentary account defaults to one post a day
(`COMMENTARY_POSTS_PER_DAY = 1` in `src/clipper/accounts.py`) in the US YouTube windows
(10:00–11:00 and 13:00–14:00), which hold only 2 posts 2 h apart. CLP-149 changes the
default to 3 posts and a 120-minute gap, in the windows 12:00–13:00, 15:00–16:00 and
19:00–20:00 New York time (see "Settled during the 2026-11-24 review"). The usual warm-up ramp still applies. Campaign rules (no AI content) stay away from this channel, and this
channel's TTS stays away from campaign accounts.

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

`config/commentary.yaml`, validated by `clipper check-config`. The keys marked "new" and the
new defaults are added by CLP-144 (virality bar, score floor; shipped) and CLP-147 (voice);
today's code (`src/clipper/commentary/config.py`) still has `voice: am_michael`.

```yaml
defaults:                    # apply to every channel unless the channel's overrides change them
  run_at: "06:00"            # local time of the channel's account
  candidates_per_day: 12
  stories_per_day: 3
  trending_boost: 0.5        # the clip's game is in Twitch's top 20 today
  pinned_boost: 0.5          # the clip's streamer is pinned
  min_clip_views: 10000      # virality bar: at least this many views ...
  min_views_per_hour: 400    # new; ... or at least this many views an hour (10,000 / 24 h, rounded down)
  min_score: null            # new; score floor, default is an open question
  max_streamers_scanned: 50  # trending streamers kept from each day's scan
  excluded_game_ids: []      # added to the gambling categories the code always excludes
  voice: am_fenrir
  music_dir: null            # e.g. /data/music; null = no music
channels:
  - account: random_gaming_yt   # an accounts.yaml id with content: commentary
    source: twitch_clips        # discovery source; the only one at go-live
    prompt: commentary_gaming_v1
    games:                      # at least one
      - name: Grand Theft Auto VI
        aliases: [GTA 6, GTA VI]  # how YouTube titles name it (CLP-152)
        game_id: "1867279146"     # Twitch's id; `clipper resolve` confirms it
    streamers:                  # pinned: always scanned, pinned_boost; may be empty
      - name: Kai Cenat         # the first Twitch pin
        twitch: kaicenat        # login; user_id filled in by `clipper resolve`
      - name: IShowSpeed
        youtube:                # a YouTube streamer, scanned by CLP-152 after go-live
          handle: "@IShowSpeed"
          channel_id: UCWsDFcIhY2DBi3GB5uykGXA
    exclude_streamers: []       # Twitch logins or YouTube handles never used
    overrides: {}               # any default, for this channel only
```

Each channel names its account, its discovery `source`, its script `prompt` and that
source's inputs, and may change any default in `overrides` (for example another `voice` or
`run_at`). For `twitch_clips` the inputs are `games` (the Twitch categories the channel
covers, at least one, each with optional `aliases` and its Twitch `game_id`), `streamers`
(pinned streamers, optional, each with exactly one of a Twitch login or a YouTube channel)
and `exclude_streamers` (Twitch logins or YouTube handles never used). A new channel with
other content is a new `channels` entry and, if it needs one, a new source or prompt; the
stages stay the same. check-config rejects a channel whose account is missing, is not
`content: commentary`, or is used by two channels; a `twitch_clips` channel with no game; a
pinned streamer with both or neither of `twitch` and `youtube`; duplicate games, aliases,
game ids, logins, handles or channel ids; a login that is not a valid Twitch login; and a
streamer who is both pinned and excluded.

The games and the pinned list are the owner's call; the first game is Grand Theft Auto VI,
the first Twitch pin is Kai Cenat, and the full pinned list is in `config/commentary.yaml`.
A pinned Twitch streamer is found through Twitch. A pinned YouTube streamer, such as
IShowSpeed, is found by [CLP-152](https://linear.app/tengdev/issue/CLP-152) after go-live;
until it ships, check-config notes that the pin is not scanned yet. Either way, only the
streamer's moments of the channel's games count. A streamer who asks to be left out is added
to `exclude_streamers`, and their unused candidates are dropped.

## Phase 1 render spike

Remotion with Chrome, and Kokoro, inside the Linux worker container are unproven for this
composition. The image already fetches Remotion's pinned `chrome-headless-shell`
(`npx --no-install remotion browser ensure` in the `Dockerfile`), and clip renders work there, but an
earlier local batch of commentary renders hit a Remotion "Target closed" error. The spike
settles where commentary Shorts render before CLP-147 and CLP-148 are built.

**Test.** In the worker container built from the current `Dockerfile`, on the production
compose settings (`docker-compose.yml`):

1. Voice a 38 s script with Kokoro `am_fenrir` and get word timings from `create_timed`.
2. Render a throwaway `CommentaryShort` prototype from it: 1080×1920, 30 fps, 38 s, a
   vertical Twitch clip full-frame, captions in white and yellow, three face zooms, about 6
   SFX per 10 s, the watermark, then `loudnorm` to −13 LUFS.
3. Run step 2 three times in a row, and once with `max_concurrent_renders` renders at once.

**Pass criteria.** All of these hold:

- Every render finishes with no "Target closed" or other Chrome error, and no retry.
- Each output is 1080×1920 at 30 fps, 28–38 s long, at −13 LUFS ±1 with true peak at most
  −1 dBTP.
- Kokoro runs in the container and its timings line up with the audio.
- The container stays within its memory limit.
- The spike records the render time and peak memory of each run, so CLP-148 can size the job
  lease and `max_concurrent_renders`.

**Fallback.** If any criterion fails and the cause is not fixed within the spike, voicing and
rendering run in a render worker on the Mac host (outside Docker), sharing the media root
with the container. The container keeps discovery, scripting, review and posting. A Mac
render worker can be asleep, so phase 5's back-fill requirement then also covers renders.

## Always-on requirements (phase 5)

The goal is that approval is the only manual step. These gaps stop that today, and phase 5
closes them:

1. **Automatic recheck of held posts.** Today only `clipper posts recheck` (`recheck_held`
   in `src/clipper/posting/scheduler.py`) re-checks held posts. The worker must do it on its
   own.
2. **Retry or back-fill a missed day.** When the host was asleep or down at `run_at` until
   local midnight, that day gets no job: `src/clipper/commentary/daily.py` lines 9–10 say
   there is no back-fill. The monitor must retry or back-fill the missed day.
3. **Channel-ownership check before upload.** The "Channel check" decision above. It ships
   earlier, with CLP-149, before the first commentary upload.
4. **Config reload without a restart.** Today `run-monitor` reads `accounts.yaml` and
   `commentary.yaml` once, at start. An edit must take effect without a restart.
5. **OAuth app "In production".** The Google OAuth consent screen must be published to
   Production: a Testing-mode app's refresh tokens expire after 7 days, which would stop
   uploads every week.

## Out of scope

- Hybrid clips on this channel, and any clipping-program campaign for it (later epic).
- Platforms other than YouTube Shorts for commentary (TikTok and Reels later, through the
  existing publishers).
- X as a discovery source, and open YouTube trending (`search.list`). YouTube (CLP-152) and
  Kick (CLP-153) come after go-live.
- Open trending discovery across all games (the scan covers each channel's games only),
  auto-approve, or a cloned voice. If a cloned voice is ever added, it must set the AI flag.
- Comments and community posts.

## Testing

- Unit tests with httpx `MockTransport` fixtures for the Helix calls (pagination, an expired
  app token, HTTP errors), scoring, the virality bar, the score floor, filters and the
  clips-first checks.
- The scripting stage with a fake selector returning fixed JSON, including invalid stories
  and stories that break the clips-first rules.
- The AI flag: a sidecar with generic Kokoro narration sends no flag; a sidecar with a cloned
  voice or realistic synthetic footage sends it; a missing or invalid sidecar holds the post.
- The TTS stage and a `CommentaryShort` render under the existing `slow` and `render` marks,
  so the nightly job runs them with the real Kokoro model and Remotion.
- Review app tests for the commentary clip page and the approve-to-schedule path, plus one
  browser test in the e2e job.
- Live acceptance, by hand: one real day on the owner's channel, from discovery to a public
  Short at its `publishAt` time.

## Stories

1. **Config, accounts and storage.** [CLP-143](https://linear.app/tengdev/issue/CLP-143)
   (shipped): `config/commentary.yaml` (per channel), `content:` and `channel_id` in
   `accounts.yaml`, migration `0011` (`story_candidates`, `streamer_rankings`,
   `commentary_stories`), the new job statuses, and the monitor creating the daily job.
2. **Twitch discovery.** [CLP-144](https://linear.app/tengdev/issue/CLP-144): the
   `discovering` stage: the game scan (Get Clips by `game_id`), pinned Twitch streamers'
   clips of the channel's games, the trending-streamer ranking, Get Top Games, Get Channel
   Information, the virality bar (`min_clip_views` 10,000, `min_views_per_hour`), velocity
   scoring with boosts, the score floor, filters, `story_candidates` and `streamer_rankings`
   rows, and the quiet-day skip. Extends `clipper resolve` to the channels' games and pinned
   streamers.
3. **Fetch and transcribe candidates.** [CLP-145](https://linear.app/tengdev/issue/CLP-145):
   the `fetching_sources` and `transcribing` stages for commentary jobs, vertical Twitch
   formats first, and loudness peaks per candidate.
4. **Script stage.** [CLP-146](https://linear.app/tengdev/issue/CLP-146):
   `prompts/commentary_gaming_v1.md` (prompt per channel) with the SEO rules, score, rank,
   loudness peaks, the last 5 formats and the owner's decisions as `feedback_examples`; the
   JSON schema; the clips-first checks; `commentary_stories` rows; the profanity gate; always
   on the `claude_cli` selector.
5. **Local TTS.** [CLP-147](https://linear.app/tengdev/issue/CLP-147):
   `clipper.pipeline.tts` with Kokoro ONNX and the `am_fenrir` voice, `tts fetch` with pinned
   hashes, the `clipper doctor` check, the `voicing` stage and word timings from
   `create_timed` (faster-whisper as fallback), pinned to the `model-files-v1.1` files.
   Dockerfile and macOS setup.
6. **CommentaryShort render.** [CLP-148](https://linear.app/tengdev/issue/CLP-148): the
   Remotion composition (clips first, vertical full-frame, white and yellow captions, face
   zoom, SFX, rotating watermark), music ducking, −13 LUFS, the custom 9:16 thumbnail, the
   render sidecar EDL with the AI flag rule, and the `rendering` stage for commentary jobs,
   where the phase 1 spike says it runs.
7. **Review and posting.** [CLP-149](https://linear.app/tengdev/issue/CLP-149): the
   commentary clip page, checklist, one-tap approve, daily Discord ping,
   regenerate-with-note for one story, insights tags at approval, lifting the three posting
   exclusions (see "Posting for commentary accounts"), the 3-a-day 2-hour plan,
   `thumbnails.set`, and the nudity, near-duplicate and YouTube metadata gates.
8. **Setup guide and live run.** [CLP-150](https://linear.app/tengdev/issue/CLP-150): the
   "Commentary channel" section of `docs/DEPLOYMENT.md` (accounts below), the nightly slow
   test, and the first live day.
9. **Pinned YouTube streamers.** [CLP-152](https://linear.app/tengdev/issue/CLP-152), after
   go-live: discovery for pinned YouTube streamers through the existing YouTube source (RSS
   and yt-dlp metadata), game matching on titles and descriptions with the games' aliases,
   "most replayed" heatmap peaks as moments, clips from the existing section download, the
   "Clip: youtube.com/@&lt;handle&gt;" credit, and their storage.
10. **Kick discovery.** [CLP-153](https://linear.app/tengdev/issue/CLP-153), after go-live.
11. **Brand assets and anti-copy watermark.** [CLP-155](https://linear.app/tengdev/issue/CLP-155)
    (shipped): `assets/brand/random_gaming_yt/`.

## Build order

| Phase | Work |
|---|---|
| 0 | This spec update. |
| 1 | The render spike (see "Phase 1 render spike"). |
| 2 | [CLP-144](https://linear.app/tengdev/issue/CLP-144), [CLP-145](https://linear.app/tengdev/issue/CLP-145), [CLP-146](https://linear.app/tengdev/issue/CLP-146). |
| 3 | [CLP-147](https://linear.app/tengdev/issue/CLP-147), [CLP-148](https://linear.app/tengdev/issue/CLP-148). |
| 4 | [CLP-149](https://linear.app/tengdev/issue/CLP-149). |
| 5 | Always-on (see "Always-on requirements"). |
| 6 | [CLP-152](https://linear.app/tengdev/issue/CLP-152), [CLP-153](https://linear.app/tengdev/issue/CLP-153). |

[CLP-150](https://linear.app/tengdev/issue/CLP-150) (setup guide and first live day) closes
phase 5. Go-live follows it.

## Settled during the 2026-11-24 review

1. **Pinned first.** Clips by pinned streamers that pass the virality bar sort above every
   other clip; other streamers fill in only when a pinned streamer has nothing viral (owner,
   2026-11-19). `pinned_boost` still orders clips within each group.
2. **`thumbnails.set` scope.** No new scope is needed: `thumbnails.set` worked on a Random
   Gaming Short with the `youtube.upload` scope on 2026-11-19.
3. **Channel check timing.** The channel-ownership check ships with CLP-149, before the
   first commentary upload, not in phase 5.
4. **CLP-150 phase.** The setup guide and first live day are the last step of phase 5;
   go-live follows it.
5. **SFX source.** The approved sample Shorts used SFX synthesized in code, which need no
   licence. CLP-148 keeps that, or names any file it adds with its licence.
6. **Quality gates on commentary renders.** The CLP-74 gates (`src/clipper/pipeline/quality.py`)
   run on commentary renders with the commentary targets: −13 LUFS ±1 and speech by 0.3 s.
7. **Posting windows** (owner, 2026-11-24). A commentary account posts in three windows,
   New York time: 12:00–13:00, 15:00–16:00 and 19:00–20:00 (lunch, after school, evening).
   They are moved once insights shows which publish hours do best, after about 20 Shorts.

## Open questions

Decided inside the named story, with the reasoning in its PR:

1. **Score floor** (CLP-144). The default `min_score`, proposed from the first week's scores.
2. **Script length** (CLP-146). How many words of our commentary fit a 28–38 s Short when a
   line lands every 4 s over the clip. The old 60–120-word narration no longer applies.
3. **Rotation rule** (CLP-146). Which repeats against the last 5 Shorts drop a story: the
   same hook type, the same structure, the same palette, or a combination.
4. **AI flag in the sidecar EDL** (CLP-148). Today `has_ai_elements` (`src/clipper/edl.py`)
   counts any synthetic hook voice as an AI element, which campaign AI bans also read. The
   commentary sidecar must record "generic TTS, no YouTube label" without weakening that
   check, while a cloned voice or realistic synthetic footage still sets the flag. The
   sidecar's `edl` must also pass `parse_edl`, and today's `EDL` schema describes one
   `source` timeline for the `Clip` composition. A Short built from several clips needs a
   schema that still validates there, or every post is held.

## Owner setup

One-time setup. After it, approval is the only manual step.

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
6. **Claude subscription token.** `CLAUDE_CODE_OAUTH_TOKEN` (already set locally), for the
   `claude_cli` selector. No `ANTHROPIC_API_KEY`.
7. **Games and streamers** for `config/commentary.yaml`: the games to cover (first: Grand
   Theft Auto VI), the pinned streamers (the owner's top ten on Twitch, Kai Cenat first, plus
   IShowSpeed on YouTube, chosen 2026-11-19) and any streamers to exclude.
8. **Music** (optional): 10–20 YouTube Audio Library tracks that need no attribution, in one
   folder mounted at `music_dir`.

No TTS account is needed.
