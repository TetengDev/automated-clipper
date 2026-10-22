# Spec: open-source transcription and editing stack

Status: accepted for implementation, 2026-10-22. Research was done by two Opus research agents on 2026-10-22. Star counts and licences were checked with the GitHub API on that date.

## Goal

Make the clipper free to run apart from Claude, and raise clip quality, using only widely used open-source projects. "Widely used" means at least 1,000 GitHub stars, a permissive licence, and maintenance within the last 12 months.

## Decisions

| Area | Decision | Project (stars, licence) | Rejected and why |
|---|---|---|---|
| Transcription | **Local faster-whisper is the default backend**, with `large-v3-turbo`, int8, on CPU. Groq stays as an opt-in backend. | SYSTRAN/faster-whisper (26k, MIT) | WhisperX: heavy (PyTorch, pyannote). whisper.cpp: Python binding effort. Distil-Whisper and Parakeet: English only. whisper-timestamped: AGPL. stable-ts: archived. |
| Highlight selection | **Keep Claude.** | n/a | A local 7B model on 4 vCPU takes about 6–14 min per 30 min window and needs about 5–6 GB of RAM. Its Taglish quality is unproven. |
| Selection cost on long sources | **Rank windows by engagement signals** (YouTube heatmap, loudness peaks) and send only the best windows to Claude. | yt-dlp heatmap, ffmpeg `ebur128` (both already collected) | |
| Clip boundaries | **Snap to scene cuts and trim dead air at the head and tail**, after the LLM has picked the clip. | Breakthrough/PySceneDetect (5.3k, BSD-3) | auto-editor: we take only the idea and use word gaps instead. |
| Reframe | **Face-tracked 9:16 crop** with a smoothed path. It falls back to blurred fill. | google-ai-edge/mediapipe (37k, Apache-2.0) | ultralytics YOLO: AGPL. InsightFace: non-commercial models. AutoFlip: support ended in 2023. |
| Rendering | **Keep Remotion.** | remotion-dev/remotion (66k, Remotion License) | Remotion is free for individuals and companies of up to 3 people. Its 5.0 draft counts contractors toward that limit. The ffmpeg + libass path is tracked as a spike, in case the team grows. |
| Loudness | Already done: two-pass `loudnorm` to -14 LUFS in `render.py`. | FFmpeg | |

## Stories and acceptance criteria

### A. CLP-35: local faster-whisper as the default transcriber

- A `transcriber: local | groq` setting, defaulting to `local`, with a per-creator override. Settings `whisper_model` (default `large-v3-turbo`) and `whisper_compute_type` (default `int8`).
- `FasterWhisperTranscriber` implements the existing `Transcriber` protocol. It reuses the existing chunking, stitching, caching and `transcript.json` schema, so the transcript output is the same whichever backend runs.
- Guards against hallucination:
  - `vad_filter=True` and `condition_on_previous_text=False`;
  - drop segments with `no_speech_prob > 0.6` and an average log-prob below -1;
  - drop segments whose compression ratio is above 2.4;
  - drop words in an unexpected script (anything outside Latin) when the creator's language is `en` or `tl`.
- The model is loaded once per worker process. Files are cached under `data/models/`, which is a persistent volume in compose. Spend is recorded as $0 with the provider `local`.
- `GROQ_API_KEY` is required only when `transcriber=groq`.
- `clipper bench-transcribe <audio> --models large-v3-turbo,large-v3` prints the real-time factor and the first 300 characters of each transcript, so the models can be compared side by side.
- Tests:
  - unit tests for the guard filters, the backend selection and the missing-key message;
  - an integration test that transcribes the 30 s fixture with `tiny`, marked `slow` and skipped when the model is absent;
  - the stitch and schema tests run with the local backend through a fake model.

### B. CLP-36: rank windows by engagement signals

- Heatmap and loudness hints already reach the prompt. This story adds a pre-ranking step:
  - Score each window by the mean of its top-3 normalised heatmap values plus its normalised loudness-peak density.
  - When a source has more windows than `max_llm_windows` (default 8, with a per-creator override), send only the top `max_llm_windows` windows to Claude.
  - Always include the first window, because hooks often sit in intros.
- With no signals at all, keep today's behaviour: every window, up to `MAX_WINDOWS`.
- Log which windows were skipped and why. The budget estimate counts only the windows that will be sent.
- Tests: ranking on synthetic signals; the no-signal path; the first window always kept; the cap respected; a cached paid window is never re-billed.

### C. CLP-68: clip boundary polish (scene-cut snap and dead-air trim)

- Runs in the render stage on the padded section that has already been downloaded, before the EDL is built.
- Scene snap: run PySceneDetect `ContentDetector` on the padded section, downscaled. If a cut lies within ±1.0 s of the clip start or end, move that boundary onto the cut, but only if the result stays on a word boundary and within `[clip_min_seconds, clip_max_seconds]`.
- Dead-air trim: start no more than 0.25 s before the first word, and end no more than 0.4 s after the last word.
- Order: a trim that knows about cuts runs first, then the snap. The trim stops at a cut between an edge and the nearest word, so it never leaves a flash of another shot at the edge. The snap never adds or drops a whole word, and an end snapped to a cut stops half a frame before it. The result is that every edge lands either on a cut or at the trim limit (changed in review from "snap, then trim").
- Both steps are pure functions over (boundaries, words, cut list) and are unit-tested. Scene detection gets a hard timeout. On a timeout or error the clip renders with the original boundaries and a warning is logged.
- Tests:
  - unit tests for snapping: a cut inside or outside the tolerance, a snap that would break min/max, a snap that would land mid-word;
  - unit tests for the trim;
  - an integration test on a fixture with a known hard cut, marked `slow`.

### D. CLP-58: face-tracked reframe

- Uses the MediaPipe Face Detector (BlazeFace short-range, through the Tasks API) at 5 fps on frames downscaled to 640 px.
- Crop path: largest face (or the centroid of 2), then an EMA with a dead-zone, a maximum speed per frame, and a reset at scene cuts (from story C).
- Layout choice: `face_track` when faces appear in at least 60% of sampled frames. Otherwise use `blurred_fill`.
- The EDL gains `layout=face_track` and `crop: [{t_s, x_center}]` keyframes. Remotion interpolates the crop.
- The model file is fetched once into `data/models/` and verified with a SHA-256 checksum.
- Tests:
  - unit tests for the smoothing function (dead-zone, speed cap, reset) and the layout choice;
  - Remotion vitest coverage of crop interpolation;
  - an integration test on a fixture with a moving face.

### Later (filed, not in this batch)

- CLP-53: caption style library (keyword emphasis, emoji). Unchanged, Remotion.
- CLP-69: Twitch chat-spike signal (needs the S3 Twitch source).
- CLP-70: spike on an ffmpeg + libass render path as a Remotion fallback. Trigger: the team grows past 3 people, or Remotion 5 changes the licence terms.

## Delivery

- One PR per story into `dev`, each written test-first: failing tests are committed before the implementation.
- Each PR gets an independent code review and security review on Opus, at most 3 passes. The review record goes on the PR and on Linear. A human merges.
- A, B and C touch separate modules and run in parallel. D builds on C, because it uses the scene-cut list.
