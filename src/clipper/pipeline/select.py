"""SELECTING stage: ask Claude for clip-worthy moments, then make them safe to render (CLP-15).

Reads `transcript.json` (required), `meta.json` and `signals.json` (optional hints) from
the job's media dir and writes `highlights.json`:

    {"model": str, "prompt_version": str,
     "clips": [{"rank", "start_s", "end_s", "hook", "title", "caption",
                "hashtags", "score", "reason"}]}

The transcript is split into ~30 min windows with a small overlap; each window is one
Messages API call with a cached system prompt and a JSON-schema structured output. When a
source has more than `max_llm_windows` windows and engagement signals, only the first window
and the best-scoring others are sent (CLP-36); windows already cached are still used. The
candidates are then validated, snapped to word boundaries within [clip_min, clip_max],
deduped (>30% overlap keeps the higher score) and cut to the top K.

Every call reserves a worst-case estimate against the daily LLM cap before it is made
(`common.reserve_spend`; over the cap the job is deferred to the next UTC midnight) and is
settled to its actual token cost. Each window's validated candidates are cached in
`.select/` as soon as they are paid for, so a retried job does not pay for them again; the
cache is removed once highlights.json is written. Model ID, pricing and request shape come
from the claude-api skill (Claude Sonnet 5.5: $2 in / $10 out per MTok; cache writes
1.25x, cache reads $0.20).

The title and transcript are untrusted (anyone can upload a video): they are stripped of
control characters, the transcript is fenced in <transcript> tags, the system prompt says
to treat both as data, and every output field is length-limited and sanitized.

Prompt caching note: the system prompt is only cached if the cached prefix reaches the
model's minimum cacheable length; select_v1 alone may fall short. We log
`cache_read_input_tokens` per call; verify against live traffic before relying on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipper.config import MissingSecretError, Secrets, Settings
from clipper.jobs.models import Job
from clipper.jobs.stages import Deferred, PermanentError, SkipJob, StageContext, StageDef
from clipper.pipeline import common
from clipper.pipeline.common import read_json, strip_control, write_json_atomic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = "select_v1"
PROVIDER = "anthropic"
ARTIFACT = "highlights.json"

WINDOW_S = 30 * 60.0
OVERLAP_S = 60.0
MAX_TOKENS = 16_000
MAX_HINTS = 10
CANDIDATES_PER_WINDOW = 8
MAX_ATTEMPTS_PER_WINDOW = 2  # one retry on invalid output
MAX_OVERLAP = 0.30
MAX_WINDOWS = 40
"""40 windows x 29 min covers ~19 h, more than max_duration_seconds allows."""
MAX_SNAP_S = 3.0
"""Snapping to word boundaries may move a boundary at most this far; else the clip is dropped."""
MAX_TITLE_CHARS = 300
MAX_LANGUAGE_CHARS = 16
CREDIT_RETRY = timedelta(hours=1)
CREDIT_MAX_WAIT = timedelta(hours=48)
CREDIT_MARKER = ".credit_deferred_since"
CREDIT_MARKER_MAX_AGE = timedelta(days=7)
"""An older marker is from an earlier, unrelated outage and restarts the clock."""
CACHE_DIR = ".select"
FIELD_LIMITS = {"hook": 120, "title": 100, "caption": 2200, "reason": 500}
MAX_HASHTAGS = 10
_HASHTAG = re.compile(r"^#\w{1,50}$")
_TRANSCRIPT_TAG = re.compile(r"</?\s*transcript\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(self, usage: Any) -> float:
        def tokens(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        total = (
            tokens("input_tokens") * self.input
            + tokens("output_tokens") * self.output
            + tokens("cache_creation_input_tokens") * self.cache_write
            + tokens("cache_read_input_tokens") * self.cache_read
        )
        return total / 1_000_000


# Source: claude-api skill (models table, cached 2026-10-06; prompt-caching economics:
# 5-minute cache writes cost 1.25x base input).
PRICING: Mapping[str, Pricing] = {
    "claude-sonnet-5-5": Pricing(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
}


class InvalidModelOutput(Exception):
    """Claude's reply did not match the schema twice in a row (retryable)."""


# --- input artifacts -------------------------------------------------------------------


class _Word(BaseModel):
    word: str
    start: float
    end: float


class _Segment(BaseModel):
    start: float
    end: float
    text: str


class _Transcript(BaseModel):
    language: str | None = None
    duration_s: float | None = None
    segments: list[_Segment] = []
    words: list[_Word] = []


MAX_SIGNAL_POINTS = 10_000


class _HeatPoint(BaseModel):
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(ge=0, allow_inf_nan=False)
    value: float = Field(allow_inf_nan=False)

    @field_validator("value", mode="after")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return min(max(value, 0.0), 1.0)


class _LoudPeak(BaseModel):
    t_s: float = Field(ge=0, allow_inf_nan=False)
    lufs: float = Field(allow_inf_nan=False)


class _Signals(BaseModel):
    heatmap: list[_HeatPoint] = Field([], max_length=MAX_SIGNAL_POINTS)
    loudness_peaks: list[_LoudPeak] = Field([], max_length=MAX_SIGNAL_POINTS)


# --- model output ------------------------------------------------------------------------


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    hook: str
    title: str
    caption: str
    hashtags: list[str]
    score: int = Field(ge=0, le=100)
    reason: str

    @field_validator("hook", "title", "caption", "reason", mode="after")
    @classmethod
    def _clean_text(cls, value: str, info: Any) -> str:
        """Control characters stripped, then cut to the field's limit (not rejected: the
        call was already paid for)."""
        return strip_control(value)[: FIELD_LIMITS[info.field_name]]

    @field_validator("hashtags", mode="after")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        for raw in value:
            tag = strip_control(raw).replace(" ", "")
            tag = tag if tag.startswith("#") else f"#{tag}"
            if _HASHTAG.fullmatch(tag) and tag not in tags:
                tags.append(tag)
        return tags[:MAX_HASHTAGS]

    @model_validator(mode="after")
    def _ordered(self) -> Candidate:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be after start_s")
        return self


class _Reply(BaseModel):
    clips: list[Candidate]


_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start_s": {"type": "number"},
        "end_s": {"type": "number"},
        "hook": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["start_s", "end_s", "hook", "title", "caption", "hashtags", "score", "reason"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": _CANDIDATE_SCHEMA}},
    "required": ["clips"],
    "additionalProperties": False,
}


# --- prompt building ----------------------------------------------------------------------


def load_system_prompt(version: str = PROMPT_VERSION) -> str:
    return (resources.files("clipper.pipeline") / "prompts" / f"{version}.md").read_text(
        encoding="utf-8"
    )


def fmt_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"[{total // 60:02d}:{total % 60:02d}]"


@dataclass(frozen=True)
class Window:
    start_s: float
    end_s: float
    segments: tuple[_Segment, ...]


def make_windows(
    segments: Sequence[_Segment],
    window_s: float = WINDOW_S,
    overlap_s: float = OVERLAP_S,
    *,
    duration_s: float | None = None,
    max_windows: int = MAX_WINDOWS,
) -> list[Window]:
    """Split segments into windows of `window_s` that overlap by `overlap_s`. Segments past
    `duration_s` (the video's real length) are ignored; at most `max_windows` windows."""
    if duration_s is not None and duration_s > 0:
        segments = [s for s in segments if s.start < duration_s]
    if not segments:
        return []
    end_all = max(s.end for s in segments)
    if duration_s is not None and duration_s > 0:
        end_all = min(end_all, duration_s)
    step = window_s - overlap_s
    windows: list[Window] = []
    start = 0.0
    while True:
        end = start + window_s
        inside = tuple(s for s in segments if s.end > start and s.start < end)
        if inside:
            windows.append(Window(start, min(end, end_all), inside))
        if end >= end_all:
            break
        if len(windows) >= max_windows:
            logger.warning(
                "transcript needs more than %d windows; the rest is ignored", max_windows
            )
            break
        start += step
    return windows


def _normalised(values: list[float]) -> list[float]:
    top = max(values, default=0.0)
    return [v / top if top > 0 else 0.0 for v in values]


def rank_windows(windows: Sequence[Window], signals: _Signals) -> list[float]:
    """Engagement score per window, 0-2: the mean of its top-3 heatmap values divided by
    the source's highest heatmap value, plus its loudness-peak density divided by the
    source's highest density. Density is per full window length, so a short tail window
    (mostly overlap) is not inflated."""
    top_heat = max((h.value for h in signals.heatmap), default=0.0)
    heat: list[float] = []
    loud: list[float] = []
    for w in windows:
        values = sorted(
            (h.value for h in signals.heatmap if h.end_s > w.start_s and h.start_s < w.end_s),
            reverse=True,
        )[:3]
        heat.append(sum(values) / len(values) / top_heat if values and top_heat > 0 else 0.0)
        peaks = sum(1 for p in signals.loudness_peaks if w.start_s <= p.t_s < w.end_s)
        loud.append(peaks / max(w.end_s - w.start_s, WINDOW_S))
    return [h + lo for h, lo in zip(heat, _normalised(loud), strict=True)]


def choose_windows(windows: Sequence[Window], signals: _Signals, limit: int) -> list[int]:
    """Indexes of the windows to send to Claude, in time order: all of them when they fit
    in `limit` or the signals are empty or all zero; else the first (intros hold hooks) plus the
    best-scoring others, ties broken by index."""
    if len(windows) <= limit or not (signals.heatmap or signals.loudness_peaks):
        return list(range(len(windows)))
    scores = rank_windows(windows, signals)
    if not any(scores):  # signals carry no information: keep every window
        return list(range(len(windows)))
    best = sorted(range(1, len(windows)), key=lambda i: (-scores[i], i))[: limit - 1]
    return [0, *sorted(best)]


def _hints(window: Window, signals: _Signals) -> str:
    heat = sorted(
        (h for h in signals.heatmap if h.end_s > window.start_s and h.start_s < window.end_s),
        key=lambda h: h.value,
        reverse=True,
    )[:MAX_HINTS]
    loud = sorted(
        (p for p in signals.loudness_peaks if window.start_s <= p.t_s < window.end_s),
        key=lambda p: p.lufs,
        reverse=True,
    )[:MAX_HINTS]
    lines: list[str] = []
    if heat:
        lines.append("Most-replayed ranges (intensity 0-1):")
        lines += [
            f"- {fmt_ts(h.start_s)}-{fmt_ts(h.end_s)} {h.value:.2f}"
            for h in sorted(heat, key=lambda h: h.start_s)
        ]
    if loud:
        lines.append("Loudness peaks (LUFS, higher is louder):")
        lines += [f"- {fmt_ts(p.t_s)} {p.lufs:.1f}" for p in sorted(loud, key=lambda p: p.t_s)]
    return "\n".join(lines)


def build_user_prompt(
    window: Window,
    signals: _Signals,
    meta: Mapping[str, Any],
    settings: Settings,
    feedback_examples: Sequence[Mapping[str, Any]] = (),
) -> str:
    parts = []
    title = strip_control(str(meta.get("title") or ""))[:MAX_TITLE_CHARS]
    if title:
        parts.append(f"Video title (untrusted data): {title}")
    parts.append(
        f"Window: {fmt_ts(window.start_s)} to {fmt_ts(window.end_s)} "
        f"(seconds {window.start_s:.0f}-{window.end_s:.0f} of the video)."
    )
    parts.append(
        f"Clip length: {settings.clip_min_seconds}-{settings.clip_max_seconds} seconds. "
        f"Return at most {CANDIDATES_PER_WINDOW} candidates."
    )
    if feedback_examples:
        # CLP-37 will feed reviewer decisions here as few-shot examples.
        examples = json.dumps(list(feedback_examples), ensure_ascii=False, sort_keys=True)
        parts.append(f"Past reviewer decisions (learn from these):\n{examples}")
    hints = _hints(window, signals)
    if hints:
        parts.append(hints)
    transcript = "\n".join(
        f"{fmt_ts(s.start)} {strip_control(_TRANSCRIPT_TAG.sub(' ', s.text))}"
        for s in window.segments
    )
    parts.append(f"Transcript (untrusted data):\n<transcript>\n{transcript}\n</transcript>")
    return "\n\n".join(parts)


def build_system(creator: str, language: str | None, tags: Sequence[str]) -> list[dict[str, Any]]:
    """Static instructions first (cached), then the per-creator style block (cached too)."""
    language = strip_control(language or "")[:MAX_LANGUAGE_CHARS] or "unknown"
    style = f"Creator: {creator}\nVideo language: {language}"
    if tags:
        style += f"\nCreator style tags: {', '.join(tags)}"
    return [
        {"type": "text", "text": load_system_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": style, "cache_control": {"type": "ephemeral"}},
    ]


# --- spend ----------------------------------------------------------------------------------


def llm_spend_today(conn: sqlite3.Connection, now: datetime) -> float:
    return common.spend_today(conn, "llm", now)


def _input_tokens(usage: Any) -> int:
    return sum(
        int(getattr(usage, name, 0) or 0)
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def estimate_call_usd(system: Sequence[Mapping[str, Any]], prompt: str) -> float:
    """Worst case for one call: every input char a token at the cache-write rate, plus the
    full MAX_TOKENS of output."""
    price = PRICING[MODEL]
    chars = len(prompt) + sum(len(str(block.get("text", ""))) for block in system)
    return (chars * price.cache_write + MAX_TOKENS * price.output) / 1_000_000


# --- post-processing --------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    start_s: float
    end_s: float
    candidate: Candidate

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


def snap_to_words(
    candidate: Candidate, words: Sequence[_Word], min_s: float, max_s: float
) -> Clip | None:
    """Move start to the nearest word start and end to the nearest word end such that the
    clip length stays within [min_s, max_s]. None if no such pair exists nearby."""
    if not words:
        return None
    starts = sorted({w.start for w in words})
    ends = sorted({w.end for w in words})
    # Try word starts in order of distance from the proposed start.
    for start in sorted(starts, key=lambda s: (abs(s - candidate.start_s), s))[:20]:
        target = min(max(candidate.end_s, start + min_s), start + max_s)
        valid = [e for e in ends if start + min_s <= e <= start + max_s]
        if valid:
            end = min(valid, key=lambda e: (abs(e - target), e))
            # Snapping only nudges boundaries (length clamping to [min_s, max_s] is allowed
            # to move the end further); a big jump means the words are not there.
            if abs(start - candidate.start_s) > MAX_SNAP_S or abs(end - target) > MAX_SNAP_S:
                logger.info(
                    "dropping candidate %.1f-%.1f: nearest words at %.1f-%.1f",
                    candidate.start_s,
                    candidate.end_s,
                    start,
                    end,
                )
                return None
            return Clip(start, end, candidate)
    return None


def overlap_ratio(a: Clip, b: Clip) -> float:
    inter = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
    if inter <= 0:
        return 0.0
    return inter / min(a.duration, b.duration)


def dedupe(clips: Sequence[Clip], max_overlap: float = MAX_OVERLAP) -> list[Clip]:
    """Highest score first; drop any clip overlapping a kept one by more than max_overlap."""
    kept: list[Clip] = []
    for clip in sorted(clips, key=lambda c: (-c.candidate.score, c.start_s)):
        if all(overlap_ratio(clip, other) <= max_overlap for other in kept):
            kept.append(clip)
    return kept


def finalize(
    candidates: Sequence[Candidate], words: Sequence[_Word], settings: Settings, top_k: int
) -> list[dict[str, Any]]:
    snapped = [
        clip
        for c in candidates
        if (clip := snap_to_words(c, words, settings.clip_min_seconds, settings.clip_max_seconds))
    ]
    kept = dedupe(snapped)[:top_k]
    return [
        {
            "rank": rank,
            "start_s": round(clip.start_s, 3),
            "end_s": round(clip.end_s, 3),
            "hook": clip.candidate.hook,
            "title": clip.candidate.title,
            "caption": clip.candidate.caption,
            "hashtags": list(clip.candidate.hashtags),
            "score": clip.candidate.score,
            "reason": clip.candidate.reason,
        }
        for rank, clip in enumerate(kept, start=1)
    ]


# --- the stage ----------------------------------------------------------------------------------


def _settings_for(job: Job, ctx: StageContext) -> tuple[Settings, str | None, tuple[str, ...]]:
    creator = common.creator_for(job, ctx.config)
    settings = common.settings_for(job, ctx.config)
    return (settings, creator.language, creator.tags) if creator else (settings, None, ())


def _make_client() -> anthropic.Anthropic:
    try:
        key = Secrets().require("ANTHROPIC_API_KEY")
    except MissingSecretError as exc:
        raise PermanentError(str(exc)) from None
    return anthropic.Anthropic(api_key=key)


class _Refused(Exception):
    """The model declined this window; the call is paid, retrying it would be too."""


def _parse_reply(message: Any) -> list[Candidate]:
    if message.stop_reason == "refusal":
        raise _Refused
    if message.stop_reason not in ("end_turn", "stop_sequence"):
        raise ValueError(f"stop_reason={message.stop_reason}")  # e.g. max_tokens: retry
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise ValueError("no text block")
    return _Reply.model_validate_json(text).clips


class _CreditDeferred(Deferred):
    """Deferred because the Anthropic account is out of credit (keeps the marker)."""


def _no_credit(ctx: StageContext) -> Exception:
    """Deferred an hour at a time, for at most CREDIT_MAX_WAIT since the first deferral."""
    now = ctx.clock()
    since = common.first_seen(ctx.media_dir / CREDIT_MARKER, now, max_age=CREDIT_MARKER_MAX_AGE)
    if now - since > CREDIT_MAX_WAIT:
        hours = CREDIT_MAX_WAIT.total_seconds() / 3600
        return PermanentError(f"Anthropic credit balance too low for {hours:.0f} h; giving up")
    logger.warning(
        "Anthropic credit balance too low (since %s); deferring this job by %s",
        since.isoformat(),
        CREDIT_RETRY,
    )
    return _CreditDeferred(now + CREDIT_RETRY, "Anthropic credit balance too low")


def _call(
    client: anthropic.Anthropic, system: list[dict[str, Any]], prompt: str, ctx: StageContext
) -> Any:
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise PermanentError(f"Anthropic rejected the credentials ({exc.status_code})") from None
    except anthropic.BadRequestError as exc:
        if "credit balance" in str(exc).lower():
            raise _no_credit(ctx) from None
        raise PermanentError(f"Anthropic rejected the request ({exc.status_code})") from None
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Anthropic API error ({exc.status_code})") from None
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Anthropic API unreachable ({type(exc).__name__})") from None
    (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)
    return message


def _paid_call(
    client: anthropic.Anthropic,
    system: list[dict[str, Any]],
    prompt: str,
    job: Job,
    ctx: StageContext,
    cap: float,
) -> Any:
    """One API call with its estimate reserved first and its actual cost settled after."""
    with common.reserve_spend(
        ctx.conn,
        kind="llm",
        label="LLM",
        provider=PROVIDER,
        job_id=job.id,
        estimate_usd=estimate_call_usd(system, prompt),
        cap_usd=cap,
        now=ctx.clock(),
    ) as charge:
        message = _call(client, system, prompt, ctx)
        usage = message.usage
        charge.usd = PRICING[MODEL].cost(usage)
        charge.input_tokens = _input_tokens(usage)
        charge.output_tokens = int(usage.output_tokens or 0)
    logger.info(
        "job %s: LLM call $%.4f, cache_read_input_tokens=%s",
        job.id,
        charge.usd,
        getattr(usage, "cache_read_input_tokens", None),
    )
    return message


def _select_window(
    client: anthropic.Anthropic,
    system: list[dict[str, Any]],
    prompt: str,
    job: Job,
    ctx: StageContext,
    cap: float,
) -> list[Candidate]:
    problem = ""
    for _ in range(MAX_ATTEMPTS_PER_WINDOW):
        message = _paid_call(client, system, prompt, job, ctx, cap)
        try:
            return _parse_reply(message)
        except _Refused:
            logger.warning("job %s: model refused a window; treating it as empty", job.id)
            return []
        except (ValidationError, ValueError) as exc:
            problem = type(exc).__name__ if isinstance(exc, ValidationError) else str(exc)
    raise InvalidModelOutput(
        f"model output invalid after {MAX_ATTEMPTS_PER_WINDOW} tries: {problem}"
    )


def _check_budget(ctx: StageContext, cap: float) -> None:
    now = ctx.clock()
    spent = llm_spend_today(ctx.conn, now)
    if spent >= cap:
        raise Deferred(
            common.next_utc_midnight(now),
            f"daily LLM budget reached (${spent:.2f} of ${cap:.2f})",
        )


def _window_cache(
    cache_dir: Path, index: int, system: list[dict[str, Any]], prompt: str, settings: Settings
) -> Path:
    """Keyed by everything that shapes the request: prompt version, model, the exact system
    blocks and user prompt, and the settings that reach the prompt (clip length range).
    Unrelated settings (budgets, top-K) must not throw away paid windows."""
    prompt_settings = {
        "clip_min_seconds": settings.clip_min_seconds,
        "clip_max_seconds": settings.clip_max_seconds,
    }
    key = json.dumps([PROMPT_VERSION, MODEL, system, prompt, prompt_settings], sort_keys=True)
    return cache_dir / f"{index:03d}-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def _load_window(path: Path) -> list[Candidate] | None:
    if not path.is_file():
        return None
    try:
        return [Candidate.model_validate(c) for c in read_json(path)["clips"]]
    except (PermanentError, ValidationError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None


def select_highlights(
    job: Job,
    ctx: StageContext,
    client: anthropic.Anthropic | None = None,
    feedback_examples: Sequence[Mapping[str, Any]] = (),
) -> None:
    """The stage. Any outcome other than a credit deferral (success incl. all windows from
    cache, any error, giving up) ends the credit episode and removes its marker."""
    try:
        _select(job, ctx, client, feedback_examples)
    except _CreditDeferred:
        raise
    except BaseException:
        (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)
        raise
    (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)


def _select(
    job: Job,
    ctx: StageContext,
    client: anthropic.Anthropic | None,
    feedback_examples: Sequence[Mapping[str, Any]],
) -> None:
    raw_transcript = read_json(ctx.media_dir / "transcript.json", required=True)
    try:
        transcript = _Transcript.model_validate(raw_transcript)
    except ValidationError as exc:
        raise PermanentError(f"invalid input artifact ({exc.error_count()} errors)") from None
    try:
        signals = _Signals.model_validate(
            read_json(ctx.media_dir / "signals.json", required=False) or {}
        )
    except ValidationError as exc:
        # Signals are optional hints: drop them rather than fail the job.
        logger.warning(
            "job %s: signals.json is invalid (%d errors); ignoring it", job.id, exc.error_count()
        )
        signals = _Signals()
    meta = read_json(ctx.media_dir / "meta.json", required=False) or {}
    if not isinstance(meta, dict):
        meta = {}

    if not transcript.words or not transcript.segments:
        raise SkipJob("transcript has no speech to clip")

    settings, language, tags = _settings_for(job, ctx)
    top_k = settings.max_clips_per_stream if job.is_stream else settings.max_clips_per_source
    cap = settings.daily_llm_usd_cap
    detected = transcript.language if transcript.language != "unknown" else None
    system = build_system(job.creator, detected or language, tags)
    duration = meta.get("duration_s") or transcript.duration_s
    windows = make_windows(transcript.segments, duration_s=float(duration) if duration else None)
    chosen = set(choose_windows(windows, signals, settings.max_llm_windows))
    skipped: list[str] = []
    cache_dir = ctx.media_dir / CACHE_DIR
    cache_dir.mkdir(exist_ok=True)
    candidates: list[Candidate] = []
    for index, window in enumerate(windows):
        prompt = build_user_prompt(window, signals, meta, settings, feedback_examples)
        cache = _window_cache(cache_dir, index, system, prompt, settings)
        found = _load_window(cache)
        if found is None:  # a window paid for earlier is used even if not chosen: it is free
            if index not in chosen:
                skipped.append(f"#{index} {fmt_ts(window.start_s)}-{fmt_ts(window.end_s)}")
                continue
            _check_budget(ctx, cap)
            if client is None:
                client = _make_client()
            found = _select_window(client, system, prompt, job, ctx, cap)
            write_json_atomic(cache, {"clips": [c.model_dump() for c in found]})
        candidates += found
    if skipped:
        logger.info(
            "job %s: %d windows exceed max_llm_windows=%d; skipped low-engagement windows %s",
            job.id,
            len(windows),
            settings.max_llm_windows,
            ", ".join(skipped),
        )

    words = sorted(transcript.words, key=lambda w: w.start)
    clips = finalize(candidates, words, settings, top_k)
    write_json_atomic(
        ctx.media_dir / ARTIFACT,
        {"model": MODEL, "prompt_version": PROMPT_VERSION, "clips": clips},
    )
    shutil.rmtree(cache_dir, ignore_errors=True)


STAGE = StageDef(select_highlights, ARTIFACT)
