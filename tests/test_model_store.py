"""The local Whisper model store under concurrent use (CLP-76): one lock, per-process
marker temp files, and pruning revisions no pin names any more.

The two-process test runs real processes, each with the fake hub of `asr_support` (no
network). The fake fetch takes 2 s and both processes start `ensure_model` together, so
without the lock both find no marker and both fetch.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from asr_support import (
    FAKE_FILES,
    FAKE_MODEL_BIN,
    FAKE_MODEL_SHA256,
    FAKE_PINS,
    FakeAsr,
    install_fake_asr,
    write_snapshot,
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


# --------------------------------------------------------------------------- pruning


def _store_file(models: Path, data: bytes) -> Path:
    store_id = hashlib.sha256(b"xet" + data).hexdigest()  # as write_snapshot names it
    return models / "blobs" / store_id[:2] / store_id


def _old_only_blob(snapshot: Path, name: str, data: bytes) -> Path:
    """A file that only the old revision has, linked like the Hub client links it."""
    blob = snapshot.parents[1] / "blobs" / hashlib.sha1(data).hexdigest()  # noqa: S324
    blob.write_bytes(data)
    os.symlink(os.path.relpath(blob, snapshot), snapshot / name)
    return blob


def test_a_revision_no_longer_pinned_is_pruned_after_the_new_one_is_fetched(
    fake_asr: FakeAsr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin moved on (a deploy): the first job fetches the new revision and the old one
    goes, with the files only it used. Files both revisions share stay."""
    models = tmp_path / "models"
    monkeypatch.setitem(config.WHISPER_MODELS, "tiny", _pin()._replace(revision=OLD_REVISION))
    old = local().ensure_model()
    old_blob = _old_only_blob(old, "preprocessor_config.json", b'{"old": true}')
    monkeypatch.setitem(config.WHISPER_MODELS, "tiny", _pin())

    new = local().ensure_model()

    assert new != old and not old.exists()
    assert not old_blob.exists()
    assert tr.snapshot_problems(new, _pin()) == {}  # shared blobs and store file kept
    assert _store_file(models, FAKE_MODEL_BIN).is_file()
    assert local().ensure_model() == new
    assert len(fake_asr.fetch_calls()) == 2


def test_a_repo_no_model_uses_is_pruned_with_its_store_files(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    models = tmp_path / "models"
    gone = write_snapshot(
        models, "Systran/faster-whisper-retired", OLD_REVISION, arrives={"model.bin": b"old"}
    )
    shares = write_snapshot(models, "Systran/faster-whisper-twin", OLD_REVISION)
    face_model = models / "face_detection_short_range.tflite"
    face_model.write_bytes(b"reframe's model")

    snapshot = local().ensure_model()

    assert not gone.parents[1].exists()  # the whole repo dir
    assert not _store_file(models, b"old").exists()
    assert not shares.parents[1].exists()
    assert _store_file(models, FAKE_MODEL_BIN).is_file()  # pinned tiny uses it
    assert tr.snapshot_problems(snapshot, _pin()) == {}
    assert face_model.read_bytes() == b"reframe's model"
    assert (models / ".lock").is_file()


def test_every_pinned_revision_survives_the_prune(fake_asr: FakeAsr, tmp_path: Path) -> None:
    models = tmp_path / "models"
    pinned = [
        write_snapshot(models, pin.repo, pin.revision)
        for name, pin in config.WHISPER_MODELS.items()
        if name != "tiny"
    ]
    local().ensure_model()
    for snapshot in pinned:
        assert sorted(os.listdir(snapshot)) == sorted(["model.bin", *FAKE_FILES])
        assert (snapshot / "model.bin").read_bytes() == FAKE_MODEL_BIN


def test_the_prune_never_follows_links_out_of_the_models_dir(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    models = tmp_path / "models"
    outside = tmp_path / "elsewhere"
    (outside / "snapshots" / OLD_REVISION).mkdir(parents=True)
    precious = outside / "precious.bin"
    precious.write_bytes(b"keep me")
    (outside / "snapshots" / OLD_REVISION / "model.bin").write_bytes(b"keep me too")
    stale = write_snapshot(models, TINY[0], OLD_REVISION)
    os.symlink(precious, stale / "planted.bin")  # inside a stale snapshot
    blobs = stale.parents[1] / "blobs"
    os.symlink(precious, blobs / "planted-blob")  # a repo blob nothing links
    models.mkdir(exist_ok=True)
    os.symlink(outside, models / "models--evil--linked")  # a whole repo dir that is a link
    os.symlink(outside, (models / "blobs" / "ff"))  # a store prefix dir that is a link

    local().ensure_model()

    assert precious.read_bytes() == b"keep me"
    assert (outside / "snapshots" / OLD_REVISION / "model.bin").read_bytes() == b"keep me too"
    assert not stale.exists()
    assert not (blobs / "planted-blob").is_symlink()  # the link itself went, not its target


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_undeletable_path_is_logged_and_the_prune_goes_on(
    fake_asr: FakeAsr, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    models = tmp_path / "models"
    stuck = write_snapshot(models, "Systran/faster-whisper-stuck", OLD_REVISION)
    later = write_snapshot(models, "Systran/faster-whisper-zzz", OLD_REVISION)
    stuck.chmod(0o555)  # its links cannot be removed
    try:
        with caplog.at_level(logging.WARNING, logger=tr.__name__):
            snapshot = local().ensure_model()
    finally:
        stuck.chmod(0o755)
    assert snapshot.is_dir() and (snapshot / tr.COMPLETE_MARKER).is_file()
    assert not later.parents[1].exists()
    assert stuck.is_dir()
    assert f"cannot delete {stuck / 'config.json'}: PermissionError" in caplog.text


def test_nothing_is_pruned_while_another_process_holds_the_lock(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    stale = write_snapshot(tmp_path / "models", TINY[0], OLD_REVISION)
    with held_lock(tmp_path / "models"), pytest.raises(MediaError):
        local().ensure_model(timeout_s=1.0)
    assert stale.is_dir()
    local().ensure_model()
    assert not stale.exists()
