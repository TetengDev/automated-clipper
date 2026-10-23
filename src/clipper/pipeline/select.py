"""SELECTING stage: ask Claude for clip-worthy moments, then make them safe to render (CLP-15).

Reads `transcript.json` (required), `meta.json` and `signals.json` (optional hints) from
the job's media dir and writes `highlights.json`:

    {"model": str, "prompt_version": str, "playbook_version": str,
     "clips": [{"rank", "start_s", "end_s", "hook", "title", "caption",
                "hashtags", "score", "reason", "payoff_s", "open_question", "sensitive"}]}

The system prompt is `select_v2` plus the universal editing playbook (static, shared by every
creator), then the creator block with its content type's playbook section (CLP-51).

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

With `selector: claude_cli` (CLP-81) the same request goes through the Claude Code CLI on a
Claude subscription instead (`claude -p`, no API key, nothing billed per token): no tools,
no user settings, plugins or MCP servers, the prompt on stdin, and only the subscription
token in its environment. A usage limit defers the job until the limit resets.

Prompt caching note: the system prompt is only cached if the cached prefix reaches the
model's minimum cacheable length; select_v2 plus the playbook may still fall short. We log
`cache_read_input_tokens` per call; verify against live traffic before relying on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from clipper.config import ContentType, Creator, MissingSecretError, Secrets, Settings
from clipper.jobs.models import Job
from clipper.jobs.stages import Deferred, PermanentError, SkipJob, StageContext, StageDef
from clipper.pipeline import common
from clipper.pipeline.common import read_json, strip_control, write_json_atomic
from clipper.pipeline.ytdlp import CommandTimeout, RunResult
from clipper.redact import redact

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = "select_v2"
PLAYBOOK_VERSION = "playbook_v4"
"""Bump when select_v2.md or any file in prompts/playbook/ changes: it is part of the window
cache key (a test pins the text hash per version)."""
PROVIDER = "anthropic"
CLI_PROVIDER = "claude_cli"
CLI_COMMAND = "claude"
CLI_TIMEOUT_S = 600.0
_LOCAL_LOGIN_ENV = ("CLAUDE_CONFIG_DIR", "USER", "LOGNAME")
_CLI_QUIET_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
CLI_MAX_TURNS = 3
_CLI_GAVE_UP = ("error_max_turns", "error_max_structured_output_retries")
"""Structured output takes 2 turns (the schema is answered through a tool call)."""
USAGE_LIMIT_MAX_WAIT = timedelta(days=8)
"""A reset time further out than this (the weekly limit plus slack) is not trusted."""
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
PAYOFF_OUTSIDE = "payoff_outside"
FIELD_LIMITS = {"hook": 120, "title": 100, "caption": 2200, "reason": 500, "open_question": 200}
MAX_HASHTAGS = 10
_HASHTAG = re.compile(r"^#\w{1,50}$")
_RESET_EPOCH = re.compile(r"\|(\d{9,11})\b")
_USAGE_LIMIT = re.compile(r"usage limit|hit your limit|limit reached|rate.?limit", re.IGNORECASE)
_LOGIN = re.compile(r"/login|not logged in|invalid api key|authenticat|oauth token", re.IGNORECASE)
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
    # v2 fields; defaults keep v1 replies and cached v1 windows loadable.
    # No ge=0: a negative payoff fails the range check below and drops only its candidate.
    payoff_s: float | None = Field(None, allow_inf_nan=False)
    open_question: str = ""
    sensitive: bool = False

    @field_validator("hook", "title", "caption", "reason", "open_question", mode="after")
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
        if self.payoff_s is not None and not self.start_s <= self.payoff_s <= self.end_s:
            raise PydanticCustomError(PAYOFF_OUTSIDE, "payoff_s must fall inside [start_s, end_s]")
        return self


class _Reply(BaseModel):
    clips: list[dict[str, Any]]


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
        "payoff_s": {"type": "number"},
        "open_question": {"type": "string"},
        "sensitive": {"type": "boolean"},
    },
    "required": [
        "start_s",
        "end_s",
        "hook",
        "title",
        "caption",
        "hashtags",
        "score",
        "reason",
        "payoff_s",
        "open_question",
        "sensitive",
    ],
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


def load_playbook(section: ContentType | Literal["universal"]) -> str:
    """The universal rules or one content type's section; any other value is a ValueError."""
    name = "universal" if section == "universal" else ContentType(section).value
    path = resources.files("clipper.pipeline") / "prompts" / "playbook" / f"{name}.md"
    return path.read_text(encoding="utf-8")


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


def build_system(
    creator: str,
    language: str | None,
    tags: Sequence[str],
    content_type: ContentType | None = None,
) -> list[dict[str, Any]]:
    """Static instructions and the universal playbook first (cached, shared by every
    creator), then the per-creator block with its content-type section (cached too)."""
    language = strip_control(language or "")[:MAX_LANGUAGE_CHARS] or "unknown"
    style = f"Creator: {creator}\nVideo language: {language}"
    if tags:
        style += f"\nCreator style tags: {', '.join(tags)}"
    if content_type:
        style += f"\nContent type: {content_type.value}\n\n{load_playbook(content_type)}"
    static = f"{load_system_prompt()}\n\n{load_playbook('universal')}"
    return [
        {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
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


def _payoff_kept(clip: Clip, max_s: float) -> bool:
    """Snapping may nudge the payoff just outside the clip (clamped later); a payoff the
    length clamp cut off is a clip without its payoff, so a clamped clip gets no slack."""
    payoff = clip.candidate.payoff_s
    clamped = clip.candidate.end_s - clip.candidate.start_s > max_s
    slack = 0.0 if clamped else MAX_SNAP_S
    if payoff is None or clip.start_s - slack <= payoff <= clip.end_s + slack:
        return True
    logger.info(
        "dropping candidate %.1f-%.1f: payoff at %.1f falls outside the final clip %.1f-%.1f",
        clip.candidate.start_s,
        clip.candidate.end_s,
        payoff,
        clip.start_s,
        clip.end_s,
    )
    return False


def finalize(
    candidates: Sequence[Candidate], words: Sequence[_Word], settings: Settings, top_k: int
) -> list[dict[str, Any]]:
    snapped = [
        clip
        for c in candidates
        if (clip := snap_to_words(c, words, settings.clip_min_seconds, settings.clip_max_seconds))
        and _payoff_kept(clip, settings.clip_max_seconds)
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
            # Snapping may nudge the boundaries (_payoff_kept); keep the payoff inside.
            "payoff_s": None
            if (payoff := clip.candidate.payoff_s) is None
            else round(min(max(payoff, clip.start_s), clip.end_s), 3),
            "open_question": clip.candidate.open_question,
            "sensitive": clip.candidate.sensitive,
        }
        for rank, clip in enumerate(kept, start=1)
    ]


# --- the stage ----------------------------------------------------------------------------------


def _settings_for(job: Job, ctx: StageContext) -> tuple[Settings, Creator | None]:
    return common.settings_for(job, ctx.config), common.creator_for(job, ctx.config)


def _make_client() -> anthropic.Anthropic:
    try:
        key = Secrets().require("ANTHROPIC_API_KEY")
    except MissingSecretError as exc:
        raise PermanentError(str(exc)) from None
    return anthropic.Anthropic(api_key=key)


class _Refused(Exception):
    """The model declined this window; the call is paid, retrying it would be too."""


@dataclass(frozen=True)
class Reply:
    """One model answer, the same shape from either backend."""

    text: str | None
    stop_reason: str | None
    usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None


def _parse_reply(reply: Reply) -> list[Candidate]:
    if reply.stop_reason == "refusal":
        raise _Refused
    if reply.stop_reason not in ("end_turn", "stop_sequence"):
        raise ValueError(f"stop_reason={reply.stop_reason}")  # e.g. max_tokens: retry
    text = reply.text
    if text is None:
        raise ValueError("no text block")
    candidates: list[Candidate] = []
    for raw in _Reply.model_validate_json(text).clips:
        try:
            candidates.append(Candidate.model_validate(raw))
        except ValidationError as exc:
            # A payoff outside its own clip is a judgment miss on one candidate, not a
            # malformed reply: drop it and keep the paid window. Anything else retries.
            if not all(e["type"] == PAYOFF_OUTSIDE for e in exc.errors()):
                raise
            logger.info(
                "dropping candidate %r-%r: payoff_s %r is outside the clip",
                raw.get("start_s"),
                raw.get("end_s"),
                raw.get("payoff_s"),
            )
    return candidates


class _CreditDeferred(Deferred):
    """Deferred because the Anthropic account is out of credit (keeps the marker)."""


def _no_credit(ctx: StageContext, reason: str = "Anthropic credit balance too low") -> Exception:
    """Deferred an hour at a time, for at most CREDIT_MAX_WAIT since the first deferral."""
    now = ctx.clock()
    since = common.first_seen(ctx.media_dir / CREDIT_MARKER, now, max_age=CREDIT_MARKER_MAX_AGE)
    if now - since > CREDIT_MAX_WAIT:
        hours = CREDIT_MAX_WAIT.total_seconds() / 3600
        return PermanentError(f"{reason} for {hours:.0f} h; giving up")
    logger.warning(
        "%s (since %s); deferring this job by %s", reason, since.isoformat(), CREDIT_RETRY
    )
    return _CreditDeferred(now + CREDIT_RETRY, reason)


def _usage_limited(ctx: StageContext, text: str) -> Exception:
    """Subscription usage limit: wait for the reset time the CLI reports, else hourly."""
    reason = "Claude usage limit reached"
    now = ctx.clock()
    if match := _RESET_EPOCH.search(text):
        reset = datetime.fromtimestamp(int(match[1]), UTC)
        if now < reset <= now + USAGE_LIMIT_MAX_WAIT:
            logger.warning("%s; deferring this job until %s", reason, reset.isoformat())
            return _CreditDeferred(reset, reason)
    return _no_credit(ctx, reason)


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
    return message


class _ApiBackend:
    """The paid Messages API; the client is built on the first call (cached windows need
    no key)."""

    provider = PROVIDER
    billed = True

    def __init__(self, client: anthropic.Anthropic | None) -> None:
        self.client = client

    def estimate(self, system: list[dict[str, Any]], prompt: str) -> float:
        return estimate_call_usd(system, prompt)

    def complete(self, system: list[dict[str, Any]], prompt: str, ctx: StageContext) -> Reply:
        if self.client is None:
            self.client = _make_client()
        message = _call(self.client, system, prompt, ctx)
        usage = message.usage
        return Reply(
            text=next((b.text for b in message.content if b.type == "text"), None),
            stop_reason=message.stop_reason,
            usd=PRICING[MODEL].cost(usage),
            input_tokens=_input_tokens(usage),
            output_tokens=int(usage.output_tokens or 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
        )


class _CliBackend:
    """The Claude Code CLI on a Claude subscription (`claude -p`): nothing billed per token.

    The transcript is untrusted, so the model gets no tools, and the CLI loads no user
    settings, plugins, MCP servers or slash commands (they would also multiply the tokens
    each call uses). The prompt goes on stdin, the child runs in an empty scratch dir, and
    its env is the runner's allowlist plus the subscription token and two flags that turn
    off autoupdate and non-essential traffic. A token login gets a throwaway config dir (the
    image's root fs is read-only); without a token the CLI uses the machine's existing
    `claude` login (keeping CLAUDE_CONFIG_DIR, USER and LOGNAME)."""

    provider = CLI_PROVIDER
    billed = False

    def __init__(self, runner: Callable[..., RunResult] | None) -> None:
        self.runner = runner

    def estimate(self, system: list[dict[str, Any]], prompt: str) -> float:
        return 0.0

    def complete(self, system: list[dict[str, Any]], prompt: str, ctx: StageContext) -> Reply:
        token = Secrets().claude_code_oauth_token
        with tempfile.TemporaryDirectory(prefix="clipper-claude-") as tmp:
            root = Path(tmp)
            (root / "work").mkdir()
            (root / "system.md").write_text("\n\n".join(b["text"] for b in system), "utf-8")
            (root / "prompt.txt").write_text(prompt, "utf-8")
            # Without a token the machine's login is used: it may live under a custom
            # CLAUDE_CONFIG_DIR, and the macOS Keychain lookup needs USER (none are secrets).
            env = {k: os.environ[k] for k in _LOCAL_LOGIN_ENV if os.environ.get(k)}
            if token and token.get_secret_value():
                (root / "config").mkdir()
                env = {
                    "CLAUDE_CODE_OAUTH_TOKEN": token.get_secret_value(),
                    "CLAUDE_CONFIG_DIR": str(root / "config"),
                }
            env |= _CLI_QUIET_ENV
            args = [
                CLI_COMMAND, "-p", "--model", MODEL, "--max-turns", str(CLI_MAX_TURNS),
                "--output-format", "json", "--json-schema", json.dumps(OUTPUT_SCHEMA),
                "--system-prompt-file", str(root / "system.md"),
                "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--disable-slash-commands", "--no-session-persistence",
            ]  # fmt: skip
            run = self.runner or common.default_runner()
            try:
                result = run(
                    args, CLI_TIMEOUT_S, env=env, stdin_path=root / "prompt.txt", cwd=root / "work"
                )
            except FileNotFoundError:
                raise PermanentError(
                    "claude CLI not found; install @anthropic-ai/claude-code (docs/DEPLOYMENT.md)"
                ) from None
            except CommandTimeout as exc:
                raise RuntimeError(f"claude CLI: {exc}") from None
        return _cli_reply(result, ctx)


def _cli_reply(result: RunResult, ctx: StageContext) -> Reply:
    try:
        out = json.loads(result.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        detail = common.stderr_tail(result.stderr) or "no JSON output"
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {detail}")
    if out.get("subtype") in _CLI_GAVE_UP:  # no valid answer in time: invalid output, retried
        return Reply(None, "end_turn", 0.0, 0, 0)
    if out.get("is_error"):
        text = str(out.get("result") or "")
        status = out.get("api_error_status")
        if status == 429 or _USAGE_LIMIT.search(text):
            raise _usage_limited(ctx, text)
        if status in (401, 403) or _LOGIN.search(text):
            raise PermanentError(
                "Claude subscription login rejected; run `claude setup-token` and set"
                " CLAUDE_CODE_OAUTH_TOKEN"
            )
        detail = strip_control(redact(text))[:300]
        raise RuntimeError(f"claude CLI error ({out.get('subtype')}): {detail}")
    if result.returncode != 0:
        detail = common.stderr_tail(result.stderr) or "no error output"
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {detail}")
    usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    structured = out.get("structured_output")

    def tokens(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and value >= 0 else 0

    return Reply(
        text=None if structured is None else json.dumps(structured),
        stop_reason="refusal" if out.get("stop_reason") == "refusal" else "end_turn",
        usd=0.0,
        input_tokens=sum(
            tokens(n)
            for n in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ),
        output_tokens=tokens("output_tokens"),
        cache_read_tokens=tokens("cache_read_input_tokens"),
    )


Backend = _ApiBackend | _CliBackend


def _paid_call(
    backend: Backend,
    system: list[dict[str, Any]],
    prompt: str,
    job: Job,
    ctx: StageContext,
    cap: float,
) -> Reply:
    """One call with its estimate reserved first and its actual cost settled after."""
    with common.reserve_spend(
        ctx.conn,
        kind="llm",
        label="LLM",
        provider=backend.provider,
        job_id=job.id,
        estimate_usd=backend.estimate(system, prompt),
        cap_usd=cap,
        now=ctx.clock(),
    ) as charge:
        reply = backend.complete(system, prompt, ctx)
        charge.usd = reply.usd
        charge.input_tokens = reply.input_tokens
        charge.output_tokens = reply.output_tokens
    (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)
    logger.info(
        "job %s: LLM call via %s $%.4f, cache_read_input_tokens=%s",
        job.id,
        backend.provider,
        charge.usd,
        reply.cache_read_tokens,
    )
    return reply


def _select_window(
    backend: Backend,
    system: list[dict[str, Any]],
    prompt: str,
    job: Job,
    ctx: StageContext,
    cap: float,
) -> list[Candidate]:
    problem = ""
    for _ in range(MAX_ATTEMPTS_PER_WINDOW):
        reply = _paid_call(backend, system, prompt, job, ctx, cap)
        try:
            return _parse_reply(reply)
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
    key = json.dumps(
        [PROMPT_VERSION, PLAYBOOK_VERSION, MODEL, system, prompt, prompt_settings], sort_keys=True
    )
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
    runner: Callable[..., RunResult] | None = None,
) -> None:
    """The stage. Any outcome other than a credit deferral (success incl. all windows from
    cache, any error, giving up) ends the credit episode and removes its marker. `client`
    (API) and `runner` (CLI) are test seams."""
    try:
        _select(job, ctx, client, feedback_examples, runner)
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
    runner: Callable[..., RunResult] | None,
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

    settings, creator = _settings_for(job, ctx)
    top_k = settings.max_clips_per_stream if job.is_stream else settings.max_clips_per_source
    cap = settings.daily_llm_usd_cap
    backend: Backend = (
        _CliBackend(runner) if settings.selector == "claude_cli" else _ApiBackend(client)
    )
    detected = transcript.language if transcript.language != "unknown" else None
    system = build_system(
        job.creator,
        detected or (creator.language if creator else None),
        creator.tags if creator else (),
        creator.content_type if creator else None,
    )
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
            if backend.billed:
                _check_budget(ctx, cap)
            found = _select_window(backend, system, prompt, job, ctx, cap)
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
        {
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "playbook_version": PLAYBOOK_VERSION,
            "clips": clips,
        },
    )
    shutil.rmtree(cache_dir, ignore_errors=True)


STAGE = StageDef(select_highlights, ARTIFACT)
