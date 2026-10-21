"""Export of approved clips to output/ready/<date>/ + rclone (CLP-21)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clipper import clips, export
from clipper.db import connect
from clipper.export import MAX_EXPORT_FAILURES, export_clip, export_pending, slugify
from review_support import Env, RecordingRunner, make_env

APPROVED_AT = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
LATER = datetime(2027, 1, 17, 9, 30, tzinfo=UTC)
VIDEO = b"\x00\x00\x00\x18ftypmp42fake-video-bytes"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def approved_clip(
    env: Env,
    *,
    title: str | None = "Big Moment!",
    creator: str = "Kai Cenat",
    video: str = "job/clip_v1.mp4",
    item_id: str | None = None,
    **kwargs: object,
) -> int:
    path = env.media_root / video
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(VIDEO)
    job = env.job(creator, item_id=item_id, title="Source")
    clip_id = env.clip(job, title=title, video_path=video, **kwargs)  # type: ignore[arg-type]
    clips.approve(env.conn, clip_id, APPROVED_AT)
    return clip_id


def run(env: Env, clip_id: int, *, now: datetime = LATER, **kwargs: object):
    params: dict[str, object] = {
        "output_dir": env.output_dir,
        "media_root": env.media_root,
        "now": now,
        "runner": env.runner,
    }
    params.update(kwargs)
    return export_clip(env.conn, clip_id, **params)  # type: ignore[arg-type]


def test_export_writes_package_to_dated_folder(env: Env) -> None:
    clip_id = approved_clip(env, hashtags=["funny", "#viral"], start_s=125.0, item_id="dQw4w9WgXcQ")

    result = run(env, clip_id)

    day_dir = env.output_dir / "ready" / "2027-01-15"
    stem = f"kai-cenat_big-moment-{clip_id}_v1"
    assert result.status == "exported"
    assert result.files == (
        day_dir / f"{stem}.mp4",
        day_dir / f"{stem}.caption.txt",
        day_dir / f"{stem}.json",
    )
    assert (day_dir / f"{stem}.mp4").read_bytes() == VIDEO
    assert (day_dir / f"{stem}.caption.txt").read_text() == "Watch this\n\n#funny #viral\n"
    meta = json.loads((day_dir / f"{stem}.json").read_text())
    assert meta["title"] == "Big Moment!"
    assert meta["creator"] == "Kai Cenat"
    assert meta["source_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=125s"
    assert meta["score"] == 7.5
    assert meta["reason"] == "Strong hook and payoff"
    assert meta["prompt_version"] == "select-v3"
    assert (meta["clip_id"], meta["version"]) == (clip_id, 1)
    assert sorted(p.name for p in day_dir.iterdir()) == [
        f"{stem}.caption.txt",
        f"{stem}.json",
        f"{stem}.mp4",
    ]
    assert list((env.output_dir / ".staging").iterdir()) == []  # staging cleaned up
    assert env.status(clip_id) == "exported"
    assert [r["action"] for r in env.reviews(clip_id)] == ["approve", "export"]


def test_export_is_idempotent(env: Env) -> None:
    clip_id = approved_clip(env)

    first = run(env, clip_id)
    second = run(env, clip_id)

    assert first.status == "exported"
    assert second.status == "already_exported"
    assert second.files == first.files
    assert len(list(env.output_dir.rglob("*.mp4"))) == 1
    assert [r["action"] for r in env.reviews(clip_id)] == ["approve", "export"]


def test_recorded_export_is_not_repeated_after_crash(env: Env) -> None:
    clip_id = approved_clip(env)
    run(env, clip_id)
    # Simulate a crash between recording the export and flipping the status.
    env.conn.execute("UPDATE clips SET status = 'approved' WHERE id = ?", (clip_id,))
    for f in env.output_dir.rglob("*.mp4"):
        f.unlink()

    result = run(env, clip_id)

    assert result.status == "already_exported"
    assert env.status(clip_id) == "exported"
    assert list(env.output_dir.rglob("*.mp4")) == []


def test_not_approved_clip_is_not_exported(env: Env) -> None:
    clip_id = env.clip()

    assert run(env, clip_id).status == "not_approved"
    assert not env.output_dir.exists()
    with pytest.raises(LookupError):
        run(env, 999)


def test_missing_source_keeps_approved_and_records_error(env: Env) -> None:
    clip_id = approved_clip(env)
    (env.media_root / "job/clip_v1.mp4").unlink()

    result = run(env, clip_id)

    assert result.status == "failed"
    assert "missing" in (result.error or "")
    assert env.status(clip_id) == "approved"
    failed = env.reviews(clip_id)[-1]
    assert failed["action"] == "export_failed"
    assert "missing" in json.loads(failed["payload"])["error"]


def test_source_outside_media_root_is_refused(env: Env, tmp_path: Path) -> None:
    (tmp_path / "outside.mp4").write_bytes(VIDEO)
    job = env.job()
    clip_id = env.clip(job, video_path="../outside.mp4")
    clips.approve(env.conn, clip_id, APPROVED_AT)

    assert run(env, clip_id).status == "failed"
    assert not list(env.output_dir.rglob("*.mp4"))


def test_rclone_uploads_complete_staged_package_before_publishing(env: Env) -> None:
    clip_id = approved_clip(env)
    ready = env.output_dir / "ready"
    seen: dict[str, object] = {}

    def runner(args: list[str]) -> None:
        seen["args"] = args
        seen["staged"] = sorted(p.name for p in Path(args[3]).iterdir())
        seen["ready"] = list(ready.rglob("*.*"))

    result = run(env, clip_id, rclone_remote="gdrive:clips", runner=runner)

    args = seen["args"]
    assert result.status == "exported"
    assert args[:3] == ["rclone", "copy", "--"]  # type: ignore[index]
    assert args[4] == "gdrive:clips/2027-01-15"  # type: ignore[index]
    assert Path(args[3]).parent == env.output_dir / ".staging"  # type: ignore[index]
    assert seen["staged"] == sorted(p.name for p in result.files)
    assert seen["ready"] == []  # nothing published until the upload succeeded
    payload = json.loads(env.reviews(clip_id)[-1]["payload"])
    assert payload["rclone_remote"] == "gdrive:clips"


def test_no_rclone_without_remote(env: Env) -> None:
    run(env, approved_clip(env))

    assert env.runner.calls == []


@pytest.mark.parametrize(
    ("remote", "dest"), [("gdrive:", "gdrive:2027-01-15"), ("gdrive:a/", "gdrive:a/2027-01-15")]
)
def test_rclone_destination_joining(remote: str, dest: str, tmp_path: Path) -> None:
    assert export.rclone_args(remote, tmp_path, "2027-01-15")[-1] == dest


def test_rclone_failure_is_retried_and_alerts_after_three(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.runner = RecordingRunner(fail=MAX_EXPORT_FAILURES)
    clip_id = approved_clip(env)

    results = [
        run(env, clip_id, rclone_remote="gdrive:clips") for _ in range(MAX_EXPORT_FAILURES + 1)
    ]

    assert [r.status for r in results] == ["failed", "failed", "failed", "exported"]
    assert [r.alert for r in results] == [False, False, True, False]
    assert results[2].failures == MAX_EXPORT_FAILURES
    assert "quota exceeded" in (results[0].error or "")
    assert any("ALERT" in rec.message for rec in caplog.records if rec.levelno == logging.ERROR)
    assert len(list(env.output_dir.rglob("*.mp4"))) == 1  # failed attempts publish nothing
    assert env.status(clip_id) == "exported"
    actions = [r["action"] for r in env.reviews(clip_id)]
    assert actions == ["approve"] + ["export_failed"] * 3 + ["export"]


def test_missing_rclone_binary_is_a_recorded_failure(env: Env) -> None:
    def no_binary(_args: list[str]) -> None:
        raise FileNotFoundError("rclone")

    clip_id = approved_clip(env)

    result = run(env, clip_id, rclone_remote="gdrive:clips", runner=no_binary)

    assert result.status == "failed"
    assert env.status(clip_id) == "approved"
    assert list(env.output_dir.rglob("*.mp4")) == []
    assert list((env.output_dir / ".staging").iterdir()) == []


def test_export_pending_retries_all_approved(env: Env) -> None:
    first = approved_clip(env, title="One", video="a/one.mp4")
    second = approved_clip(env, title="Two", video="b/two.mp4")
    env.clip()  # pending, untouched

    results = export_pending(
        env.conn,
        output_dir=env.output_dir,
        media_root=env.media_root,
        now=LATER,
        runner=env.runner,
    )

    assert [(r.clip_id, r.status) for r in results] == [(first, "exported"), (second, "exported")]


def test_same_title_different_clips_do_not_overwrite(env: Env) -> None:
    first = approved_clip(env, title="Same", video="a/one.mp4")
    second = approved_clip(env, title="Same", video="b/two.mp4")

    a = run(env, first)
    b = run(env, second)

    assert a.files[0].name == f"kai-cenat_same-{first}_v1.mp4"
    assert b.files[0].name == f"kai-cenat_same-{second}_v1.mp4"
    assert a.files[0].parent == b.files[0].parent
    assert len(list(env.output_dir.rglob("*.mp4"))) == 2


def test_concurrent_export_of_same_clip_records_once(env: Env) -> None:
    clip_id = approved_clip(env)
    barrier = threading.Barrier(2)
    results: list[export.ExportResult] = []

    def worker() -> None:
        conn = connect(env.db_path, check_same_thread=False)
        try:
            barrier.wait()
            results.append(
                export_clip(
                    conn,
                    clip_id,
                    output_dir=env.output_dir,
                    media_root=env.media_root,
                    now=LATER,
                    runner=env.runner,
                )
            )
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r.status for r in results) in (
        ["already_exported", "exported"],
        ["already_exported", "already_exported"],  # second saw the status already flipped
    )
    assert [r["action"] for r in env.reviews(clip_id)].count("export") == 1
    assert env.status(clip_id) == "exported"
    [video] = env.output_dir.rglob("*.mp4")
    assert video.read_bytes() == VIDEO
    assert json.loads(video.with_suffix(".json").read_text())["clip_id"] == clip_id


def test_export_uses_current_version(env: Env) -> None:
    path = env.media_root / "v2.mp4"
    path.write_bytes(VIDEO)
    clip_id = env.clip(title="Edited", video_path="v2.mp4", versions=2)
    clips.approve(env.conn, clip_id, APPROVED_AT)

    result = run(env, clip_id)

    assert result.files[0].name == f"mrbeast_edited-{clip_id}_v2.mp4"
    assert env.reviews(clip_id)[-1]["version"] == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Big Moment!", "big-moment"),
        ("../../etc/passwd", "etc-passwd"),
        ("Ça va? 🔥 Résumé", "ca-va-resume"),
        ("", "clip"),
        (None, "clip"),
        ("🔥🔥", "clip"),
        ("a" * 100, "a" * 60),
        ("CON", "con"),
    ],
)
def test_slugify_is_filesystem_safe(text: str | None, expected: str) -> None:
    assert slugify(text) == expected


def test_failed_atomic_write_leaves_no_partial_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_id = approved_clip(env)

    def boom(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(export.shutil, "copyfileobj", boom)

    result = run(env, clip_id)

    assert result.status == "failed"
    assert "disk full" in (result.error or "")
    assert not (env.output_dir / "ready" / "2027-01-15").exists()  # no empty day dir
    assert list((env.output_dir / ".staging").iterdir()) == []


def test_rclone_failure_leaves_no_day_dir(env: Env) -> None:
    env.runner = RecordingRunner(fail=1)
    clip_id = approved_clip(env)

    result = run(env, clip_id, rclone_remote="gdrive:clips")

    assert result.status == "failed"
    assert not (env.output_dir / "ready").exists()


def test_retry_after_partial_publish_yields_one_row_and_intact_files(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_id = approved_clip(env)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # mp4 published, caption/json not yet
            raise OSError("disk hiccup")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(export.os, "replace", flaky_replace)
    first = run(env, clip_id)
    monkeypatch.setattr(export.os, "replace", real_replace)
    second = run(env, clip_id)

    assert first.status == "failed"
    assert second.status == "exported"
    actions = [r["action"] for r in env.reviews(clip_id)]
    assert actions == ["approve", "export_failed", "export"]
    day_dir = second.files[0].parent
    assert sorted(p.name for p in day_dir.iterdir()) == sorted(f.name for f in second.files)
    assert second.files[0].read_bytes() == VIDEO
    assert json.loads(second.files[2].read_text())["clip_id"] == clip_id


def test_day_dir_containment_checked_before_mkdir(env: Env, tmp_path: Path) -> None:
    clip_id = approved_clip(env)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env.output_dir.mkdir()
    os.symlink(outside, env.output_dir / "ready")

    result = run(env, clip_id)

    assert result.status == "failed"
    assert "escapes" in (result.error or "")
    assert list(outside.iterdir()) == []  # nothing created through the link


def test_version_change_during_export_fails(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    clip_id = approved_clip(env)
    real_get_clip = export.get_clip

    def bumped(conn: sqlite3.Connection, cid: int):  # type: ignore[no-untyped-def]
        clip = real_get_clip(conn, cid)
        return dataclasses.replace(clip, current=dataclasses.replace(clip.current, version=2))

    monkeypatch.setattr(export, "get_clip", bumped)

    result = run(env, clip_id)

    assert result.status == "failed"
    assert "version changed" in (result.error or "")
    assert env.status(clip_id) == "approved"


def test_unexpected_error_is_recorded_and_loop_continues(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = approved_clip(env, title="One", video="a/one.mp4")
    second = approved_clip(env, title="Two", video="b/two.mp4")
    real = export._publish_package

    def explode(conn: sqlite3.Connection, clip_id: int, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if clip_id == first:
            raise RuntimeError("unexpected")
        return real(conn, clip_id, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(export, "_publish_package", explode)

    results = export_pending(
        env.conn, output_dir=env.output_dir, media_root=env.media_root, now=LATER,
        runner=env.runner,
    )  # fmt: skip

    assert [(r.clip_id, r.status) for r in results] == [(first, "failed"), (second, "exported")]
    assert "RuntimeError: unexpected" in (results[0].error or "")
    assert env.reviews(first)[-1]["action"] == "export_failed"


def test_export_pending_survives_failure_to_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = approved_clip(env, title="One", video="a/one.mp4")

    def broken(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(export, "export_clip", broken)

    results = export_pending(
        env.conn, output_dir=env.output_dir, media_root=env.media_root, now=LATER,
        runner=env.runner,
    )  # fmt: skip

    assert [(r.clip_id, r.status) for r in results] == [(first, "failed")]
    assert "database is locked" in caplog.text


def test_sweep_staging_removes_only_old_entries(env: Env) -> None:
    staging = env.output_dir / ".staging"
    old_dir = staging / "old.abc"
    fresh_dir = staging / "fresh.abc"
    old_dir.mkdir(parents=True)
    fresh_dir.mkdir()
    (old_dir / "x.mp4").write_bytes(b"x")
    stamp = LATER.timestamp() - 2 * 3600
    os.utime(old_dir, (stamp, stamp))
    os.utime(fresh_dir, (LATER.timestamp(), LATER.timestamp()))

    removed = export.sweep_staging(env.output_dir, LATER)

    assert removed == 1
    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert export.sweep_staging(env.output_dir / "nope", LATER) == 0


def test_sweep_staging_unlinks_stray_files_and_links(env: Env, tmp_path: Path) -> None:
    staging = env.output_dir / ".staging"
    staging.mkdir(parents=True)
    target = tmp_path / "keep"
    target.mkdir()
    (staging / "stray.tmp").write_bytes(b"x")
    os.symlink(target, staging / "link")
    old = LATER.timestamp() - 2 * 3600
    os.utime(staging / "stray.tmp", (old, old))
    os.utime(staging / "link", (old, old), follow_symlinks=False)

    assert export.sweep_staging(env.output_dir, LATER) == 2
    assert target.exists()  # link removed, never followed


def test_export_pending_sweeps_stale_staging(env: Env) -> None:
    stale = env.output_dir / ".staging" / "stale.abc"
    stale.mkdir(parents=True)
    stamp = LATER.timestamp() - 2 * 3600
    os.utime(stale, (stamp, stamp))

    export_pending(env.conn, output_dir=env.output_dir, media_root=env.media_root, now=LATER)

    assert not stale.exists()


def test_db_allows_only_one_export_row_per_version(env: Env) -> None:
    clip_id = approved_clip(env)
    now = datetime(2027, 1, 15, tzinfo=UTC)
    clips.record_review(env.conn, clip_id, 1, clips.ReviewAction.EXPORT, now)
    clips.record_review(env.conn, clip_id, 1, clips.ReviewAction.EXPORT_FAILED, now)
    clips.record_review(env.conn, clip_id, 1, clips.ReviewAction.EXPORT_FAILED, now)

    with pytest.raises(sqlite3.IntegrityError):
        clips.record_review(env.conn, clip_id, 1, clips.ReviewAction.EXPORT, now)


def test_unique_index_conflict_is_treated_as_already_exported(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_id = approved_clip(env)
    calls = {"n": 0}
    real = export._export_row

    def blind(conn: sqlite3.Connection, cid: int, version: int):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:  # the in-transaction re-check misses a concurrent writer's row
            clips.record_review(conn, cid, version, clips.ReviewAction.EXPORT, LATER)
            return None
        return real(conn, cid, version)

    monkeypatch.setattr(export, "_export_row", blind)

    result = run(env, clip_id)

    assert result.status == "already_exported"
    assert env.status(clip_id) == "exported"
    assert [r["action"] for r in env.reviews(clip_id)].count("export") == 1


def test_bad_approval_date_falls_back_to_today(env: Env) -> None:
    clip_id = approved_clip(env)
    env.conn.execute(
        "UPDATE clip_reviews SET created_at = '../../etc' WHERE clip_id = ?", (clip_id,)
    )

    result = run(env, clip_id)

    assert result.files[0].parent == env.output_dir / "ready" / "2027-01-17"


def test_symlinked_day_dir_escaping_ready_is_refused(env: Env, tmp_path: Path) -> None:
    clip_id = approved_clip(env)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (env.output_dir / "ready").mkdir(parents=True)
    os.symlink(outside, env.output_dir / "ready" / "2027-01-15")

    result = run(env, clip_id)

    assert result.status == "failed"
    assert "escapes" in (result.error or "")
    assert list(outside.iterdir()) == []


def test_invalid_source_id_exports_without_source_url(env: Env) -> None:
    path = env.media_root / "x.mp4"
    path.write_bytes(VIDEO)
    clip_id = env.clip(env.job(item_id="bad id!"), video_path="x.mp4")
    clips.approve(env.conn, clip_id, APPROVED_AT)

    result = run(env, clip_id)

    assert result.status == "exported"
    assert json.loads(result.files[2].read_text())["source_url"] is None


def test_export_locks_are_per_clip_and_non_blocking() -> None:
    locks = export.ExportLocks()

    assert locks.try_acquire(1)
    assert not locks.try_acquire(1)
    assert locks.try_acquire(2)
    locks.release(1)
    assert locks.try_acquire(1)


def test_error_text_redacts_before_truncating_stderr() -> None:
    stderr = "password=" + "S" * 30 + " " + "y" * 190
    exc = subprocess.CalledProcessError(1, ["rclone"], stderr=stderr)

    text = export._error_text(exc)

    assert "SSSSS" not in text
    assert text.endswith("y)")


def test_error_text_caps_huge_messages() -> None:
    exc = subprocess.CalledProcessError(1, ["rclone"], stderr="-" * 1_000_000 + " tail")

    start = time.perf_counter()
    text = export._error_text(exc)

    assert time.perf_counter() - start < 0.5
    assert text.endswith(" tail)")
    assert len(text) <= 500
    huge = export._error_text(RuntimeError("_" * 1_000_000))
    assert len(huge) <= 500


def test_sweep_staging_ignores_symlinked_staging(env: Env, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old = elsewhere / "old.abc"
    old.mkdir()
    stamp = LATER.timestamp() - 2 * 3600
    os.utime(old, (stamp, stamp))
    env.output_dir.mkdir()
    os.symlink(elsewhere, env.output_dir / ".staging")

    assert export.sweep_staging(env.output_dir, LATER) == 0
    assert old.exists()


def test_error_text_includes_stderr_and_is_redacted() -> None:
    exc = subprocess.CalledProcessError(
        1, ["rclone"], stderr="failed https://x/?token=abc123 denied"
    )

    text = export._error_text(exc)

    assert "denied" in text
    assert "abc123" not in text


def test_run_subprocess_uses_argv_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> None:
        seen["args"], seen["kwargs"] = args, kwargs

    monkeypatch.setattr(export.subprocess, "run", fake_run)

    export.run_subprocess(["rclone", "copy", "--", "a", "b"])

    assert seen["args"] == ["rclone", "copy", "--", "a", "b"]
    assert seen["kwargs"]["check"] is True  # type: ignore[index]
    assert "shell" not in seen["kwargs"]  # type: ignore[operator]
