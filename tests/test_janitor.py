"""Janitor (CLP-38): review expiry, retention sweeps, disk budget, and the deletion guards."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.config import AppConfig, Settings
from clipper.db import connect, migrate
from clipper.janitor import GB, Janitor, media_bytes
from clipper.jobs.models import to_iso
from clipper.notify import NotifyError

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
SOURCE_FILES = ("audio_16k.flac", ".src.webm.part", ".info.tmp.json")
SECTION_FILES = ("source.mp4", ".section.json", "source.tmp.mp4")
RENDERED_FILES = ("v1.mp4", "v1.jpg", "v1.json")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Files outside the media root that a planted symlink points at."""
    path = tmp_path / "outside"
    (path / "clipdir").mkdir(parents=True)
    (path / "secret.flac").write_bytes(b"not media")
    (path / "clipdir" / "v1.mp4").write_bytes(b"not media either")
    return path


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(
    conn: sqlite3.Connection,
    root: Path,
    *,
    alerts: list[str] | None = None,
    usage: int | None = None,
    clock: Clock | None = None,
    **settings: object,
) -> Janitor:
    sink = alerts if alerts is not None else []
    return Janitor(
        conn=conn,
        config=AppConfig(settings=Settings(**settings)),  # type: ignore[arg-type]
        media_root=root,
        alert=sink.append,
        clock=clock or Clock(),
        disk_usage=media_bytes if usage is None else (lambda _root: usage),
    )


def add_job(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    status: str = "clips_ready",
    age: timedelta = 3 * DAY,
) -> int:
    stamp = to_iso(T0 - age)
    return conn.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, status, created_at,"
        " updated_at) VALUES ('youtube', ?, 'MrBeast', 'u', ?, ?, ?)",
        (item_id, status, stamp, stamp),
    ).lastrowid


def add_clip(
    conn: sqlite3.Connection,
    job_id: int,
    rank: int,
    status: str,
    *,
    age: timedelta = 3 * DAY,
    created: timedelta | None = None,
) -> int:
    clip_id = conn.execute(
        "INSERT INTO clips (job_id, rank, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, rank, status, to_iso(T0 - (created or age)), to_iso(T0 - age)),
    ).lastrowid
    conn.execute(
        "INSERT INTO clip_versions (clip_id, version, start_s, end_s, video_path, thumb_path,"
        " created_at) VALUES (?, 1, 0, 30, ?, ?, ?)",
        (clip_id, f"x/clips/{rank}/v1.mp4", f"x/clips/{rank}/v1.jpg", to_iso(T0)),
    )
    return clip_id


def job_dir(root: Path, item_id: str, ranks: tuple[int, ...] = (1,)) -> Path:
    """A job's media as the pipeline leaves it: source audio, artifacts, clip dirs."""
    path = root / f"youtube_{item_id}"
    path.mkdir()
    for name in (*SOURCE_FILES, "transcript.json", "meta.json"):
        (path / name).write_bytes(b"x" * 100)
    for rank in ranks:
        clip = path / "clips" / str(rank)
        clip.mkdir(parents=True)
        for name in (*SECTION_FILES, *RENDERED_FILES):
            (clip / name).write_bytes(b"x" * 100)
    return path


def names(path: Path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*")}


def status(conn: sqlite3.Connection, clip_id: int) -> str:
    return conn.execute("SELECT status FROM clips WHERE id = ?", (clip_id,)).fetchone()[0]


# --- review expiry ----------------------------------------------------------------------


def test_reviewable_clips_past_review_expiry_are_archived(
    conn: sqlite3.Connection, root: Path
) -> None:
    job = add_job(conn, "aaaaaaaaaaa")
    old_pending = add_clip(conn, job, 1, "pending_review", age=8 * DAY)
    young_pending = add_clip(conn, job, 2, "pending_review", age=6 * DAY)
    old_attention = add_clip(conn, job, 3, "needs_attention", age=8 * DAY)
    old_approved = add_clip(conn, job, 4, "approved", age=30 * DAY)

    report = make(conn, root, review_expiry_days=7).run()

    assert report.archived == 2
    assert status(conn, old_pending) == "archived"
    assert status(conn, old_attention) == "archived"
    assert status(conn, young_pending) == "pending_review"
    assert status(conn, old_approved) == "approved"
    rows = conn.execute(
        "SELECT clip_id, version, action, payload, created_at FROM clip_reviews ORDER BY clip_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (old_pending, 1, "archive", '{"reason": "review_expired"}', to_iso(T0)),
        (old_attention, 1, "archive", '{"reason": "review_expired"}', to_iso(T0)),
    ]
    (updated,) = conn.execute("SELECT updated_at FROM clips WHERE id = ?", (old_pending,))
    assert updated[0] == to_iso(T0)  # the rejected/archived retention counts from here


def test_review_expiry_counts_from_creation_not_last_update(
    conn: sqlite3.Connection, root: Path
) -> None:
    clip = add_clip(conn, add_job(conn, "aaaaaaaaaaa"), 1, "pending_review", created=8 * DAY)
    conn.execute("UPDATE clips SET updated_at = ? WHERE id = ?", (to_iso(T0), clip))

    make(conn, root, review_expiry_days=7).run()

    assert status(conn, clip) == "archived"


# --- source media retention -------------------------------------------------------------


def test_clips_ready_source_media_past_retention_is_deleted(
    conn: sqlite3.Connection, root: Path
) -> None:
    add_job(conn, "aaaaaaaaaaa", age=49 * HOUR)
    old = job_dir(root, "aaaaaaaaaaa", ranks=(1, 2))
    add_job(conn, "bbbbbbbbbbb", age=47 * HOUR)
    young = job_dir(root, "bbbbbbbbbbb")
    before_young = names(young)

    report = make(conn, root, media_retention_hours=48).run()

    assert names(old) == {
        "transcript.json",
        "meta.json",
        "clips",
        *(f"clips/{r}" for r in (1, 2)),
        *(f"clips/{r}/{n}" for r in (1, 2) for n in RENDERED_FILES),
    }
    assert names(young) == before_young
    assert report.freed_bytes == 100 * (len(SOURCE_FILES) + 2 * len(SECTION_FILES))


@pytest.mark.parametrize("job_status", ["rendering", "transcribing", "failed", "skipped"])
def test_jobs_not_clips_ready_keep_their_source_media(
    conn: sqlite3.Connection, root: Path, job_status: str
) -> None:
    add_job(conn, "aaaaaaaaaaa", status=job_status, age=30 * DAY)
    path = job_dir(root, "aaaaaaaaaaa")
    before = names(path)

    make(conn, root).run()

    assert names(path) == before


# --- rejected / archived retention ------------------------------------------------------


def test_rejected_and_archived_clips_are_deleted_after_their_retention(
    conn: sqlite3.Connection, root: Path
) -> None:
    job = add_job(conn, "aaaaaaaaaaa", status="clips_ready", age=1 * HOUR)
    path = job_dir(root, "aaaaaaaaaaa", ranks=(1, 2, 3))
    old_rejected = add_clip(conn, job, 1, "rejected", age=15 * DAY)
    old_archived = add_clip(conn, job, 2, "archived", age=15 * DAY)
    young_rejected = add_clip(conn, job, 3, "rejected", age=13 * DAY)

    make(conn, root, rejected_retention_days=14).run()

    assert not (path / "clips" / "1").exists()
    assert not (path / "clips" / "2").exists()
    assert {n for n in names(path / "clips" / "3")} == {*SECTION_FILES, *RENDERED_FILES}
    # Rows (and the review history the feedback loop reads) stay.
    assert [status(conn, c) for c in (old_rejected, old_archived, young_rejected)] == [
        "rejected",
        "archived",
        "rejected",
    ]


# --- protected clips --------------------------------------------------------------------


@pytest.mark.parametrize("keep", ["approved", "exported", "changes_requested", "re_rendering"])
def test_kept_clips_survive_every_sweep_even_far_over_the_disk_budget(
    conn: sqlite3.Connection, root: Path, keep: str
) -> None:
    job = add_job(conn, "aaaaaaaaaaa", age=90 * DAY)
    path = job_dir(root, "aaaaaaaaaaa", ranks=(1, 2))
    add_clip(conn, job, 1, keep, age=90 * DAY)
    add_clip(conn, job, 2, "rejected", age=90 * DAY)

    make(conn, root, usage=100 * GB, disk_budget_gb=1).run()

    assert names(path / "clips" / "1") == {*SECTION_FILES, *RENDERED_FILES}
    assert not (path / "clips" / "2").exists()
    assert not (path / "audio_16k.flac").exists()


def test_files_a_post_uses_are_kept(conn: sqlite3.Connection, root: Path) -> None:
    job = add_job(conn, "aaaaaaaaaaa", age=90 * DAY)
    path = job_dir(root, "aaaaaaaaaaa")
    clip = add_clip(conn, job, 1, "rejected", age=90 * DAY)
    video = path / "clips" / "1" / "v1.mp4"
    conn.execute(
        "INSERT INTO posts (clip_id, clip_version, account_id, platform, scheduled_at,"
        " idempotency_key, video_path, created_at, updated_at)"
        " VALUES (?, 1, 'acct', 'tiktok', ?, 'k1', ?, ?, ?)",
        (clip, to_iso(T0), str(video), to_iso(T0), to_iso(T0)),
    )

    make(conn, root).run()

    assert video.is_file()


# --- symlinks and the media root --------------------------------------------------------


def test_planted_symlinks_are_never_followed_or_deleted(
    conn: sqlite3.Connection, root: Path, outside: Path
) -> None:
    job = add_job(conn, "aaaaaaaaaaa", age=90 * DAY)
    path = job_dir(root, "aaaaaaaaaaa", ranks=(1,))
    (path / "audio_16k.flac").unlink()
    (path / "audio_16k.flac").symlink_to(outside / "secret.flac")
    (path / "clips" / "2").symlink_to(outside / "clipdir", target_is_directory=True)
    add_clip(conn, job, 1, "rejected", age=90 * DAY)
    add_clip(conn, job, 2, "rejected", age=90 * DAY)
    (path / "clips" / "1" / "v1.mp4").unlink()
    (path / "clips" / "1" / "v1.mp4").symlink_to(outside / "clipdir" / "v1.mp4")
    # A whole job dir that is a link to somewhere else.
    add_job(conn, "bbbbbbbbbbb", age=90 * DAY)
    (root / "youtube_bbbbbbbbbbb").symlink_to(outside, target_is_directory=True)
    (outside / "audio_16k.flac").write_bytes(b"outside")

    make(conn, root, usage=100 * GB, disk_budget_gb=1).run()

    assert (outside / "secret.flac").read_bytes() == b"not media"
    assert (outside / "clipdir" / "v1.mp4").read_bytes() == b"not media either"
    assert (outside / "audio_16k.flac").read_bytes() == b"outside"
    assert (path / "audio_16k.flac").is_symlink()
    assert (path / "clips" / "2").is_symlink()
    assert (path / "clips" / "1" / "v1.mp4").is_symlink()  # the clip dir is left, not followed
    assert not (path / "clips" / "1" / "v1.jpg").exists()  # its real files still go
    assert (root / "youtube_bbbbbbbbbbb").is_symlink()


def test_a_symlinked_clips_dir_is_not_entered(
    conn: sqlite3.Connection, root: Path, outside: Path
) -> None:
    add_job(conn, "aaaaaaaaaaa", age=90 * DAY)
    path = root / "youtube_aaaaaaaaaaa"
    path.mkdir()
    (outside / "clipdir" / "source.mp4").write_bytes(b"outside section")
    (outside / "1").mkdir()
    (outside / "1" / "source.mp4").write_bytes(b"outside section")
    (path / "clips").symlink_to(outside, target_is_directory=True)

    make(conn, root).run()

    assert (outside / "1" / "source.mp4").read_bytes() == b"outside section"


def test_a_symlinked_media_root_is_resolved_and_still_cleaned(
    conn: sqlite3.Connection, root: Path, tmp_path: Path
) -> None:
    add_job(conn, "aaaaaaaaaaa", age=90 * DAY)
    path = job_dir(root, "aaaaaaaaaaa")
    link = tmp_path / "media-link"
    link.symlink_to(root, target_is_directory=True)

    make(conn, link).run()

    assert not (path / "audio_16k.flac").exists()


def test_a_missing_media_root_still_expires_reviews(conn: sqlite3.Connection, root: Path) -> None:
    clip = add_clip(conn, add_job(conn, "aaaaaaaaaaa"), 1, "pending_review", age=30 * DAY)

    report = make(conn, root / "not-yet").run()

    assert status(conn, clip) == "archived"
    assert report.errors == []


def test_media_bytes_counts_files_and_skips_symlinks(root: Path, outside: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "f").write_bytes(b"x" * 10)
    (root / "g").write_bytes(b"x" * 5)
    (root / "a" / "link").symlink_to(outside / "secret.flac")
    (root / "dir-link").symlink_to(outside, target_is_directory=True)

    assert media_bytes(root) == 15  # regular files only; links are never followed
    assert media_bytes(root / "missing") == 0


# --- disk budget ------------------------------------------------------------------------


def test_over_budget_deletes_the_oldest_source_media_first_until_under(
    conn: sqlite3.Connection, root: Path
) -> None:
    add_job(conn, "aaaaaaaaaaa", age=10 * HOUR)
    older = job_dir(root, "aaaaaaaaaaa")
    add_job(conn, "bbbbbbbbbbb", age=5 * HOUR)
    newer = job_dir(root, "bbbbbbbbbbb")
    before = names(newer)

    report = make(conn, root, usage=GB + 100, disk_budget_gb=1).run()

    assert not (older / "audio_16k.flac").exists()
    assert not (older / "clips" / "1" / "source.mp4").exists()
    assert (older / "clips" / "1" / "v1.mp4").is_file()
    assert names(newer) == before
    assert report.freed_bytes == 100 * (len(SOURCE_FILES) + len(SECTION_FILES))


def test_disk_alert_fires_once_per_crossing_of_80_percent(
    conn: sqlite3.Connection, root: Path
) -> None:
    alerts: list[str] = []
    clock = Clock()
    usage = [int(GB * f) for f in (0.79, 0.81, 0.9, 0.5, 0.85)]
    janitor = make(conn, root, alerts=alerts, clock=clock, disk_budget_gb=1)
    janitor.disk_usage = lambda _root: usage.pop(0)

    for _ in range(5):
        janitor.run()
        clock.now += HOUR

    assert len(alerts) == 2
    assert "81%" in alerts[0] and "of the 1 GB disk budget" in alerts[0]
    assert "85%" in alerts[1]


def test_a_failed_disk_alert_is_sent_again_next_run(conn: sqlite3.Connection, root: Path) -> None:
    sent: list[str] = []

    def flaky(text: str) -> None:
        if not sent and not getattr(flaky, "failed", False):
            flaky.failed = True  # type: ignore[attr-defined]
            raise NotifyError("discord webhook returned 500")
        sent.append(text)

    janitor = make(conn, root, usage=int(GB * 0.9), disk_budget_gb=1)
    janitor.alert = flaky
    janitor.run()
    janitor.run()
    janitor.run()

    assert len(sent) == 1


# --- schedule and isolation -------------------------------------------------------------


def test_tick_runs_at_most_hourly(conn: sqlite3.Connection, root: Path) -> None:
    clock = Clock()
    janitor = make(conn, root, clock=clock)

    assert janitor.tick() is not None
    clock.now += 59 * timedelta(minutes=1)
    assert janitor.tick() is None
    clock.now += timedelta(minutes=1)
    assert janitor.tick() is not None


def test_a_failing_sweep_is_reported_and_the_others_still_run(
    conn: sqlite3.Connection, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = add_clip(conn, add_job(conn, "aaaaaaaaaaa", age=90 * DAY), 1, "pending_review")
    conn.execute("UPDATE clips SET created_at = ?", (to_iso(T0 - 30 * DAY),))
    path = job_dir(root, "aaaaaaaaaaa", ranks=())
    janitor = make(conn, root)

    def broken(now: datetime, report: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(janitor, "prune_sources", broken)
    report = janitor.run()

    assert report.errors == ["prune_sources: OperationalError: database is locked"]
    assert status(conn, clip) == "archived"
    assert (path / "audio_16k.flac").is_file()


def test_tick_never_raises(conn: sqlite3.Connection, root: Path, monkeypatch) -> None:
    janitor = make(conn, root)
    monkeypatch.setattr(janitor, "run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert janitor.tick() is None
