"""Shared pipeline helpers: spend reservation, stage deadline, small utilities."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper.db import connect, migrate
from clipper.jobs import Deferred, Job, JobStatus, SkipJob
from clipper.jobs.stages import PermanentError
from clipper.pipeline import common, ytdlp
from clipper.pipeline.common import (
    Deadline,
    StageTimeout,
    first_seen,
    next_utc_midnight,
    reserve_source_hours,
    reserve_spend,
    spend_today,
    strip_control,
)
from clipper.urls import SourceRef

T0 = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    c.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES ('youtube', 'dQw4w9WgXcQ', 'x', 'u', ?, ?)",
        ("2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z"),
    )
    yield c
    c.close()


def _reserve(
    conn: sqlite3.Connection,
    estimate: float,
    cap: float = 1.0,
    job_id: int = 1,
    now: datetime = T0,
):
    return reserve_spend(
        conn,
        kind="llm",
        label="LLM",
        provider="anthropic",
        job_id=job_id,
        estimate_usd=estimate,
        cap_usd=cap,
        now=now,
    )


def _rows(conn: sqlite3.Connection) -> list[tuple[float, int | None]]:
    return [tuple(r) for r in conn.execute("SELECT usd, input_tokens FROM spend ORDER BY id")]


def test_reservation_is_visible_while_in_flight_then_settled(conn: sqlite3.Connection) -> None:
    with _reserve(conn, 0.4) as charge:
        assert spend_today(conn, "llm", T0) == pytest.approx(0.4)
        charge.usd, charge.input_tokens, charge.output_tokens = 0.05, 100, 10
    assert _rows(conn) == [(0.05, 100)]


def test_second_reservation_over_cap_is_deferred_to_utc_midnight(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, created_at, updated_at)"
        " SELECT platform, 'other00000a', creator, source_url, created_at, updated_at"
        " FROM jobs WHERE id = 1"
    )
    held = _reserve(conn, 0.6)  # job 1, in flight
    held.__enter__()
    with (
        pytest.raises(Deferred, match="daily LLM budget reached") as exc,
        _reserve(conn, 0.6, job_id=2),
    ):
        pass
    held.__exit__(None, None, None)
    assert exc.value.until == datetime(2026, 10, 22, tzinfo=UTC)


def test_estimate_above_cap_is_skipped(conn: sqlite3.Connection) -> None:
    with pytest.raises(SkipJob, match="exceeds the daily cap"), _reserve(conn, 2.0):
        pass
    assert _rows(conn) == []


def test_nothing_charged_deletes_the_reservation(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="network"), _reserve(conn, 0.4):
        raise RuntimeError("network down")
    assert _rows(conn) == []


def test_partial_charge_is_kept_when_the_call_fails(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), _reserve(conn, 0.4) as charge:
        charge.usd, charge.audio_seconds = 0.1, 600.0
        raise RuntimeError("later chunk failed")
    (row,) = conn.execute("SELECT usd, audio_seconds FROM spend").fetchall()
    assert tuple(row) == (0.1, 600.0)


def test_settle_db_error_does_not_mask_the_original_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(common, "_settle", broken)
    with pytest.raises(ValueError, match="the real problem"), _reserve(conn, 0.4):
        raise ValueError("the real problem")
    assert "could not settle spend row" in caplog.text


def test_deadline_shrinks_and_then_expires() -> None:
    now = [100.0]
    deadline = Deadline(budget_s=60.0, clock=lambda: now[0])
    assert deadline.timeout(600.0) == 60.0
    assert deadline.timeout(10.0) == 10.0
    now[0] = 150.0
    assert deadline.timeout(600.0) == pytest.approx(10.0)
    now[0] = 159.5
    with pytest.raises(StageTimeout, match="1 min budget"):
        deadline.timeout(5.0)


def test_next_utc_midnight_and_strip_control() -> None:
    assert next_utc_midnight(T0) == datetime(2026, 10, 22, tzinfo=UTC)
    assert strip_control("a\nb\x00\tc\x7f  d") == "a b c d"
    # Format characters (bidi, zero-width, BOM, tags) and blank fillers go.
    spoof = "\u202eab\u200b\u200e\u200f\u2060\u2066c\u2069\ufeff\U000e0041"
    assert strip_control(spoof) == "abc"
    assert strip_control("a\u061cb\u180ec\ufff9d") == "abcd"
    assert strip_control("\u3164\u115f\u1160\u034f\uffa0") == ""
    # ZWJ (compound emoji) and ZWNJ (Persian/Indic spelling) stay.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert strip_control(family) == family
    assert strip_control("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645") == (
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    )


def test_leaked_reservation_of_this_job_is_dropped(conn: sqlite3.Connection) -> None:
    leaked = _reserve(conn, 0.7, now=T0 - timedelta(hours=3))
    leaked.__enter__()  # worker killed 3 h ago (past the 2 h lease): never settled
    conn.execute(
        "INSERT INTO spend (job_id, provider, kind, usd, input_tokens, created_at)"
        " VALUES (1, 'anthropic', 'llm', 0.2, 50, '2026-10-21T23:00:00Z')"
    )  # a settled row stays
    conn.execute(
        "INSERT INTO spend (job_id, provider, kind, usd, created_at)"
        " VALUES (NULL, 'groq', 'llm', 0.05, '2026-10-21T23:00:00Z')"
    )  # another job's row stays
    with _reserve(conn, 0.7) as charge:  # would not fit next to the leaked 0.7
        charge.usd, charge.input_tokens = 0.1, 10
    assert sorted(_rows(conn)) == [(0.05, None), (0.1, 10), (0.2, 50)]


def test_fresh_unsettled_row_is_kept(conn: sqlite3.Connection) -> None:
    in_flight = _reserve(conn, 0.3, now=T0 - timedelta(minutes=30))
    in_flight.__enter__()  # within the lease: may still be a live call, so it counts
    with _reserve(conn, 0.3) as charge:
        charge.usd, charge.input_tokens = 0.1, 10
    assert sorted(_rows(conn)) == [(0.1, 10), (0.3, None)]
    with pytest.raises(Deferred), _reserve(conn, 0.65):
        pass


def test_first_seen_records_reads_and_clamps(tmp_path: Path) -> None:
    marker = tmp_path / "m"
    assert first_seen(marker, T0) == T0
    assert first_seen(marker, T0 + timedelta(hours=5)) == T0
    marker.write_text((T0 + timedelta(days=1)).isoformat())  # future-dated (clock skew)
    assert first_seen(marker, T0) == T0
    assert first_seen(marker, T0 + timedelta(hours=1)) == T0


def test_first_seen_restarts_a_stale_marker(tmp_path: Path) -> None:
    marker = tmp_path / "m"
    first_seen(marker, T0)
    later = T0 + timedelta(days=8)
    assert first_seen(marker, later, max_age=timedelta(days=7)) == later
    assert first_seen(marker, later + timedelta(hours=1), max_age=timedelta(days=7)) == later


def test_first_seen_write_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    marker = tmp_path / ".not_ready_since"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    marker.with_name(f"{marker.name}.tmp").symlink_to(victim)

    assert first_seen(marker, T0) == T0

    assert victim.read_text() == "keep me"
    assert not marker.is_symlink() and marker.read_text() == T0.isoformat()


def test_episode_since_keeps_an_active_episode_and_restarts_a_stale_one(tmp_path: Path) -> None:
    marker, cap = tmp_path / "m", timedelta(hours=48)
    assert common.episode_since(marker, T0, cap) == T0
    assert common.episode_since(marker, T0 + timedelta(hours=40), cap) == T0
    assert common.episode_since(marker, T0 + timedelta(hours=80), cap) == T0  # last at 40 h
    later = T0 + timedelta(hours=129)  # 49 h after the last event
    assert common.episode_since(marker, later, cap) == later


def _format_error_at(marker: Path, when: datetime) -> Exception:
    """Run `deferring_no_format` over a format error at `when`; return what it raised."""
    error = ytdlp.Unavailable("Requested format is not available", ytdlp.NO_FORMAT)
    try:
        with common.deferring_no_format(marker, lambda: when):
            raise error
    except Exception as exc:  # noqa: BLE001 - the test inspects whatever came out
        return exc
    raise AssertionError("deferring_no_format swallowed the error")


def test_format_error_picked_up_late_still_fails_at_the_cap(tmp_path: Path) -> None:
    # Deferred every 30 min for almost 48 h, then the worker reaches the job only 2.5 h
    # after its last retry_at (busy, or down): the episode is the same one, so it fails
    # now instead of starting a fresh 48 h (about 96 h in all).
    marker = tmp_path / ".no_format_since"
    when = T0
    while when < T0 + common.NO_FORMAT_MAX_AGE - timedelta(hours=1):
        assert isinstance(_format_error_at(marker, when), common.NoUsableFormat)
        when += common.NO_FORMAT_RETRY

    late = when + common.NO_FORMAT_RETRY + timedelta(hours=2)
    assert late - T0 > common.NO_FORMAT_MAX_AGE + common.NO_FORMAT_RETRY
    outcome = _format_error_at(marker, late)

    assert type(outcome) is PermanentError and "giving up" in str(outcome)


def test_format_error_idle_for_the_cap_starts_a_new_episode(tmp_path: Path) -> None:
    marker = tmp_path / ".no_format_since"
    _format_error_at(marker, T0)
    _format_error_at(marker, T0 + timedelta(hours=1))  # last deferral of that episode

    later = T0 + timedelta(hours=1) + common.NO_FORMAT_MAX_AGE + timedelta(minutes=1)
    assert isinstance(_format_error_at(marker, later), common.NoUsableFormat)
    assert isinstance(_format_error_at(marker, later + timedelta(hours=47)), common.NoUsableFormat)
    outcome = _format_error_at(marker, later + common.NO_FORMAT_MAX_AGE + timedelta(minutes=1))
    assert type(outcome) is PermanentError


def test_format_error_marker_from_before_the_upgrade_still_counts(tmp_path: Path) -> None:
    # A marker holding only the first-seen time (written before CLP-85) keeps its start.
    marker = tmp_path / ".no_format_since"
    marker.write_text(T0.isoformat())

    assert isinstance(_format_error_at(marker, T0 + timedelta(hours=47)), common.NoUsableFormat)
    assert type(_format_error_at(marker, T0 + timedelta(hours=49))) is PermanentError


def test_format_error_marker_from_before_the_upgrade_fails_just_past_the_cap(
    tmp_path: Path,
) -> None:
    # A one-line marker has no last-error time. Up to one retry past the cap, the episode
    # may have been retried all along: it fails at the cap instead of starting over.
    marker = tmp_path / ".no_format_since"
    marker.write_text(T0.isoformat())

    outcome = _format_error_at(marker, T0 + common.NO_FORMAT_MAX_AGE + timedelta(minutes=15))

    assert type(outcome) is PermanentError and "giving up" in str(outcome)


def test_format_error_marker_from_before_the_upgrade_goes_stale_after_one_retry(
    tmp_path: Path,
) -> None:
    marker = tmp_path / ".no_format_since"
    marker.write_text(T0.isoformat())
    late = T0 + common.NO_FORMAT_MAX_AGE + common.NO_FORMAT_RETRY + timedelta(minutes=1)

    assert isinstance(_format_error_at(marker, late), common.NoUsableFormat)
    assert marker.read_text().split() == [late.isoformat(), late.isoformat()]


def test_format_error_marker_read_is_bounded(tmp_path: Path) -> None:
    # Only the start of the marker is read: whatever follows it is never looked at.
    marker = tmp_path / ".no_format_since"
    stamps = f"{T0.isoformat()}\n{(T0 + timedelta(hours=1)).isoformat()}\n"
    marker.write_text(stamps + " " * 300 + "not-a-time" * 100_000)

    _format_error_at(marker, T0 + timedelta(hours=2))

    assert marker.read_text().split() == [
        T0.isoformat(),
        (T0 + timedelta(hours=2)).isoformat(),
    ]


def test_format_error_marker_write_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    marker = tmp_path / ".no_format_since"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    marker.with_name(f"{marker.name}.tmp").symlink_to(victim)

    assert isinstance(_format_error_at(marker, T0), common.NoUsableFormat)

    assert victim.read_text() == "keep me"
    assert not marker.is_symlink() and marker.read_text().split()[0] == T0.isoformat()


def test_other_unavailable_errors_pass_through_untouched(tmp_path: Path) -> None:
    marker = tmp_path / ".no_format_since"
    error = ytdlp.Unavailable("Private video", "private")
    with pytest.raises(ytdlp.Unavailable), common.deferring_no_format(marker, lambda: T0):
        raise error
    assert not marker.exists()


def test_stderr_tail_redacts_before_cutting() -> None:
    noise = "x" * 100_000 + "\n"
    tail = common.stderr_tail(noise + "fetch https://h/?sig=SECRET1 failed\nmore\nERROR: end\n")
    assert "SECRET1" not in tail and tail.endswith("ERROR: end")
    assert len(tail) <= common.STDERR_TAIL_CHARS


# --- source hours per creator per UTC day (CLP-38) -----------------------------------------


def _job(conn: sqlite3.Connection, item_id: str, creator: str = "x") -> Job:
    job_id = conn.execute(
        "INSERT INTO jobs (platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES ('youtube', ?, ?, 'u', ?, ?)",
        (item_id, creator, "2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z"),
    ).lastrowid
    return Job(
        id=job_id,
        ref=SourceRef("youtube", item_id),
        creator=creator,
        source_url="u",
        status=JobStatus.FETCHING_AUDIO,
        is_stream=False,
        attempts=1,
    )


def _source_hours(conn: sqlite3.Connection, job: Job, hours: float, now: datetime = T0) -> None:
    reserve_source_hours(conn, job, hours * 3600, cap_hours=10.0, now=now)


def test_source_hours_over_the_cap_defer_to_utc_midnight_then_resume(
    conn: sqlite3.Connection,
) -> None:
    _source_hours(conn, _job(conn, "aaaaaaaaaaa"), 6)
    late = _job(conn, "bbbbbbbbbbb")
    with pytest.raises(Deferred, match="daily source hours reached") as exc:
        _source_hours(conn, late, 5)
    assert exc.value.until == datetime(2026, 10, 22, tzinfo=UTC)
    # Nothing was recorded for the deferred job; the next UTC day it fits again.
    _source_hours(conn, late, 5, now=exc.value.until)
    rows = conn.execute("SELECT job_id, kind, usd, audio_seconds FROM spend ORDER BY id")
    assert [tuple(r) for r in rows] == [
        (2, "source", 0.0, 6 * 3600.0),
        (3, "source", 0.0, 5 * 3600.0),
    ]


def test_source_hours_are_counted_once_per_job(conn: sqlite3.Connection) -> None:
    job = _job(conn, "aaaaaaaaaaa")
    _source_hours(conn, job, 6)
    _source_hours(conn, job, 6)  # a retried stage (crash, not-ready) is already admitted
    (count,) = conn.execute("SELECT COUNT(*) FROM spend WHERE kind = 'source'").fetchone()
    assert count == 1


def test_source_hours_are_per_creator(conn: sqlite3.Connection) -> None:
    _source_hours(conn, _job(conn, "aaaaaaaaaaa", creator="one"), 9)
    _source_hours(conn, _job(conn, "bbbbbbbbbbb", creator="two"), 9)
    with pytest.raises(Deferred):
        _source_hours(conn, _job(conn, "ccccccccccc", creator="one"), 2)


def test_source_hours_ignore_money_spend_and_earlier_days(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO spend (job_id, provider, kind, usd, audio_seconds, created_at)"
        " VALUES (1, 'groq', 'transcribe', 0.5, 36000, '2026-10-21T10:00:00Z'),"
        " (1, 'youtube', 'source', 0, 36000, '2026-10-20T23:59:59Z')"
    )
    _source_hours(conn, _job(conn, "aaaaaaaaaaa"), 9)


def test_source_longer_than_the_whole_cap_is_skipped(conn: sqlite3.Connection) -> None:
    with pytest.raises(SkipJob, match="exceeds the daily max_source_hours_per_day"):
        _source_hours(conn, _job(conn, "aaaaaaaaaaa"), 11)
    assert _rows(conn) == []
