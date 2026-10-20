# PRD — Automated Clipper (v1)

Linear project: [Automated Clipper](https://linear.app/tengdev/project/automated-clipper-5a6e5d6d4211) (team CLP)

## Problem

Clipping long videos and streams by hand is slow, and the best moments go stale within hours. Six high-volume creators produce many hours of content every day, including 6–12 h Kai Cenat and IShowSpeed streams.

## Goal

New content from the tracked creators becomes reviewable, ready-to-post vertical clips within **30 minutes** of upload. For streams, the clock starts when the VOD becomes available. The system runs unattended 24/7, and a human makes the final approve/reject call.

## Creators (v1)

| Creator | Platform(s) | Region |
|---|---|---|
| MrBeast | YouTube | global |
| IShowSpeed | YouTube (streams + videos) | global |
| Kai Cenat | Twitch (VODs), YouTube | global |
| Raffy Tulfo in Action | YouTube | PH |
| Ivana Alawi | YouTube | PH |
| Cong TV | YouTube | PH |

## Users

- **Owner / Reviewer** (one person in v1). Reviews clips on phone or desktop, approves, rejects, or requests edits, then posts the approved clips.
- **Operator** (the same person). Adds creators, watches costs and health, and deploys.

## User stories

- As a reviewer, I get a Discord ping when new clips are ready, so I can review them quickly.
- As a reviewer, I can watch a clip, see why it was picked (score and reason), and approve, reject, or request changes from my phone.
- As a reviewer, I can comment, adjust the trim, title, or caption, or ask for a regeneration, and get a new version back.
- As a reviewer, approved clips land in a `ready/` folder with caption and hashtags, so I can post right away.
- As an operator, I add a creator by editing YAML only.
- As an operator, I get alerted when the system is down, downloads are blocked, the disk is nearly full, or a budget is exceeded.
- As an operator, I can see daily spend and clip throughput.

## Functional requirements

1. **Detection**
   - Poll every 5–10 minutes.
   - YouTube uses an RSS gate plus YouTube Data API v3. `search.list` is never used.
   - Twitch uses Helix.
   - Items are deduplicated by (platform, id).
   - Live items wait for their VOD.
   - The first run records a baseline and does not process the backlog.
2. **Processing**
   - Download audio first.
   - Transcribe with a hosted Whisper API that returns word timestamps.
   - Claude selects the highlights.
   - Download only the selected sections of video.
   - A second Claude "editor" pass turns each highlight into an Edit Decision List (EDL) using the content playbook for that content type (hook in the first 1–3 s, pattern interrupts every 3–5 s, open loop and payoff).
   - Render with Remotion (React/TypeScript in headless Chromium) at 9:16, 1080x1920, 30 fps. Formats: hook card plus animated captions, split-screen, punch-in zooms, b-roll cutaways (from the source video and from Pexels/Pixabay stock), licensed meme, emoji and SFX overlays, and a face-tracked reframe. ffmpeg handles loudness normalization (-14 LUFS).
3. **Review**
   - Clip states: pending, approved, rejected, changes requested.
   - Reviewers can comment, trim by ±10 s, edit the title and caption, or "regenerate with note".
   - Reviewers can see the EDL and toggle editing elements (layout, hook text, caption style, b-roll, SFX, emoji), which triggers a re-render.
   - Version history is kept for every clip.
4. **Export**
   - On approve, copy the clip to `ready/<date>/` together with `caption.txt` and `clip.json`.
   - Optional rclone sync to Google Drive.
5. **Operations**
   - Daily and per-creator budgets.
   - A janitor job that enforces a disk budget.
   - Health pings, Discord alerts, and a status page.

## Non-goals (v1)

- Auto-posting to TikTok, Shorts, or Reels.
- Real-time live clipping.
- AI presenters (Arcads). This is limited to a feature-flagged spike (E7-S11) and is off by default.
- Multiple users or roles.
- Twitch chat signals.

## Non-functional requirements

- **Freshness:** P90 time from detection to `pending_review` is 30 minutes or less for videos up to 1 h, and 2 hours or less for 12 h streams.
- **Reliability:**
  - Runs 7 days unattended.
  - After a crash or restart, work resumes from the last completed stage.
  - No duplicate clips.
- **Cost:** daily caps on LLM and transcription spend are enforced, and spend per clip is logged.
- **Security:**
  - The review app is served over HTTPS only and requires login.
  - Secrets live only in `.env` on the server.
  - The firewall opens ports 22, 80, and 443 only.

## Success metrics

- Approval rate of 40% or more after prompt tuning.
- At least 1 approved clip per creator for each day that has new content.
- Median review time under 30 seconds per clip.
- Cost per approved clip is tracked, with a target set after week 1.

## Risks

- **YouTube blocks VPS (datacenter) IPs.** This is the biggest risk. Mitigations: use cookies, the PO-token plugin, or a residential proxy, or run the worker from home. An alert and a runbook entry cover this case.
- **yt-dlp breaks when platforms change.** Mitigation: a nightly rebuild with a smoke test.
- **Copyright and terms-of-service exposure.** Nothing is posted without human approval, and every clip carries attribution in its sidecar. Use clipping programs that creators sanction, or get permission.
- **Costs run away on long streams.** Mitigations: audio-only transcription, a windowed LLM pass, and hard daily caps.
- **Twitch VODs expire.** Mitigation: fetch audio promptly after detection.
- **Highlight quality is poor.** Mitigations: heatmap and loudness signals, a review feedback loop, and a dedicated prompt-tuning milestone.

## Milestones

Each milestone is a vertical slice that works end to end.

| Milestone | Target date | Scope |
|---|---|---|
| S0 Scaffold | 2026-10-22 | Repo, config, database, CLI, CI |
| S1 First clip end-to-end | 2026-10-31 | `enqueue <url>` → Remotion clip (hook card + animated captions) → review → export |
| S2 24/7 YouTube detection | 2026-11-04 | Monitor daemon for YouTube |
| S3 Twitch + live VODs | 2026-11-06 | Helix source, wait for VOD |
| S4 Full review workflow | 2026-11-11 | Comments, edit, regenerate, Discord, status page |
| S4b Creative formats & motion graphics | 2026-11-18 | Content playbook, Claude editor pass (EDL), caption styles, split-screen, zooms, b-roll, overlays, face reframe |
| S5 Robustness | 2026-11-21 | Leases, budgets, janitor, fallback transcription, signals, feedback loop |
| S6 Production deploy | 2026-11-26 | VPS, HTTPS, CI/CD, alerts, backups |
| S7 Hardening + launch | 2026-12-07 | Chaos tests, tuning, soak, 7-day unattended run |
