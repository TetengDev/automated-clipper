# Review Workflow

Every clip is reviewed by a person before anything is exported. Review happens in a small web app (FastAPI + Jinja2 + HTMX). It is served behind Caddy, which provides HTTPS and basic_auth, and the app has its own session login on top of that.

## Flow

1. The worker finishes rendering a source. Each rendered clip first runs the quality gates (CLP-74): captions match the EDL words, no word crosses a jump-cut join, speech starts within 0.25 s, loudness is -14 LUFS ±1 with true peak at most -1 dBTP, and a `tiny` Whisper pass around each join hears the EDL's words. A clip that passes gets status `pending_review`. A clip that fails a gate gets `needs_attention`, and the clip page lists the reasons; it can still be approved or rejected. A gate that cannot run (for example, an ffmpeg timeout) is logged and shown as `gate_error`, but the clip still goes to `pending_review`.
2. Discord receives one message per source batch, with deep links to each `/clip/{id}`.
3. The reviewer opens the queue or a clip and takes one of these actions:
   - **Approve**: the clip becomes `approved`, it is exported to `output/ready/<date>/`, and its status becomes `exported`.
   - **Reject**: a reason tag is required. Valid tags are `boring`, `bad_cut`, `bad_captions`, `duplicate`, `unsafe`, `other`. The clip becomes `rejected`.
   - **Edit**: change the trim (±10 s), title, caption, hashtags, or turn captions on/off. The clip becomes `changes_requested`. It is then re-rendered as version n+1 and returns to `pending_review`.
   - **Adjust edit (EDL)**: switch the layout (blurred fill, face-track, split-screen), edit the hook text, change the caption style, or toggle b-roll, SFX, and emoji (all at once or per cue). The clip is re-rendered as version n+1.
   - **Regenerate with note**: Claude picks a new cut within ±60 s of the original, guided by the reviewer's note. This also produces version n+1 in `pending_review`.
   - **Comment**: adds to the clip's thread. Comments persist across all versions of the clip.
4. Pending clips older than `review_expiry_days` (default 7) are archived automatically.

## Clip state machine

```
needs_attention ──approve / reject──▶ (as pending_review)

pending_review ──approve──▶ approved ──export──▶ exported
      │
      ├──reject (reason)──▶ rejected
      │
      └──edit / regenerate──▶ changes_requested ──▶ re_rendering ──▶ pending_review (v n+1)
```

## Pages

| Route | Purpose |
|---|---|
| `/login` | Single-user login. Rate-limited. |
| `/queue` | Pending clips, filterable by creator, status, and minimum score. Sort by newest or by score. |
| `/clip/{id}` | Player, metadata, Claude's reason, source link with timestamp, versions, comments, and the actions above. |
| `/status` | Job states, failures with a retry button, today's spend, disk use, and last poll per creator. |
| `/healthz` | Liveness check. No auth, and it returns no data. |

Keyboard shortcuts: `a` approve, `r` reject, `e` edit, `j`/`k` next/previous.

## Export package

Each export writes three files to `output/ready/<YYYY-MM-DD>/`:

- `<creator>_<slug>_v<n>.mp4`
- `<creator>_<slug>_v<n>.caption.txt`, containing the caption and hashtags, ready to paste
- `<creator>_<slug>_v<n>.json`, containing the title, source URL with timestamp, creator, score, reason, and prompt version

If `RCLONE_REMOTE` is set, the files are also copied to Drive.

## Feedback loop

Approvals, rejection reasons, and comments are turned into per-creator style notes. These go into Claude's cached system prompt and are refreshed daily, so that picks improve over time.

## Data

These tables hold review state: `clips`, `clip_versions`, `clip_reviews` (audit trail of actions with timestamps), and `clip_comments`.
