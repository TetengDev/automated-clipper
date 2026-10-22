"""TRANSCRIBING stage (CLP-14): audio_16k.flac -> transcript.json with word timestamps.

The audio is cut into overlapping chunks (each well under Groq's 25 MB upload limit),
transcribed concurrently through a `Transcriber` (local faster-whisper by default, CLP-35;
Groq when `transcriber: groq`), then stitched back into one timeline:
timestamps are offset by the chunk start, the overlap is split at its midpoint so each
word is kept from exactly one chunk, and word starts are forced to be non-decreasing.

Cost is estimated before any upload (budget check against `daily_transcribe_usd_cap`) and
what was actually billed is written to the `spend` table, also when a later chunk fails.

transcript.json (shared contract with CLP-15/16):
    {"language": str, "duration_s": float, "model": str,
     "segments": [{"start": float, "end": float, "text": str}],
     "words": [{"word": str, "start": float, "end": float}]}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

from clipper.config import WHISPER_MODELS, MissingSecretError, Secrets, Settings
from clipper.jobs.models import Job
from clipper.jobs.stages import PermanentError, StageContext, StageDef
from clipper.pipeline import local_asr
from clipper.pipeline.common import (
    PROTOCOL_WHITELIST,
    REDACT_LIMIT_CHARS,
    Deadline,
    MediaError,
    NoDuration,
    creator_for,
    default_runner,
    ffprobe_duration,
    read_json,
    reserve_spend,
    run_media,
    settings_for,
    stderr_tail,
    write_json_atomic,
)
from clipper.pipeline.local_asr import keep_segment as keep_segment  # re-export: guards
from clipper.pipeline.local_asr import latin_word as latin_word  # run in the child
from clipper.pipeline.ytdlp import CommandTimeout, RunResult
from clipper.redact import redact

logger = logging.getLogger(__name__)

AUDIO_FILE = "audio_16k.flac"
ARTIFACT = "transcript.json"
CACHE_DIR = ".transcribe"
"""Per-chunk results that were already paid for; reused when a failed run is retried."""
WORK_PREFIX = ".transcribe-"

CUT_TIMEOUT_S = 10 * 60.0
CHUNK_CALL_TIMEOUT_S = 15 * 60.0
"""Upper bound for one chunk's Groq call incl. retries; shrunk to the stage's Deadline."""
MAX_UNUSABLE_PER_CHUNK = 2
"""A chunk whose paid response was unusable is re-requested once more, then the job fails."""
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")

CHUNK_SECONDS = 600.0
OVERLAP_SECONDS = 2.0
MAX_CONCURRENCY = 3

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# whisper-large-v3 (not -turbo): noticeably better on Tagalog/Taglish code-switching.
GROQ_MODEL = "whisper-large-v3"
# Groq pricing, https://console.groq.com/docs/speech-to-text (checked 2026-10-21):
# whisper-large-v3 $0.111/hour, whisper-large-v3-turbo $0.04/hour; each request is billed
# for at least 10 s. Upload limit is 25 MB (free tier); a 10 min 16 kHz mono FLAC is ~10 MB.
GROQ_USD_PER_HOUR = {"whisper-large-v3": 0.111, "whisper-large-v3-turbo": 0.04}
GROQ_MIN_BILLED_SECONDS = 10.0
GROQ_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 60.0

DEFAULT_MODELS_DIR = Path("data/models")
"""Local Whisper model cache when CLIPPER_MODELS_DIR is unset (next to data/state.db)."""
ASR_COMMAND = (sys.executable, "-I", "-m", "clipper.pipeline.local_asr")
"""The local ASR child: isolated Python (no PYTHON* env, no cwd on sys.path)."""
MODEL_FETCH_TIMEOUT_S = 30 * 60.0

# verbose_json reports the language by name; transcript.json uses ISO-639-1 codes.
_LANGUAGE_CODES = {
    "english": "en",
    "tagalog": "tl",
    "filipino": "tl",
    "spanish": "es",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "indonesian": "id",
    "malay": "ms",
    "french": "fr",
    "german": "de",
    "portuguese": "pt",
}


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Word:
    word: str
    start: float
    end: float


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class ChunkTranscript:
    """One chunk's result, timestamps relative to the chunk start."""

    language: str | None
    words: tuple[Word, ...]
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Chunk:
    index: int
    offset: float  # seconds into the full audio
    duration: float


@dataclass(frozen=True)
class Transcript:
    language: str
    duration_s: float
    model: str
    segments: tuple[Segment, ...]
    words: tuple[Word, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration_s": round(self.duration_s, 3),
            "model": self.model,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in self.segments],
            "words": [{"word": w.word, "start": w.start, "end": w.end} for w in self.words],
        }


# --------------------------------------------------------------------------- transcribers


class Transcriber(Protocol):
    """Speech-to-text backend: `GroqTranscriber` or `FasterWhisperTranscriber` (CLP-35)."""

    provider: str
    model: str

    def transcribe(
        self, audio: Path, *, language: str | None, timeout_s: float | None = None
    ) -> ChunkTranscript:
        """`timeout_s` bounds the whole call including retries. A response that was billed
        but cannot be used raises `BilledChunkError`."""
        ...

    def cost_usd(self, chunk_seconds: Sequence[float]) -> float:
        """What transcribing chunks of these lengths costs (0 for a local model)."""
        ...

    def billed_seconds(self, chunk_seconds: Sequence[float]) -> float: ...


class BilledChunkError(RuntimeError):
    """The provider answered (and billed) but the result is unusable. Retryable at the
    job level; the chunk still counts as paid."""


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def _error_detail(response: httpx.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.text
    return redact(str(message)[-REDACT_LIMIT_CHARS:])[:200]


@dataclass
class GroqTranscriber:
    """Groq hosted Whisper over plain httpx (no SDK), with tenacity retries."""

    api_key: str = field(repr=False)
    model: str = GROQ_MODEL
    url: str = GROQ_URL
    max_attempts: int = 5
    timeout: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(connect=10.0, read=300.0, write=120.0, pool=30.0)
    )
    sleep: Callable[[float], None] = time.sleep
    client: httpx.Client | None = None
    provider: str = "groq"

    def __post_init__(self) -> None:
        if self.model not in GROQ_USD_PER_HOUR:
            raise ValueError(f"unknown Groq model: {self.model}")

    def billed_seconds(self, chunk_seconds: Sequence[float]) -> float:
        return sum(max(s, GROQ_MIN_BILLED_SECONDS) for s in chunk_seconds)

    def cost_usd(self, chunk_seconds: Sequence[float]) -> float:
        return self.billed_seconds(chunk_seconds) / 3600.0 * GROQ_USD_PER_HOUR[self.model]

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            return exc.retry_after
        return wait_exponential_jitter(initial=1, max=30)(state)

    def _timeout(self, timeout_s: float | None) -> httpx.Timeout:
        if timeout_s is None:
            return self.timeout
        t = self.timeout
        return httpx.Timeout(
            connect=min(t.connect or timeout_s, timeout_s),
            read=min(t.read or timeout_s, timeout_s),
            write=min(t.write or timeout_s, timeout_s),
            pool=min(t.pool or timeout_s, timeout_s),
        )

    def transcribe(
        self, audio: Path, *, language: str | None, timeout_s: float | None = None
    ) -> ChunkTranscript:
        size = audio.stat().st_size
        if size > GROQ_MAX_UPLOAD_BYTES:
            raise PermanentError(f"audio chunk is {size} bytes, over Groq's upload limit")
        stop = stop_after_attempt(self.max_attempts)
        if timeout_s is not None:
            stop = stop | stop_after_delay(timeout_s)
        retrying = Retrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop,
            wait=self._wait,
            sleep=self.sleep,
            reraise=True,
        )
        timeout = self._timeout(timeout_s)
        client = self.client or httpx.Client(timeout=timeout)
        try:
            body = retrying(self._post, client, audio, language, timeout)
        finally:
            if self.client is None:
                client.close()
        try:
            return _parse_verbose_json(body)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BilledChunkError(f"Groq response unusable: {type(exc).__name__}") from None

    def _post(
        self,
        client: httpx.Client,
        audio: Path,
        language: str | None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
            "temperature": "0",
        }
        if language:
            data["language"] = language
        try:
            with audio.open("rb") as fh:
                response = client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"file": (audio.name, fh, "audio/flac")},
                    timeout=timeout or self.timeout,
                )
        except httpx.TransportError as exc:
            # Message only names the error type: some transports echo request details.
            raise _Retryable(f"Groq request failed: {type(exc).__name__}") from None
        status = response.status_code
        if status in (401, 403):
            raise PermanentError(f"Groq rejected the API key (HTTP {status}); check GROQ_API_KEY")
        if status == 429 or status >= 500:
            raise _Retryable(f"Groq HTTP {status}", _retry_after(response))
        if status >= 400:
            raise PermanentError(f"Groq HTTP {status}: {_error_detail(response)}")
        # A 2xx is billed: never re-send it within this call; the job retry reuses paid chunks.
        try:
            body = response.json()
        except ValueError:
            raise BilledChunkError("Groq returned a non-JSON response") from None
        if not isinstance(body, dict):
            raise BilledChunkError("Groq returned an unexpected response")
        return body


def _parse_verbose_json(body: dict[str, Any]) -> ChunkTranscript:
    words = tuple(
        Word(str(w["word"]).strip(), float(w["start"]), float(w["end"]))
        for w in body.get("words") or ()
        if str(w.get("word", "")).strip()
    )
    segments = tuple(
        Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip())
        for s in body.get("segments") or ()
        if str(s.get("text", "")).strip()
    )
    language = normalize_language(str(body["language"])) if body.get("language") else None
    # "unknown" is the transcript's fallback, never a detected language: keep looking.
    return ChunkTranscript(None if language == "unknown" else language, words, segments)


def normalize_language(value: str) -> str:
    """ISO-639 code (`en`, `tl`, `pt-br`) or "unknown"; never free text from the API."""
    key = value.strip().lower()
    code = _LANGUAGE_CODES.get(key, key)
    return code if _LANGUAGE_CODE.fullmatch(code) else "unknown"


# --------------------------------------------------------------------------- local whisper


def models_dir() -> Path:
    """Absolute model cache dir: CLIPPER_MODELS_DIR, else ./data/models."""
    return Path(os.environ.get("CLIPPER_MODELS_DIR") or DEFAULT_MODELS_DIR).resolve()


def _run_asr(args: Sequence[str], timeout_s: float, what: str) -> RunResult:
    """Run the ASR child like ffmpeg (`run_media`), but tell a model that cannot be loaded
    (permanent) apart from a crash, OOM kill or timeout (retryable)."""
    try:
        result = default_runner()(args, timeout_s)
    except CommandTimeout as exc:
        raise MediaError(f"{what}: {exc}") from None
    if result.returncode == local_asr.EXIT_MODEL:
        raise PermanentError(f"{what}: cannot load the model: {stderr_tail(result.stderr)}")
    if result.returncode != 0:
        raise MediaError(f"{what} failed (exit {result.returncode}): {stderr_tail(result.stderr)}")
    return result


@dataclass
class FasterWhisperTranscriber:
    """Local faster-whisper (CTranslate2) on CPU. Each chunk runs in its own child process
    (`local_asr`) that loads the pinned model snapshot from disk, offline; the stage runs
    chunks one at a time, so at most one model is in memory and it is freed per chunk."""

    model: str = "large-v3-turbo"
    compute_type: str = "int8"
    cpu_threads: int = 4
    models_dir: Path = field(default_factory=models_dir)
    provider: str = "local"

    def __post_init__(self) -> None:
        if self.model not in WHISPER_MODELS:
            raise ValueError(f"unknown whisper model: {self.model}")

    def billed_seconds(self, chunk_seconds: Sequence[float]) -> float:
        return float(sum(chunk_seconds))

    def cost_usd(self, chunk_seconds: Sequence[float]) -> float:
        return 0.0

    @property
    def snapshot_dir(self) -> Path:
        repo, sha = WHISPER_MODELS[self.model]
        return self.models_dir / f"models--{repo.replace('/', '--')}" / "snapshots" / sha

    def ensure_model(self, timeout_s: float = MODEL_FETCH_TIMEOUT_S) -> Path:
        """Download the pinned snapshot once (in a child, bounded by `timeout_s`)."""
        snapshot = self.snapshot_dir
        if not (snapshot / "model.bin").is_file():
            repo, sha = WHISPER_MODELS[self.model]
            self.models_dir.mkdir(parents=True, exist_ok=True)
            args = [*ASR_COMMAND, "fetch", repo, sha, str(self.models_dir)]
            _run_asr(args, timeout_s, f"downloading whisper model {self.model}")
            if not (snapshot / "model.bin").is_file():
                raise MediaError(f"whisper model {self.model}: download left no model.bin")
        return snapshot

    def transcribe(
        self, audio: Path, *, language: str | None, timeout_s: float | None = None
    ) -> ChunkTranscript:
        args = [
            *ASR_COMMAND,
            str(self.snapshot_dir),
            self.compute_type,
            str(self.cpu_threads),
            language or "-",
            str(audio),
        ]
        timeout = CHUNK_CALL_TIMEOUT_S if timeout_s is None else timeout_s
        result = _run_asr(args, timeout, f"local transcription of {audio.name}")
        try:
            body = json.loads(result.stdout)
            chunk = _parse_verbose_json(body)
            dropped = body.get("dropped") or {}
            segments, words = int(dropped.get("segments", 0)), int(dropped.get("words", 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BilledChunkError("local transcription output unusable") from None
        logger.info("%s: guards dropped %d segment(s), %d word(s)", audio.name, segments, words)
        return chunk


# --------------------------------------------------------------------------- chunking


def plan_chunks(duration: float, chunk_s: float, overlap_s: float) -> list[Chunk]:
    """Chunks of `chunk_s` starting every `chunk_s - overlap_s`, the last one cut short."""
    if duration <= 0:
        return []
    if not 0 <= overlap_s < chunk_s:
        raise ValueError("overlap must be shorter than the chunk")
    step = chunk_s - overlap_s
    chunks: list[Chunk] = []
    offset = 0.0
    while True:
        length = min(chunk_s, duration - offset)
        chunks.append(Chunk(len(chunks), round(offset, 3), round(length, 3)))
        if offset + chunk_s >= duration:
            return chunks
        offset += step


def probe_duration(audio: Path) -> float:
    """Timeouts and ffprobe failures stay retryable (`MediaError`); a file that probes
    fine but has no duration is permanent."""
    try:
        return ffprobe_duration(audio)
    except NoDuration:
        raise PermanentError(f"cannot read the duration of {audio.name}") from None


def cut_chunk(
    audio: Path, chunk: Chunk, out_dir: Path, *, timeout_s: float = CUT_TIMEOUT_S
) -> Path:
    out = out_dir / f"chunk_{chunk.index:04d}.flac"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{chunk.offset:.3f}", "-t", f"{chunk.duration:.3f}",
        *PROTOCOL_WHITELIST, "-i", str(audio),
        "-ac", "1", "-ar", "16000", "-c:a", "flac", str(out),
    ]  # fmt: skip
    run_media(args, timeout_s, f"ffmpeg cutting chunk {chunk.index}")
    if not out.is_file():
        raise MediaError(f"ffmpeg cutting chunk {chunk.index} produced no file")
    return out


# --------------------------------------------------------------------------- chunk cache


def audio_fingerprint(audio: Path) -> tuple[int, int]:
    """Size and mtime: a re-fetched audio file invalidates every cached chunk."""
    st = audio.stat()
    return st.st_size, st.st_mtime_ns


def _cache_path(
    cache_dir: Path,
    chunk: Chunk,
    backend: Transcriber,
    language: str | None,
    fingerprint: tuple[int, int],
) -> Path:
    parts: list[Any] = [
        chunk.index,
        chunk.offset,
        chunk.duration,
        backend.provider,
        backend.model,
        language,
        *fingerprint,
    ]
    if isinstance(backend, FasterWhisperTranscriber):  # Groq keys stay as they were
        parts += [backend.compute_type, local_asr.GUARD_VERSION]
    key = json.dumps(parts)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return cache_dir / f"{chunk.index:04d}-{digest}.json"


def _unusable_count(cache_file: Path) -> int:
    """Paid-but-unusable responses so far for the chunk cached at `cache_file` (same key:
    a re-fetched audio file or another model starts from zero)."""
    try:
        return int(cache_file.with_suffix(".unusable").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _note_unusable(cache_file: Path) -> int:
    """Record one more paid-but-unusable response; returns the new count."""
    count = _unusable_count(cache_file) + 1
    note = cache_file.with_suffix(".unusable")
    tmp = note.with_name(f".{note.name}.tmp")
    tmp.write_text(str(count), encoding="utf-8")
    os.replace(tmp, note)
    return count


def _save_chunk(path: Path, result: ChunkTranscript) -> None:
    write_json_atomic(
        path,
        {
            "language": result.language,
            "words": [[w.word, w.start, w.end] for w in result.words],
            "segments": [[s.start, s.end, s.text] for s in result.segments],
        },
        indent=None,
    )


def _load_chunk(path: Path) -> ChunkTranscript | None:
    if not path.is_file():
        return None
    try:
        data = read_json(path)
        return ChunkTranscript(
            data["language"],
            tuple(Word(str(w), float(a), float(b)) for w, a, b in data["words"]),
            tuple(Segment(float(a), float(b), str(t)) for a, b, t in data["segments"]),
        )
    except (PermanentError, KeyError, TypeError, ValueError):
        path.unlink(missing_ok=True)  # damaged cache entry: pay for this chunk again
        return None


# --------------------------------------------------------------------------- stitching


def _norm(word: str) -> str:
    return "".join(ch for ch in word.casefold() if ch.isalnum())


def stitch(
    results: Sequence[tuple[Chunk, ChunkTranscript]],
) -> tuple[tuple[Segment, ...], tuple[Word, ...]]:
    """Merge per-chunk transcripts into one timeline.

    Each overlap is split at its midpoint: the earlier chunk keeps what starts before it,
    the later chunk what starts at or after it. A word repeated right across the cut is
    dropped once more, and word starts are clamped to be non-decreasing.
    """
    ordered = sorted(results, key=lambda r: r[0].offset)
    cuts: list[float] = []
    for (prev, _), (nxt, _) in zip(ordered, ordered[1:], strict=False):
        prev_end = prev.offset + prev.duration
        cuts.append((nxt.offset + min(prev_end, nxt.offset + nxt.duration)) / 2)

    words: list[Word] = []
    segments: list[Segment] = []
    for i, (chunk, result) in enumerate(ordered):
        lo = cuts[i - 1] if i > 0 else float("-inf")
        hi = cuts[i] if i < len(cuts) else float("inf")
        boundary = len(words)
        for w in result.words:
            start, end = w.start + chunk.offset, w.end + chunk.offset
            if not lo <= start < hi:
                continue
            if (
                len(words) == boundary
                and words
                and _norm(words[-1].word) == _norm(w.word)
                and start - words[-1].start < 1.0
            ):
                continue  # same word heard on both sides of the cut
            if words and start < words[-1].start:
                start = words[-1].start
            words.append(Word(w.word, round(start, 3), round(max(end, start), 3)))
        for s in result.segments:
            start, end = s.start + chunk.offset, s.end + chunk.offset
            if lo <= start < hi:
                if segments and start < segments[-1].start:
                    start = segments[-1].start
                segments.append(Segment(round(start, 3), round(max(end, start), 3), s.text))
    return tuple(segments), tuple(words)


# --------------------------------------------------------------------------- stage


def creator_language(job: Job, ctx: StageContext) -> str | None:
    """The creator's configured language (tl handles Taglish), or None for auto-detect."""
    creator = creator_for(job, ctx.config)
    return creator.language if creator else None


def default_transcriber(settings: Settings) -> Transcriber:
    """The configured backend; GROQ_API_KEY is needed only for `transcriber: groq`."""
    if settings.transcriber == "local":
        return FasterWhisperTranscriber(
            model=settings.whisper_model,
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    try:
        key = Secrets().require("GROQ_API_KEY")
    except MissingSecretError as exc:
        raise PermanentError(str(exc)) from None
    return GroqTranscriber(api_key=key)


def transcribe(
    job: Job,
    ctx: StageContext,
    *,
    transcriber: Transcriber | None = None,
    chunk_s: float = CHUNK_SECONDS,
    overlap_s: float = OVERLAP_SECONDS,
    max_concurrency: int = MAX_CONCURRENCY,
) -> None:
    audio = ctx.media_dir / AUDIO_FILE
    if not audio.is_file():
        raise PermanentError(f"{AUDIO_FILE} is missing; the audio stage did not produce it")
    duration = probe_duration(audio)
    chunks = plan_chunks(duration, chunk_s, overlap_s)
    if not chunks:
        raise PermanentError(f"{AUDIO_FILE} has no audio")

    if transcriber is None:
        transcriber = default_transcriber(settings_for(job, ctx.config))
    backend = transcriber
    local = isinstance(backend, FasterWhisperTranscriber)
    if local:
        max_concurrency = 1  # one model in memory; it already uses whisper_cpu_threads
    language = creator_language(job, ctx)
    fingerprint = audio_fingerprint(audio)
    deadline = Deadline()
    for stale in ctx.media_dir.glob(f"{WORK_PREFIX}*"):  # chunk audio of a killed run
        shutil.rmtree(stale, ignore_errors=True)
    cache_dir = ctx.media_dir / CACHE_DIR
    cache_dir.mkdir(exist_ok=True)
    cached = {
        c.index: _load_chunk(_cache_path(cache_dir, c, backend, language, fingerprint))
        for c in chunks
    }
    todo = [c for c in chunks if cached[c.index] is None]
    results: dict[int, ChunkTranscript] = {i: r for i, r in cached.items() if r is not None}
    if todo:
        if local:  # outside the per-chunk timeouts, inside the stage budget
            backend.ensure_model(deadline.timeout(MODEL_FETCH_TIMEOUT_S))
        _transcribe_chunks(
            job,
            ctx,
            audio,
            todo,
            backend,
            language,
            cache_dir,
            max_concurrency,
            deadline,
            fingerprint,
        )
        results.update(
            (c.index, r)
            for c in todo
            if (r := _load_chunk(_cache_path(cache_dir, c, backend, language, fingerprint)))
        )
    ordered = [(c, results[c.index]) for c in chunks]

    segments, words = stitch(ordered)
    detected = next((r.language for _, r in ordered if r.language), None)
    transcript = Transcript(
        language=language or detected or "unknown",
        duration_s=duration,
        model=backend.model,
        segments=segments,
        words=words,
    )
    write_json_atomic(ctx.media_dir / ARTIFACT, transcript.to_json(), indent=None)
    shutil.rmtree(cache_dir, ignore_errors=True)
    logger.info(
        "job %s: transcribed %.0f s in %d chunk(s) (%d reused), %d words",
        job.id,
        duration,
        len(chunks),
        len(chunks) - len(todo),
        len(words),
    )


def _transcribe_chunks(
    job: Job,
    ctx: StageContext,
    audio: Path,
    chunks: Sequence[Chunk],
    backend: Transcriber,
    language: str | None,
    cache_dir: Path,
    max_concurrency: int,
    deadline: Deadline,
    fingerprint: tuple[int, int],
) -> None:
    """Transcribe `chunks`, caching each result as soon as it is paid for. The estimate is
    reserved against the daily cap first; the row is settled to what was billed, also
    when a later chunk fails. Every chunk's ffmpeg and API timeouts are shrunk to what is
    left of the stage `deadline`; running out raises `StageTimeout` (retryable, and the
    retry resumes from the cached chunks).

    A paid but unusable response leaves a note next to the cache; a chunk with
    MAX_UNUSABLE_PER_CHUNK notes fails the job instead of being paid for again. On the
    first failing chunk, chunks not yet started are cancelled."""

    def cache_file(chunk: Chunk) -> Path:
        return _cache_path(cache_dir, chunk, backend, language, fingerprint)

    for chunk in chunks:
        if _unusable_count(cache_file(chunk)) >= MAX_UNUSABLE_PER_CHUNK:
            raise PermanentError(f"chunk {chunk.index}: transcription unusable twice; giving up")
    done: list[Chunk] = []
    lock = threading.Lock()
    with reserve_spend(
        ctx.conn,
        kind="transcribe",
        label="transcription",
        provider=backend.provider,
        job_id=job.id,
        estimate_usd=backend.cost_usd([c.duration for c in chunks]),
        cap_usd=ctx.config.settings.daily_transcribe_usd_cap,
        now=ctx.clock(),
    ) as charge:
        work_dir = Path(tempfile.mkdtemp(dir=ctx.media_dir, prefix=WORK_PREFIX))

        def run(chunk: Chunk) -> None:
            path = cut_chunk(audio, chunk, work_dir, timeout_s=deadline.timeout(CUT_TIMEOUT_S))
            try:
                result = backend.transcribe(
                    path, language=language, timeout_s=deadline.timeout(CHUNK_CALL_TIMEOUT_S)
                )
            except BilledChunkError as exc:
                with lock:
                    done.append(chunk)
                    count = _note_unusable(cache_file(chunk))
                if count >= MAX_UNUSABLE_PER_CHUNK:
                    raise PermanentError(
                        f"chunk {chunk.index}: transcription unusable twice ({exc}); giving up"
                    ) from None
                raise
            with lock:
                done.append(chunk)
            _save_chunk(cache_file(chunk), result)
            path.unlink(missing_ok=True)

        pool = ThreadPoolExecutor(max_workers=max(1, max_concurrency))
        try:
            list(pool.map(run, chunks))
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)  # stop paying for queued chunks
            raise
        else:
            pool.shutdown(wait=True)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            seconds = [c.duration for c in done]
            if seconds:  # billed even if a later chunk failed
                charge.usd = backend.cost_usd(seconds)
                charge.audio_seconds = backend.billed_seconds(seconds)


STAGE = StageDef(transcribe, ARTIFACT)
