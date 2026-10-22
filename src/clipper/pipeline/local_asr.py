"""Local Whisper child process (CLP-35). The worker runs one per chunk, through
`run_command` (hard timeout with a process-group kill, allowlisted env without secrets,
capped stderr), so a hung or bloated model can never take the worker down with it and
the model's memory is returned when the child exits.

    python -I -m clipper.pipeline.local_asr
        <model_dir> <compute_type> <cpu_threads> <language|-> <chunk.flac>
      stdout: {"language", "segments": [{start, end, text}], "words": [{word, start, end}],
                 "dropped": {"segments", "words"}}   (Groq's verbose_json shape)
    python -I -m clipper.pipeline.local_asr fetch <repo_id> <commit_sha> <models_dir>
        downloads one pinned model snapshot into the Hugging Face cache layout.

Hallucination guards run here: segments the model "heard" in silence, repetition loops,
and words outside the Latin script when the language is en/tl.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from typing import IO, Any

GUARD_VERSION = 1
"""Bump when the guards change: cached chunk results from older guards are not reused."""
NO_SPEECH_PROB = 0.6
LOW_AVG_LOGPROB = -1.0
MAX_COMPRESSION_RATIO = 2.4
LATIN_LANGUAGES = frozenset({"en", "tl"})
COMPUTE_TYPES = ("int8", "int8_float16", "float16", "float32")
TRANSCRIBE_KWARGS: dict[str, Any] = {
    "word_timestamps": True,
    "vad_filter": True,
    "condition_on_previous_text": False,
}
# The files faster-whisper 1.2.1 `download_model` fetches.
ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)
HF_ENDPOINT = "https://huggingface.co"
FETCH_ETAG_TIMEOUT_S = 30.0
MAX_ADDRESS_SPACE_BYTES = 12 * 2**30
"""RLIMIT_AS for the child (Linux). CTranslate2/onnxruntime reserve far more address space
than they touch: measured in the image with 4 threads, tiny needs 2 GiB, medium and
large-v3-turbo 6 GiB, large-v3 8 GiB (peak RSS 0.3 / 2.9 / 2.6 / 5.1 GB). 12 GiB leaves
room for more threads; it stops a runaway child where no cgroup limit does (compose
already caps the worker at 8g). Too tight a limit fails the model load."""

EXIT_USAGE = 2
EXIT_MODEL = 3
"""The model could not be loaded (missing/corrupt files, faster-whisper not installed):
the parent treats this as permanent. Any other failure is retryable."""

_LANGUAGE = re.compile(r"^[a-z]{2,3}$")
_REPO = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_LATIN_EXTRAS = frozenset("ªºµ")  # Latin-1 letters NFKC would turn into a/o/Greek mu


def keep_segment(no_speech_prob: float, avg_logprob: float, compression_ratio: float) -> bool:
    """False for a likely hallucination: speech "heard" in silence, or a repetition loop."""
    if no_speech_prob > NO_SPEECH_PROB and avg_logprob < LOW_AVG_LOGPROB:
        return False
    return compression_ratio <= MAX_COMPRESSION_RATIO


def _latin_char(ch: str) -> bool:
    if not ch.isalpha() or ch in _LATIN_EXTRAS:
        return True
    return all(
        not c.isalpha() or unicodedata.name(c, "").startswith(("LATIN", "MODIFIER LETTER"))
        for c in unicodedata.normalize("NFKC", ch)
    )


def latin_word(word: str) -> bool:
    """Every letter is Latin script after NFKC (fullwidth Latin, diacritics like ñ/é and
    modifier letters like ʼ pass); digits and punctuation pass. Whisper sometimes renders
    Taglish as CJK/Cyrillic fragments."""
    return all(_latin_char(ch) for ch in word)


def model_kwargs(compute_type: str, cpu_threads: int) -> dict[str, Any]:
    return {
        "device": "cpu",
        "compute_type": compute_type,
        "cpu_threads": cpu_threads,
        "local_files_only": True,
    }


def transcribe_chunk(model: Any, audio: str, language: str | None) -> dict[str, Any]:
    segments, info = model.transcribe(audio, language=language, **TRANSCRIBE_KWARGS)
    latin_only = (language or info.language) in LATIN_LANGUAGES
    out_segments: list[dict[str, Any]] = []
    out_words: list[dict[str, Any]] = []
    dropped_segments = dropped_words = 0
    for s in segments:  # lazy: decoding happens while iterating
        if not keep_segment(s.no_speech_prob, s.avg_logprob, s.compression_ratio):
            dropped_segments += 1
            continue
        words = [w for w in s.words or () if w.word.strip()]
        kept = [w for w in words if not latin_only or latin_word(w.word)]
        dropped_words += len(words) - len(kept)
        text = "".join(w.word for w in kept).strip()  # Whisper's own spacing
        if not text:
            dropped_segments += 1
            continue
        start, end = (s.start, s.end) if len(kept) == len(words) else (kept[0].start, kept[-1].end)
        out_segments.append({"start": float(start), "end": float(end), "text": text})
        out_words.extend(
            {"word": w.word.strip(), "start": float(w.start), "end": float(w.end)} for w in kept
        )
    return {
        "language": info.language,
        "segments": out_segments,
        "words": out_words,
        "dropped": {"segments": dropped_segments, "words": dropped_words},
    }


def _fetch(repo_id: str, revision: str, models_dir: str, out: IO[str]) -> int:
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id,
        revision=revision,
        allow_patterns=list(ALLOW_PATTERNS),
        cache_dir=models_dir,
        endpoint=HF_ENDPOINT,  # never HF_ENDPOINT from the environment
        token=False,  # public repos; never send a token
        etag_timeout=FETCH_ETAG_TIMEOUT_S,
    )
    out.write(str(path))
    return 0


def _usage(message: str) -> int:
    print(f"local_asr: {message}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: Sequence[str], out: IO[str] = sys.stdout, model_class: Any = None) -> int:
    if argv[:1] == ["fetch"]:
        if len(argv) != 4 or not _REPO.match(argv[1]) or not _SHA.match(argv[2]):
            return _usage("fetch needs <repo_id> <40-hex commit sha> <models_dir>")
        return _fetch(argv[1], argv[2], argv[3], out)
    if len(argv) != 5:
        return _usage("expected <model_dir> <compute_type> <cpu_threads> <language|-> <audio>")
    model_dir, compute_type, threads, language, audio = argv
    if compute_type not in COMPUTE_TYPES:
        return _usage(f"bad compute type {compute_type!r}")
    if not threads.isdigit() or not 1 <= int(threads) <= 64:
        return _usage(f"bad cpu thread count {threads!r}")
    if language != "-" and not _LANGUAGE.match(language):
        return _usage(f"bad language {language!r}")
    try:
        if model_class is None:
            from faster_whisper import WhisperModel as model_class
        model = model_class(model_dir, **model_kwargs(compute_type, int(threads)))
    except Exception as exc:
        print(f"cannot load model: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_MODEL
    result = transcribe_chunk(model, audio, None if language == "-" else language)
    json.dump(result, out, ensure_ascii=False)
    return 0


def _limit_address_space() -> None:
    if MAX_ADDRESS_SPACE_BYTES and sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES))


if __name__ == "__main__":  # pragma: no cover - exercised as a real child process
    args = sys.argv[1:]
    if args[:1] == ["fetch"] and len(args) == 4:
        # Hub state (locks, xet cache) under the models dir: the image's root is read-only.
        os.environ["HF_HOME"] = os.path.join(args[3], ".hf-home")
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"  # transcription never touches the network
        _limit_address_space()
    sys.exit(main(args))
