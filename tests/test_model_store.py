"""The local Whisper model store under concurrent use (CLP-76): one lock and per-process
marker temp files.

The two-process test runs real processes, each with the fake hub of `asr_support` (no
network). The fake fetch takes 2 s and both processes start `ensure_model` together, so
without the lock both find no marker and both fetch.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from asr_support import (
    FAKE_MODEL_SHA256,
    FAKE_PINS,
    FakeAsr,
    install_fake_asr,
)
from clipper import config
from clipper.jobs import PermanentError
from clipper.pipeline import transcribe as tr
from clipper.pipeline.common import MediaError

TINY = ("Systran/faster-whisper-tiny", "d90ca5fe260221311c53c58e660288d3deb8d356")
OLD_REVISION = "0" * 40
TESTS_DIR = Path(__file__).parent

WORKER = """
import sys, time
from pathlib import Path

tests_dir, models, fetches, ready, name = sys.argv[1:6]
sys.path.insert(0, tests_dir)
from asr_support import FAKE_PINS, FakeAsr
from clipper import config
from clipper.pipeline import transcribe as tr
from clipper.pipeline import ytdlp

for model, pin in list(config.WHISPER_MODELS.items()):
    config.WHISPER_MODELS[model] = pin._replace(files=FAKE_PINS)


class SlowFetch(FakeAsr):
    def __call__(self, args, timeout_s, **kw):
        if list(args[len(tr.ASR_COMMAND):])[:1] == ["fetch"]:
            with open(fetches, "a") as fh:
                fh.write(name + "\\n")
            time.sleep(2.0)  # a slow download: the other process arrives meanwhile
        return super().__call__(args, timeout_s, **kw)


ytdlp.run_command = SlowFetch(ytdlp.run_command)
(Path(ready) / name).touch()
while len(list(Path(ready).iterdir())) < 2:
    time.sleep(0.01)
backend = tr.FasterWhisperTranscriber(model="tiny", models_dir=Path(models))
print(backend.ensure_model(timeout_s=60))
"""


@pytest.fixture
def fake_asr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAsr:
    return install_fake_asr(monkeypatch, tmp_path / "models")


def local() -> tr.FasterWhisperTranscriber:
    return tr.FasterWhisperTranscriber(model="tiny")


def _pin() -> config.PinnedModel:
    return config.PinnedModel(*TINY, files=FAKE_PINS)


@contextmanager
def held_lock(models: Path) -> Iterator[None]:
    """Another process's hold on the store lock (flock locks per open file)."""
    models.mkdir(parents=True, exist_ok=True)
    with (models / ".lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


# --------------------------------------------------------------------------- the lock


def test_two_processes_fetch_a_model_once_and_both_get_it(tmp_path: Path) -> None:
    models, fetches, ready = tmp_path / "models", tmp_path / "fetches.log", tmp_path / "ready"
    ready.mkdir()
    argv = [sys.executable, "-c", WORKER, str(TESTS_DIR), str(models), str(fetches), str(ready)]
    procs = [
        subprocess.Popen([*argv, name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603
        for name in ("w1", "w2")
    ]
    try:
        outs = [p.communicate(timeout=120) for p in procs]
    finally:
        for p in procs:
            p.kill()

    assert [p.returncode for p in procs] == [0, 0], [err for _, err in outs]
    snapshot = models / "models--Systran--faster-whisper-tiny" / "snapshots" / TINY[1]
    assert [out.strip() for out, _ in outs] == [str(snapshot)] * 2
    assert len(fetches.read_text().splitlines()) == 1
    assert (snapshot / tr.COMPLETE_MARKER).read_text() == FAKE_MODEL_SHA256
    assert tr.snapshot_problems(snapshot, _pin()) == {}


def test_ensure_model_waits_for_the_lock_then_gives_up_retryably(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    models = tmp_path / "models"
    with held_lock(models), pytest.raises(MediaError, match="model store busy") as exc:
        local().ensure_model(timeout_s=1.5)
    assert not isinstance(exc.value, PermanentError)
    assert fake_asr.fetch_calls() == []
    assert local().ensure_model(timeout_s=5)  # free again: fetched as usual
    assert len(fake_asr.fetch_calls()) == 1


def test_the_fast_path_also_takes_the_lock(fake_asr: FakeAsr, tmp_path: Path) -> None:
    """A marked snapshot is checked under the lock too: another process may be healing it."""
    local().ensure_model()
    with held_lock(tmp_path / "models"), pytest.raises(MediaError, match="model store busy"):
        local().ensure_model(timeout_s=1.0)


def test_the_marker_is_written_through_a_per_process_temp_file(
    fake_asr: FakeAsr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[str] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replaced.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(tr.os, "replace", spy)
    local().ensure_model()
    assert replaced == [f"{tr.COMPLETE_MARKER}.{os.getpid()}.tmp"]


def test_a_stale_marker_temp_file_does_not_cost_a_fetch(fake_asr: FakeAsr, tmp_path: Path):
    """A process killed between writing the temp file and renaming it leaves one behind."""
    snapshot = local().ensure_model()
    (snapshot / f"{tr.COMPLETE_MARKER}.4242.tmp").write_text("partial")
    assert local().ensure_model() == snapshot
    assert len(fake_asr.fetch_calls()) == 1
