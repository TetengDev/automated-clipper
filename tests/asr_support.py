"""Local Whisper test doubles (CLP-35): never download or run a real model.

`FakeAsr` stands in for the ASR child process at the runner boundary: it answers
`python -I -m clipper.pipeline.local_asr ...` in-process with the real child code
(`local_asr.main`, guards included) and a `FakeWhisperModel`; every other command
(ffmpeg, ffprobe, yt-dlp fakes) goes to the wrapped runner.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clipper import config
from clipper.pipeline import local_asr, ytdlp
from clipper.pipeline import transcribe as tr


def seg(text: str, start: float = 0.0, *, words: Any = None, **probs: float) -> SimpleNamespace:
    """A faster-whisper Segment look-alike (only the attributes the child reads)."""
    if words is None:
        words = [
            (f" {w}", start + i * 0.4, start + i * 0.4 + 0.3) for i, w in enumerate(text.split())
        ]
    return SimpleNamespace(
        start=start,
        end=start + 2.0,
        text=f" {text}",
        words=[SimpleNamespace(word=w, start=a, end=b) for w, a, b in words],
        no_speech_prob=probs.get("no_speech_prob", 0.01),
        avg_logprob=probs.get("avg_logprob", -0.2),
        compression_ratio=probs.get("compression_ratio", 1.4),
    )


class FakeWhisperModel:
    """faster_whisper.WhisperModel with the exact keyword arguments the child passes."""

    instances: list[FakeWhisperModel] = []
    segments: list[SimpleNamespace] = []
    language = "tl"

    def __init__(
        self,
        model_size_or_path: str,
        *,
        device: str,
        compute_type: str,
        cpu_threads: int,
        local_files_only: bool,
    ) -> None:
        self.model = model_size_or_path
        self.kwargs = {
            "device": device,
            "compute_type": compute_type,
            "cpu_threads": cpu_threads,
            "local_files_only": local_files_only,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        FakeWhisperModel.instances.append(self)

    def transcribe(
        self,
        audio: str,
        *,
        language: str | None,
        word_timestamps: bool,
        vad_filter: bool,
        condition_on_previous_text: bool,
    ) -> tuple[Any, SimpleNamespace]:
        self.calls.append(
            (
                audio,
                {
                    "language": language,
                    "word_timestamps": word_timestamps,
                    "vad_filter": vad_filter,
                    "condition_on_previous_text": condition_on_previous_text,
                },
            )
        )
        return iter(list(self.segments)), SimpleNamespace(language=language or self.language)


FAKE_MODEL_BIN = b"fake ctranslate2 weights"
FAKE_MODEL_SHA256 = hashlib.sha256(FAKE_MODEL_BIN).hexdigest()


def write_snapshot(
    models_dir: Path,
    repo: str,
    revision: str,
    *,
    model_bin: bytes = FAKE_MODEL_BIN,
    files: Sequence[str] = ("config.json", "tokenizer.json", "vocabulary.txt"),
) -> Path:
    """What `snapshot_download` leaves: content-addressed blobs, snapshot symlinks."""
    repo_dir = models_dir / f"models--{repo.replace('/', '--')}"
    snap = repo_dir / "snapshots" / revision
    (repo_dir / "blobs").mkdir(parents=True, exist_ok=True)
    snap.mkdir(parents=True, exist_ok=True)
    for name, data in [("model.bin", model_bin), *((f, b"{}") for f in files)]:
        digest = hashlib.sha256(data).hexdigest()
        (repo_dir / "blobs" / digest).write_bytes(data)
        link = snap / name
        if not link.is_symlink():
            os.symlink(os.path.relpath(repo_dir / "blobs" / digest, snap), link)
    return snap


class FakeAsr:
    def __init__(self, inner: Callable[..., ytdlp.RunResult]) -> None:
        self.inner = inner
        self.fetch_plan: list[str] = []
        """Per fetch: "ok", "partial" (no tokenizer.json) or "corrupt" (model.bin hash)."""
        self.calls: list[tuple[list[str], float]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, args: Sequence[str], timeout_s: float, **kw: Any) -> ytdlp.RunResult:
        if tuple(args[: len(tr.ASR_COMMAND)]) != tr.ASR_COMMAND:
            return self.inner(args, timeout_s, **kw)
        argv = list(args[len(tr.ASR_COMMAND) :])
        with self._lock:
            self.calls.append((argv, timeout_s))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if argv[0] == "fetch":
                _, repo, sha, models_dir = argv
                plan = self.fetch_plan.pop(0) if self.fetch_plan else "ok"
                snap = write_snapshot(
                    Path(models_dir),
                    repo,
                    sha,
                    model_bin=b"truncated" if plan == "corrupt" else FAKE_MODEL_BIN,
                    files=("config.json", "vocabulary.txt")
                    if plan == "partial"
                    else ("config.json", "tokenizer.json", "vocabulary.txt"),
                )
                return ytdlp.RunResult(0, str(snap), "")
            time.sleep(0.02)  # let a second worker thread overlap, if there were one
            out = io.StringIO()
            code = local_asr.main(argv, out=out, model_class=FakeWhisperModel)
            return ytdlp.RunResult(code, out.getvalue(), "")
        finally:
            with self._lock:
                self.active -= 1

    def chunk_calls(self) -> list[list[str]]:
        return [a for a, _ in self.calls if a[0] != "fetch"]

    def fetch_calls(self) -> list[list[str]]:
        return [a for a, _ in self.calls if a[0] == "fetch"]


def install_fake_asr(monkeypatch: pytest.MonkeyPatch, models_dir: Path) -> FakeAsr:
    FakeWhisperModel.instances = []
    FakeWhisperModel.segments = [seg("kumusta mga lods", 0.0), seg("salamat po", 3.0)]
    FakeWhisperModel.language = "tl"
    monkeypatch.setenv("CLIPPER_MODELS_DIR", str(models_dir))
    for name, pin in list(config.WHISPER_MODELS.items()):  # the fake weights are "pinned"
        monkeypatch.setitem(config.WHISPER_MODELS, name, pin._replace(sha256=FAKE_MODEL_SHA256))
    fake = FakeAsr(ytdlp.run_command)
    monkeypatch.setattr(ytdlp, "run_command", fake)
    return fake
