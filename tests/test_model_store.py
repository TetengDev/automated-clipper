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
import stat
import subprocess
import sys
import threading
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
    FakeWhisperModel,
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
    (models / ".lock").touch()
    with (models / ".lock").open("rb") as fh:
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_lock_file_this_process_cannot_write_still_locks(fake_asr: FakeAsr, tmp_path: Path):
    """`bench-transcribe` run as another uid leaves a lock file the worker cannot write;
    `flock` needs only read access."""
    lock = tmp_path / "models" / ".lock"
    lock.parent.mkdir()
    lock.touch(mode=0o444)
    assert local().ensure_model()
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

    new = local().ensure_model(prune=True)

    assert new != old and not old.exists()
    assert not old_blob.exists()
    assert tr.snapshot_problems(new, _pin()) == {}  # shared blobs and store file kept
    assert _store_file(models, FAKE_MODEL_BIN).is_file()
    assert local().ensure_model(prune=True) == new
    assert len(fake_asr.fetch_calls()) == 2


def test_only_a_caller_that_asks_prunes(fake_asr: FakeAsr, tmp_path: Path) -> None:
    """`bench-transcribe` and the quality gate fetch without pruning: a checkout with other
    pins must never delete the worker's pinned revision. Only the transcribe stage prunes."""
    stale = write_snapshot(tmp_path / "models", TINY[0], OLD_REVISION)
    local().ensure_model()
    assert stale.is_dir()


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

    snapshot = local().ensure_model(prune=True)

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
    local().ensure_model(prune=True)
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

    local().ensure_model(prune=True)

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
            snapshot = local().ensure_model(prune=True)
    finally:
        stuck.chmod(0o755)
    assert snapshot.is_dir() and (snapshot / tr.COMPLETE_MARKER).is_file()
    assert not later.parents[1].exists()
    assert stuck.is_dir()
    assert f"cannot delete {stuck / 'config.json'}: PermissionError" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("unreadable", ["revision", "snapshots", "blobs"])
def test_an_unreadable_snapshot_keeps_every_blob(
    fake_asr: FakeAsr, tmp_path: Path, caplog: pytest.LogCaptureFixture, unreadable: str
) -> None:
    """What the prune cannot read it cannot know is unused: a pinned snapshot or blobs dir
    it cannot list (e.g. root-owned) keeps its blobs and store files. Stale snapshots
    still go."""
    models = tmp_path / "models"
    tiny = write_snapshot(models, *TINY, arrives={"model.bin": b"tiny weights"})
    blobs = sorted(os.listdir(tiny.parents[1] / "blobs"))
    stale = write_snapshot(models, "Systran/faster-whisper-retired", OLD_REVISION)
    locked = {"revision": tiny, "snapshots": tiny.parent, "blobs": tiny.parents[1] / "blobs"}[
        unreadable
    ]
    locked.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger=tr.__name__):
            tr.FasterWhisperTranscriber(model="base").ensure_model(prune=True)
    finally:
        locked.chmod(0o755)
    assert sorted(os.listdir(tiny.parents[1] / "blobs")) == blobs
    assert _store_file(models, b"tiny weights").is_file()
    assert (tiny / "model.bin").read_bytes() == b"tiny weights"
    assert not stale.exists()
    assert f"cannot read {locked}" in caplog.text


def test_nothing_is_pruned_while_another_process_holds_the_lock(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    stale = write_snapshot(tmp_path / "models", TINY[0], OLD_REVISION)
    with held_lock(tmp_path / "models"), pytest.raises(MediaError):
        local().ensure_model(timeout_s=1.0, prune=True)
    assert stale.is_dir()
    local().ensure_model(prune=True)
    assert not stale.exists()


def test_a_failing_prune_still_returns_the_verified_snapshot(
    fake_asr: FakeAsr, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The snapshot is verified and marked before the prune runs: a prune bug must not
    fail the attempt."""

    def broken(models_dir: Path) -> int:
        raise RuntimeError("prune bug")

    monkeypatch.setattr(tr, "prune_unpinned", broken)
    with caplog.at_level(logging.ERROR, logger=tr.__name__):
        snapshot = local().ensure_model(prune=True)
    assert (snapshot / tr.COMPLETE_MARKER).is_file()
    assert "pruning unpinned revisions failed" in caplog.text


def test_a_pruned_store_file_takes_its_manifest_and_lock_file_along(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    """The Hub client keeps `<hash>.refs` and `<hash>.lock` next to each store file."""
    models = tmp_path / "models"
    write_snapshot(models, *TINY)  # not marked yet: the next call fetches and prunes
    write_snapshot(
        models, "Systran/faster-whisper-retired", OLD_REVISION, arrives={"model.bin": b"old"}
    )
    old = _store_file(models, b"old")
    kept = _store_file(models, FAKE_MODEL_BIN)
    for store in (old, kept):
        for suffix in (".refs", ".lock"):
            store.with_name(store.name + suffix).write_text("")

    local().ensure_model(prune=True)

    assert not old.exists()
    assert not old.with_name(old.name + ".refs").exists()
    assert not old.with_name(old.name + ".lock").exists()
    assert kept.with_name(kept.name + ".refs").exists()
    assert kept.with_name(kept.name + ".lock").exists()


def test_a_fifo_planted_as_the_lock_file_is_refused_without_hanging(
    fake_asr: FakeAsr, tmp_path: Path
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    os.mkfifo(models / ".lock")
    outcome: list[BaseException | Path] = []

    def run() -> None:
        try:
            outcome.append(local().ensure_model(timeout_s=5))
        except BaseException as exc:  # surfaced below
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)  # a hung open must not hang pytest
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "ensure_model blocked opening a FIFO"
    (error,) = outcome
    assert isinstance(error, MediaError) and not isinstance(error, PermanentError)
    assert "not a regular file" in str(error)
    assert fake_asr.fetch_calls() == []


def test_a_dir_planted_as_the_lock_file_is_refused(fake_asr: FakeAsr, tmp_path: Path) -> None:
    (tmp_path / "models" / ".lock").mkdir(parents=True)
    with pytest.raises(MediaError, match="not a regular file"):
        local().ensure_model(timeout_s=5)
    assert fake_asr.fetch_calls() == []


def test_the_lock_file_is_shared_with_every_uid(fake_asr: FakeAsr, tmp_path: Path) -> None:
    """Like the Hub client's own locks: another uid's umask must not lock the worker out."""
    previous = os.umask(0o077)
    try:
        local().ensure_model()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(tmp_path / "models" / ".lock").st_mode) == 0o666


def test_a_failed_load_leaves_the_marker_while_another_process_holds_the_lock(
    fake_asr: FakeAsr,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That process may be checking or healing the snapshot; the marker is dropped on a
    later failed load instead, when the lock is free."""
    snapshot = local().ensure_model()
    monkeypatch.setattr(FakeWhisperModel, "load_error", RuntimeError("Unable to load model.bin"))
    with (
        held_lock(tmp_path / "models"),
        caplog.at_level(logging.WARNING, logger=tr.__name__),
        pytest.raises(MediaError, match="model load failed"),
    ):
        local().transcribe(tmp_path / "c.flac", language="en")
    assert (snapshot / tr.COMPLETE_MARKER).is_file()
    assert "model store busy; marker kept" in caplog.text
    with pytest.raises(MediaError, match="model load failed"):
        local().transcribe(tmp_path / "c.flac", language="en")  # lock free now
    assert not (snapshot / tr.COMPLETE_MARKER).exists()
