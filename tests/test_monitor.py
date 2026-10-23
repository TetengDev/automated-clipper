"""Monitor loop (CLP-24): poll cycles, first-run modes, exactly-once enqueue, isolation.

The source is a fake (feeds and metadata per test) and the clock is fake; no network.
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from clipper import cli
from clipper.cli import app
from clipper.config import AppConfig, Secrets, load_config
from clipper.db import connect, migrate
from clipper.jobs.queue import enqueue
from clipper.monitor import (
    COMPONENT,
    MAX_METADATA_ATTEMPTS,
    FirstRun,
    SeenStatus,
    YouTubeMonitor,
)
from clipper.sources.youtube import (
    Metadata,
    SourceCancelled,
    SourceError,
    Upload,
    VideoMeta,
    YouTubeSource,
    YtDlpProvider,
)
from clipper.urls import SourceRef

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
BEAST = "UCX6OQ3DkcsbYNE6H8uQQuVA"
SPEED = "UCWsDFcIhY2DBi3GB5uykGXA"
RAFFY = "UCxhygwqQ1ZMoBGQM2yEcNug"
V1, V2, V3, V4, V5 = (c * 11 for c in "abcde")

CONFIG = f"""\
settings:
  first_run_mode: baseline
  lookback_days: 7
  min_duration_seconds: 300
  poll_interval_youtube_minutes: 10
creators:
  - name: MrBeast
    region: global
    youtube:
      handle: "@MrBeast"
      channel_id: {BEAST}
  - name: IShowSpeed
    region: global
    youtube:
      handle: "@IShowSpeed"
      channel_id: {SPEED}
  - name: Raffy Tulfo in Action
    region: ph
    youtube:
      handle: "@RaffyTulfoInAction"
      channel_id: {RAFFY}
    overrides:
      min_duration_seconds: 600
  - name: Unresolved
    region: global
    youtube:
      handle: "@Unresolved"
  - name: Twitch Only
    region: global
    twitch:
      login: twitchonly
"""


def video(vid: str, *, duration: int | None = 1200, status: str = "not_live", age_days: float = 0):
    return VideoMeta(vid, f"Title {vid}", duration, status, T0 - timedelta(days=age_days))


class FakeSource:
    def __init__(self) -> None:
        self.feeds: dict[str, list[Upload] | Exception] = {}
        self.meta: dict[str, VideoMeta | str] = {}  # "unavailable" or an error message
        self.metadata_calls: list[list[str]] = []
        self.feed_calls: list[str] = []
        self.quota_units = 0
        self.units_per_metadata_call = 0
        self.on_feed: Callable[[str], None] | None = None

    def recent_uploads(self, channel_id: str) -> list[Upload]:
        self.feed_calls.append(channel_id)
        if self.on_feed:
            self.on_feed(channel_id)
        feed = self.feeds.get(channel_id, [])
        if isinstance(feed, Exception):
            raise feed
        return feed

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool] = lambda: False):
        self.metadata_calls.append(list(ids))
        self.quota_units += self.units_per_metadata_call
        result = Metadata()
        for vid in ids:
            if should_stop():
                break
            answer = self.meta[vid]
            if answer == "unavailable":
                result.unavailable.add(vid)
            elif isinstance(answer, str) and answer.startswith("blocked"):
                result.errors[vid] = answer
                result.blocked.add(vid)
                break
            elif isinstance(answer, str) and answer.startswith("format"):
                result.errors[vid] = answer
                result.format_errors.add(vid)
                break
            elif isinstance(answer, str):
                result.errors[vid] = answer
            else:
                result.videos[vid] = answer
        return result


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    path = tmp_path / "creators.yaml"
    path.write_text(CONFIG)
    return load_config(path)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make(conn, cfg, source, clock, **kwargs) -> YouTubeMonitor:
    return YouTubeMonitor(conn, cfg, source, clock=clock, **kwargs)


def jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def seen(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT item_id, status FROM seen_items").fetchall())


def runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


# --- first run, steady state, exactly once -------------------------------------------------


def test_baseline_then_new_upload_enqueued_exactly_once_over_three_cycles(
    conn, cfg, source, clock
) -> None:
    source.feeds[BEAST] = [Upload(V1, T0 - timedelta(days=1)), Upload(V2, T0 - timedelta(days=30))]
    monitor = make(conn, cfg, source, clock)
    creators = [cfg.creator("MrBeast")]

    first = monitor.run_cycle(creators)  # baseline: everything current is marked seen
    clock.now += timedelta(minutes=10)
    second = monitor.run_cycle(creators)  # nothing new
    clock.now += timedelta(minutes=10)
    source.feeds[BEAST] = [Upload(V3, clock.now), *source.feeds[BEAST]]
    source.meta[V3] = video(V3)
    third = monitor.run_cycle(creators)  # one new upload
    clock.now += timedelta(minutes=10)
    fourth = monitor.run_cycle(creators)  # same feed again

    assert (first.items_new, second.items_new, third.items_new, fourth.items_new) == (0, 0, 1, 0)
    assert seen(conn) == {V1: "baseline", V2: "baseline", V3: "enqueued"}
    assert source.metadata_calls == [[V3]]  # baseline and quiet polls fetch no metadata
    [job] = jobs(conn)
    assert (job["item_id"], job["status"], job["creator"]) == (V3, "detected", "MrBeast")
    assert job["source_url"] == f"https://www.youtube.com/watch?v={V3}"
    assert (job["title"], job["duration_seconds"], job["is_stream"]) == (f"Title {V3}", 1200, 0)
    assert [p.enqueued for p in third.polls] == [[(job["id"], V3, f"Title {V3}")]]


def test_quiet_poll_uses_zero_quota_and_zero_metadata_calls(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.units_per_metadata_call = 1
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])
    source.metadata_calls.clear()

    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert report.quota_units == 0
    assert source.metadata_calls == []
    assert runs(conn)[-1]["quota_units"] == 0


def test_restart_does_not_enqueue_again(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = video(V1)
    make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT).run_cycle([cfg.creator("MrBeast")])

    again = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    report = again.run_cycle([cfg.creator("MrBeast")])

    assert report.items_new == 0
    assert len(jobs(conn)) == 1
    assert source.metadata_calls == [[V1]]


def test_crash_while_enqueueing_leaves_the_item_unseen(
    conn, cfg, source, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipper import monitor as mon

    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = video(V1)
    real_insert = mon.insert_job

    def crash(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(mon, "insert_job", crash)
    monitor = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    crashed = monitor.run_cycle([cfg.creator("MrBeast")])

    assert seen(conn) == {}  # seen_items insert rolled back with the failed enqueue
    assert jobs(conn) == []
    assert "disk I/O error" in crashed.errors[0]["error"]

    monkeypatch.setattr(mon, "insert_job", real_insert)
    retried = monitor.run_cycle([cfg.creator("MrBeast")])

    assert retried.items_new == 1
    assert len(jobs(conn)) == 1


def test_item_already_queued_by_hand_is_not_duplicated(conn, cfg, source, clock) -> None:
    enqueue(conn, SourceRef("youtube", V1), T0, creator="manual")
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = video(V1)

    report = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT).run_cycle(
        [cfg.creator("MrBeast")]
    )

    assert report.items_new == 0
    assert [j["creator"] for j in jobs(conn)] == ["manual"]
    assert seen(conn) == {V1: "enqueued"}


def test_emit_first_run_enqueues_recent_items_and_applies_filters(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [
        Upload(V1, T0 - timedelta(days=1)),
        Upload(V2, T0 - timedelta(days=2)),  # a Short
        Upload(V3, T0 - timedelta(hours=1)),  # live now
        Upload(V4, T0 - timedelta(days=30)),  # older than lookback_days: no metadata call
        Upload(V5, None),  # no feed date (fallback listing): metadata decides
    ]
    source.meta.update(
        {
            V1: video(V1, age_days=1),
            V2: video(V2, duration=45, age_days=2),
            V3: video(V3, duration=None, status="is_live"),
            V5: video(V5, age_days=20),
        }
    )

    report = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT).run_cycle(
        [cfg.creator("MrBeast")]
    )

    assert report.items_new == 1
    assert [j["item_id"] for j in jobs(conn)] == [V1]
    assert seen(conn) == {
        V1: "enqueued",
        V2: "skipped_short",
        V3: "waiting_vod",
        V4: "baseline",
        V5: "baseline",
    }
    assert source.metadata_calls == [[V1, V2, V3, V5]]


def test_first_run_mode_comes_from_config_unless_overridden(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = video(V1)

    make(conn, cfg, source, clock).run_cycle([cfg.creator("MrBeast")])

    assert seen(conn) == {V1: "baseline"}  # config says baseline
    assert jobs(conn) == []


def test_first_run_is_per_creator(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0)]
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])
    source.feeds[SPEED] = [Upload(V2, T0)]
    source.meta[V2] = video(V2)

    report = monitor.run_cycle([cfg.creator("MrBeast"), cfg.creator("IShowSpeed")])

    assert report.items_new == 0  # IShowSpeed's first poll is a baseline too
    assert seen(conn) == {V1: "baseline", V2: "baseline"}
    assert [p.first_run for p in report.polls] == [False, True]


# --- filters ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "status"),
    [
        (video(V1, duration=299), SeenStatus.SKIPPED_SHORT),
        (video(V1, duration=None, status="is_upcoming"), SeenStatus.WAITING_VOD),
        (video(V1, status="post_live"), SeenStatus.WAITING_VOD),
        (video(V1, status="is_live"), SeenStatus.WAITING_VOD),
        ("unavailable", SeenStatus.UNAVAILABLE),
        (video(V1, duration=300), SeenStatus.ENQUEUED),
        (video(V1, status="was_live", duration=7200), SeenStatus.ENQUEUED),
        (video(V1, duration=None), SeenStatus.ENQUEUED),  # the worker probes it again
    ],
)
def test_new_item_filters(conn, cfg, source, clock, meta, status: SeenStatus) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = meta

    report = make(conn, cfg, source, clock).run_cycle([cfg.creator("MrBeast")])

    assert seen(conn)[V1] == status
    assert report.items_new == (1 if status is SeenStatus.ENQUEUED else 0)


def test_creator_override_raises_the_minimum_duration(conn, cfg, source, clock) -> None:
    source.feeds[RAFFY] = [Upload(V1, T0)]
    source.meta[V1] = video(V1, duration=450)

    make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT).run_cycle(
        [cfg.creator("Raffy Tulfo in Action")]
    )

    assert seen(conn) == {V1: "skipped_short"}  # 450 s < the creator's 600 s


def test_metadata_error_leaves_item_unseen_and_is_retried(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0), Upload(V2, T0)]
    source.meta.update({V1: "yt-dlp failed: Connection reset", V2: video(V2)})
    monitor = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)

    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert seen(conn) == {V2: "enqueued"}
    assert report.errors == [
        {"creator": "MrBeast", "item": V1, "error": "yt-dlp failed: Connection reset"}
    ]
    assert json.loads(runs(conn)[-1]["errors"]) == report.errors

    source.meta[V1] = video(V1)
    clock.now += timedelta(minutes=10)  # the next poll, after the first backoff step
    retried = monitor.run_cycle([cfg.creator("MrBeast")])

    assert retried.items_new == 1
    assert source.metadata_calls[-1] == [V1]


def not_first_poll(conn: sqlite3.Connection, creator: str = "MrBeast") -> None:
    conn.execute(
        "INSERT INTO seen_items VALUES ('youtube', 'zzzzzzzzzzz', ?, 'baseline', ?, ?)",
        (creator, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    )


def calls_for(source: FakeSource, vid: str) -> int:
    return sum(vid in call for call in source.metadata_calls)


def test_failing_item_backs_off_then_is_given_up(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "yt-dlp failed: Connection reset"
    monitor = make(conn, cfg, source, clock)
    attempts_at: list[int] = []

    for minute in range(0, 24 * 60, 10):  # a day of 10-minute polls
        clock.now = T0 + timedelta(minutes=minute)
        before = calls_for(source, V1)
        monitor.run_cycle([cfg.creator("MrBeast")])
        if calls_for(source, V1) > before:
            attempts_at.append(minute)

    assert attempts_at == [0, 10, 30, 70, 150, 310]  # 10 min doubling, then give up
    assert len(attempts_at) == MAX_METADATA_ATTEMPTS
    assert seen(conn)[V1] == "metadata_failed"
    assert jobs(conn) == []


def test_item_recovering_from_errors_is_enqueued_and_forgotten(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "yt-dlp failed: Connection reset"
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])

    clock.now += timedelta(minutes=5)  # still backing off: no call
    monitor.run_cycle([cfg.creator("MrBeast")])
    assert calls_for(source, V1) == 1

    source.meta[V1] = video(V1)
    clock.now += timedelta(minutes=5)
    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert report.items_new == 1
    assert monitor.failures == {}


def test_items_still_backing_off_are_not_counted_as_new(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "yt-dlp failed: Connection reset"
    monitor = make(conn, cfg, source, clock)
    first = monitor.run_cycle([cfg.creator("MrBeast")])

    clock.now += timedelta(minutes=5)  # still backing off: not looked up
    deferred = monitor.run_cycle([cfg.creator("MrBeast")])

    assert first.polls[0].new == 1
    assert deferred.polls[0].new == 0 and deferred.items_new == 0
    assert calls_for(source, V1) == 1


def test_giving_up_is_reported_in_the_poll_errors(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "yt-dlp failed: Connection reset"
    monitor = make(conn, cfg, source, clock)

    for _ in range(MAX_METADATA_ATTEMPTS):
        if V1 in monitor.failures:  # skip the backoff wait
            monitor.failures[V1].retry_at = clock.now
        report = monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=1)

    assert seen(conn)[V1] == "metadata_failed"
    gave_up = {
        "creator": "MrBeast",
        "item": V1,
        "error": f"metadata failed {MAX_METADATA_ATTEMPTS} times; given up",
    }
    assert report.errors[-1] == gave_up
    assert json.loads(runs(conn)[-1]["errors"])[-1] == gave_up


def test_bot_check_is_retried_every_poll_and_never_gives_up(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0), Upload(V2, T0)]
    source.meta.update({V1: "blocked by platform (bot check)", V2: video(V2)})
    monitor = make(conn, cfg, source, clock)

    for _ in range(MAX_METADATA_ATTEMPTS * 3):
        report = monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=10)

    assert calls_for(source, V1) == MAX_METADATA_ATTEMPTS * 3
    assert V1 not in seen(conn) and V2 not in seen(conn)  # V2 waits behind the block
    assert report.errors == [
        {"creator": "MrBeast", "item": V1, "error": "blocked by platform (bot check)"}
    ]


def test_format_errors_back_off_to_hourly_and_never_give_up(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "format: Requested format is not available"
    monitor = make(conn, cfg, source, clock)
    attempts_at: list[int] = []
    error = {"creator": "MrBeast", "item": V1, "error": "format: Requested format is not available"}

    for minute in range(0, 24 * 60, 10):  # a day of 10-minute polls
        clock.now = T0 + timedelta(minutes=minute)
        before = calls_for(source, V1)
        report = monitor.run_cycle([cfg.creator("MrBeast")])
        attempted = calls_for(source, V1) > before
        if attempted:
            attempts_at.append(minute)
        assert report.errors == ([error] if attempted else [])  # backing off: no error

    assert attempts_at[:6] == [0, 10, 30, 70, 130, 190]  # 10 min doubling, capped at 1 h
    assert len(attempts_at) == 4 + (24 * 60 - 130) // 60 + 1
    assert V1 not in seen(conn) and monitor.failures == {}


@pytest.mark.parametrize("interval_minutes", [60, 90])
def test_a_video_stuck_on_format_errors_does_not_starve_the_ones_behind_it(
    conn, cfg, source, clock, interval_minutes: int
) -> None:
    # X never gets a usable format; by the time the backoff (capped at 1 h) is over it is
    # due again each poll, and it is listed first (newest). V2 and V3 still get probed.
    x = "x" * 11
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(x, T0), Upload(V2, T0), Upload(V3, T0)]
    source.meta.update({x: "format: Requested format is not available"})
    source.meta.update({V2: video(V2), V3: video(V3)})
    monitor = make(conn, cfg, source, clock)

    for _ in range(3):
        monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=interval_minutes)

    assert seen(conn) == {"zzzzzzzzzzz": "baseline", V2: "enqueued", V3: "enqueued"}
    assert x in monitor.format_retries


def test_new_items_are_probed_before_retries(conn, cfg, source, clock) -> None:
    x, y = "x" * 11, "y" * 11
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(x, T0), Upload(y, T0)]
    source.meta.update({x: "format: Requested format is not available", y: "yt-dlp failed"})
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])  # x: format error, the batch stops
    clock.now += timedelta(hours=2)
    monitor.run_cycle([cfg.creator("MrBeast")])  # y (new): generic error; x: format error
    clock.now += timedelta(hours=2)
    source.feeds[BEAST] = [Upload(x, T0), Upload(y, T0), Upload(V1, T0)]
    source.meta[V1] = video(V1)

    monitor.run_cycle([cfg.creator("MrBeast")])

    assert source.metadata_calls[-1] == [V1, y, x]  # new, then failures, then format errors


def test_format_error_stops_the_batch_with_one_error_per_poll(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0), Upload(V2, T0), Upload(V3, T0)]
    source.meta.update({V1: "format: Requested format is not available", V2: video(V2)})
    source.meta[V3] = "format: Requested format is not available"
    monitor = make(conn, cfg, source, clock)

    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert source.metadata_calls == [[V1, V2, V3]]
    assert report.polls[0].new == 1  # V2 and V3 were never looked at
    assert len(json.loads(runs(conn)[-1]["errors"])) == 1
    assert seen(conn) == {"zzzzzzzzzzz": "baseline"}


def test_format_backoff_is_forgotten_when_the_item_leaves_the_listing(
    conn, cfg, source, clock
) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = "format: Requested format is not available"
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])
    assert V1 in monitor.format_retries

    source.feeds[BEAST] = [Upload(V2, T0)]
    source.meta[V2] = video(V2)
    monitor.run_cycle([cfg.creator("MrBeast")])

    assert monitor.format_retries == {}


@pytest.mark.parametrize("interval_minutes", [60, 90])
def test_format_retry_that_recovers_is_not_starved_by_a_stuck_one(
    conn, cfg, source, clock, interval_minutes: int
) -> None:
    # x (listed first) never gets a usable format; y had a format error too, then recovers.
    # With hourly polls x is due every time: if it were always probed first, its error
    # would stop the batch before y on every poll. The least recently tried goes first.
    x, y = "x" * 11, "y" * 11
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(x, T0), Upload(y, T0)]
    source.meta.update({x: "format: Requested format is not available"})
    source.meta.update({y: "format: Requested format is not available"})
    monitor = make(conn, cfg, source, clock)
    for _ in range(3):  # x fails, then y (new, so first), then whichever was tried earlier
        monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=interval_minutes)
    assert x in monitor.format_retries and y in monitor.format_retries

    source.meta[y] = video(y)
    for _ in range(2):
        monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=interval_minutes)

    assert seen(conn).get(y) == "enqueued"
    assert [row["item_id"] for row in jobs(conn)] == [y]
    assert x in monitor.format_retries and y not in monitor.format_retries


def test_retried_items_are_counted_as_new_only_on_their_first_look(
    conn, cfg, source, clock
) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V2, T0), Upload(V1, T0)]
    source.meta.update({V1: "format: Requested format is not available"})
    source.meta.update({V2: "yt-dlp failed: Connection reset"})
    monitor = make(conn, cfg, source, clock)
    first = monitor.run_cycle([cfg.creator("MrBeast")])  # both looked at, both fail
    clock.now += timedelta(hours=1)
    retry = monitor.run_cycle([cfg.creator("MrBeast")])  # both looked at again
    clock.now += timedelta(hours=1)
    source.meta.update({V1: video(V1), V2: video(V2)})
    recovered = monitor.run_cycle([cfg.creator("MrBeast")])

    assert source.metadata_calls == [[V2, V1], [V2, V1], [V2, V1]]
    assert first.polls[0].new == 2
    assert retry.polls[0].new == 0 and recovered.polls[0].new == 0
    assert recovered.items_new == 2  # runs.items_new still counts every job created
    assert runs(conn)[-1]["items_new"] == 2


def test_upload_probed_during_a_player_breakage_is_still_enqueued(conn, cfg, source, clock) -> None:
    from clipper.pipeline.ytdlp import RunResult, YtDlp

    format_error = "ERROR: [youtube] x: Requested format is not available. Use --list-formats"
    body = {"id": V1, "title": "Big video", "duration": 1500, "timestamp": T0.timestamp()}
    body["formats"] = [{"format_id": "140", "acodec": "mp4a.40.2", "url": "https://x/140"}]
    fixed = False  # an 8 h breakage, until a yt-dlp update fixes the player
    calls: list[list[str]] = []

    def ytdlp(args, timeout_s):
        calls.append(list(args))
        return RunResult(0, json.dumps(body), "") if fixed else RunResult(1, "", format_error)

    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.videos = YtDlpProvider(YtDlp(runner=ytdlp)).videos  # type: ignore[method-assign]
    monitor = make(conn, cfg, source, clock)

    for _ in range(8 * 6):
        monitor.run_cycle([cfg.creator("MrBeast")])
        clock.now += timedelta(minutes=10)
    assert V1 not in seen(conn)

    fixed = True
    clock.now += timedelta(hours=1)  # past the longest backoff
    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert seen(conn) == {"zzzzzzzzzzz": "baseline", V1: "enqueued"}
    assert report.items_new == 1
    assert monitor.format_retries == {} and monitor.failures == {}
    assert all("--ignore-no-formats-error" not in call for call in calls)


def poll_probed(conn, cfg, source, clock, availability: str):
    """One poll of V1 through the real yt-dlp provider; the probe (with cookies) works."""
    from clipper.pipeline.ytdlp import RunResult, YtDlp

    body = {"id": V1, "title": "Gated", "duration": 1500, "timestamp": T0.timestamp()}
    body |= {"availability": availability, "formats": [{"format_id": "140", "url": "https://x"}]}
    ytdlp = YtDlp(runner=lambda args, timeout_s: RunResult(0, json.dumps(body), ""))
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.videos = YtDlpProvider(ytdlp).videos  # type: ignore[method-assign]
    return make(conn, cfg, source, clock).run_cycle([cfg.creator("MrBeast")])


def test_age_restricted_upload_fetchable_with_cookies_is_enqueued(conn, cfg, source, clock) -> None:
    report = poll_probed(conn, cfg, source, clock, "needs_auth")

    assert seen(conn)[V1] == "enqueued"
    assert report.items_new == 1 and report.errors == []


@pytest.mark.parametrize("availability", ["subscriber_only", "premium_only"])
def test_members_and_premium_uploads_are_skipped_by_default(
    conn, cfg, source, clock, availability: str
) -> None:
    assert cfg.settings.allow_gated_content is False

    report = poll_probed(conn, cfg, source, clock, availability)

    assert seen(conn)[V1] == "unavailable"  # never retried
    assert jobs(conn) == [] and report.errors == []


@pytest.mark.parametrize("availability", ["subscriber_only", "premium_only", "needs_auth"])
def test_gated_uploads_are_enqueued_when_allowed(
    conn, source, clock, tmp_path: Path, availability: str
) -> None:
    path = tmp_path / "gated.yaml"
    path.write_text(CONFIG.replace("settings:\n", "settings:\n  allow_gated_content: true\n", 1))
    cfg = load_config(path)

    poll_probed(conn, cfg, source, clock, availability)

    assert seen(conn)[V1] == "enqueued"


def test_interrupted_emit_first_poll_keeps_its_lookback_after_restart(
    conn, cfg, source, clock
) -> None:
    source.feeds[BEAST] = [Upload(V1, T0 - timedelta(days=1)), Upload(V2, None)]  # V2: undated
    source.meta.update({V1: video(V1, age_days=1), V2: video(V2, age_days=30)})
    first = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    original = source.videos

    def stop_after_first(ids, should_stop):
        return original(ids, lambda: first.stop.is_set() or bool(first.stop.set()))

    source.videos = stop_after_first  # type: ignore[method-assign]
    first.run_cycle([cfg.creator("MrBeast")])  # SIGTERM after V1: V2 never looked at
    assert seen(conn) == {V1: "enqueued"}

    source.videos = original  # type: ignore[method-assign]
    clock.now += timedelta(minutes=10)
    restarted = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    report = restarted.run_cycle([cfg.creator("MrBeast")])

    assert report.items_new == 0
    assert seen(conn) == {V1: "enqueued", V2: "baseline"}  # 30 days old: outside lookback


def test_crashed_baseline_first_poll_still_baselines_the_rest(
    conn, cfg, source, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    source.feeds[BEAST] = [Upload(V1, T0 - timedelta(days=1)), Upload(V2, T0 - timedelta(days=2))]
    source.meta.update({V1: video(V1, age_days=1), V2: video(V2, age_days=2)})
    monitor = make(conn, cfg, source, clock)
    real_record = monitor._record
    calls = []

    def crash_on_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(monitor, "_record", crash_on_second)
    monitor.run_cycle([cfg.creator("MrBeast")])
    assert seen(conn) == {V1: "baseline"}

    monkeypatch.setattr(monitor, "_record", real_record)
    clock.now += timedelta(minutes=10)
    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert report.items_new == 0
    assert seen(conn) == {V1: "baseline", V2: "baseline"}  # published before the first poll
    assert jobs(conn) == []


def test_upload_after_the_first_poll_is_still_enqueued_later(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0 - timedelta(days=1))]
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])  # baseline at T0

    clock.now = T0 + timedelta(days=3)
    source.feeds[BEAST] = [Upload(V2, None), *source.feeds[BEAST]]  # undated listing entry
    source.meta[V2] = video(V2, age_days=-2)  # published a day after the first poll
    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert report.items_new == 1


def test_rounded_listing_date_does_not_baseline_an_upload_after_the_first_poll(
    conn, cfg, source, clock
) -> None:
    # yt-dlp's approximate_date rounds "1 hour ago" to the NEAREST hour: an upload at
    # 12:10, listed at 13:15, gets 12:00, before the 12:01 first poll's cutoff.
    clock.now = T0 + timedelta(minutes=1)
    source.feeds[BEAST] = [Upload(V1, T0 - timedelta(days=1))]
    monitor = make(conn, cfg, source, clock)
    monitor.run_cycle([cfg.creator("MrBeast")])  # baseline at 12:01

    clock.now = T0 + timedelta(minutes=75)
    source.feeds[BEAST] = [Upload(V2, T0), *source.feeds[BEAST]]
    source.meta[V2] = VideoMeta(V2, "New", 1200, "not_live", T0 + timedelta(minutes=10))
    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert source.metadata_calls == [[V2]]  # probed, not prefiltered on the rounded date
    assert seen(conn)[V2] == "enqueued" and report.items_new == 1


def test_day_precision_date_gets_a_day_of_slack(conn, cfg, source, clock) -> None:
    not_first_poll(conn)  # first poll at 2026-10-01T00:00Z (baseline cutoff)
    clock.now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    source.feeds[BEAST] = [Upload(V1, None), Upload(V2, None)]
    midnight = datetime(2026, 10, 1, tzinfo=UTC)
    source.meta.update(
        {
            # upload_date only: "some time that day", possibly after the first poll
            V1: VideoMeta(V1, "a", 1200, "not_live", midnight - timedelta(days=1), True),
            V2: VideoMeta(V2, "b", 1200, "not_live", midnight - timedelta(days=2), True),
        }
    )

    make(conn, cfg, source, clock).run_cycle([cfg.creator("MrBeast")])

    assert seen(conn)[V1] == "enqueued"
    assert seen(conn)[V2] == "baseline"  # clearly before the cutoff, slack or not


# --- isolation, runs rows, unresolved creators ---------------------------------------------


def test_listing_killed_by_stop_is_not_recorded_as_an_error(conn, cfg, source, clock) -> None:
    monitor = make(conn, cfg, source, clock)
    source.on_feed = lambda channel_id: monitor.stop.set()  # SIGTERM mid-listing
    source.feeds[BEAST] = SourceCancelled("yt-dlp listing cancelled")

    report = monitor.run_cycle([cfg.creator("MrBeast"), cfg.creator("IShowSpeed")])

    assert report.errors == []
    assert runs(conn)[-1]["errors"] is None
    assert source.feed_calls == [BEAST]  # the cycle ends there


@pytest.mark.parametrize(
    "error",
    [SourceError("feed: HTTP 500; fallback: HTTP 500"), sqlite3.OperationalError("disk I/O error")],
)
def test_real_error_during_shutdown_is_still_recorded(conn, cfg, source, clock, error) -> None:
    monitor = make(conn, cfg, source, clock)
    source.on_feed = lambda channel_id: monitor.stop.set()  # SIGTERM mid-listing
    source.feeds[BEAST] = error

    report = monitor.run_cycle([cfg.creator("MrBeast")])

    assert [e["creator"] for e in report.errors] == ["MrBeast"]
    assert str(error) in json.loads(runs(conn)[-1]["errors"])[0]["error"]


def test_one_creator_raising_does_not_stop_the_others(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = SourceError("feed: HTTP 500; fallback: blocked")
    source.feeds[SPEED] = [Upload(V2, T0)]
    source.feeds[RAFFY] = RuntimeError("unexpected bug ?key=SECRETKEY")
    source.meta[V2] = video(V2)
    monitor = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)

    report = monitor.run_cycle(cfg.creators)

    assert source.feed_calls == [BEAST, SPEED, RAFFY]
    assert [j["item_id"] for j in jobs(conn)] == [V2]
    assert [e["creator"] for e in report.errors] == ["MrBeast", "Raffy Tulfo in Action"]
    assert report.errors[0]["error"] == "SourceError: feed: HTTP 500; fallback: blocked"
    row = runs(conn)[-1]
    assert "SECRETKEY" not in row["errors"]
    assert json.loads(row["errors"]) == report.errors


def test_each_cycle_writes_one_runs_row(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.meta[V1] = video(V1)
    source.units_per_metadata_call = 1
    monitor = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)

    monitor.run_cycle(cfg.creators)
    clock.now += timedelta(minutes=10)
    monitor.run_cycle(cfg.creators)

    rows = runs(conn)
    assert [r["component"] for r in rows] == [COMPONENT, COMPONENT]
    assert COMPONENT == "monitor:youtube"
    assert [(r["items_new"], r["quota_units"], r["errors"]) for r in rows] == [
        (1, 1, None),
        (0, 0, None),
    ]
    assert rows[0]["started_at"] == "2026-10-20T12:00:00Z"
    assert rows[1]["finished_at"] == "2026-10-20T12:10:00Z"


def test_creator_without_channel_id_is_skipped_with_a_warning(
    conn, cfg, source, clock, caplog: pytest.LogCaptureFixture
) -> None:
    report = make(conn, cfg, source, clock).run_cycle(cfg.creators)

    assert report.unresolved == ["Unresolved"]
    assert "Unresolved: no YouTube channel_id; run `clipper resolve`" in caplog.text
    assert BEAST in source.feed_calls  # the others are polled
    assert report.errors == []


def test_stop_between_creators_finishes_the_current_one(conn, cfg, source, clock) -> None:
    monitor = make(conn, cfg, source, clock)
    source.on_feed = lambda channel: monitor.stop.set()

    report = monitor.run_cycle(cfg.creators)

    assert source.feed_calls == [BEAST]
    assert [p.creator for p in report.polls] == ["MrBeast"]
    assert runs(conn)[-1]["finished_at"] is not None


def test_stop_during_metadata_leaves_the_rest_for_next_time(conn, cfg, source, clock) -> None:
    source.feeds[BEAST] = [Upload(V1, T0), Upload(V2, T0)]
    source.meta.update({V1: video(V1), V2: video(V2)})
    monitor = make(conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    original = source.videos

    def stop_after_first(ids, should_stop):
        return original(ids, lambda: monitor.stop.is_set() or bool(monitor.stop.set()))

    source.videos = stop_after_first  # type: ignore[method-assign]
    monitor.run_cycle([cfg.creator("MrBeast")])

    assert seen(conn) == {V1: "enqueued"}  # V2 stays unseen: fetched next poll


def test_items_not_looked_at_are_not_counted_as_new(conn, cfg, source, clock) -> None:
    not_first_poll(conn)
    source.feeds[BEAST] = [Upload(V1, T0), Upload(V2, T0), Upload(V3, T0)]
    source.meta.update({V1: video(V1), V2: video(V2), V3: video(V3)})
    monitor = make(conn, cfg, source, clock)
    original = source.videos

    def stop_after_first(ids, should_stop):
        return original(ids, lambda: monitor.stop.is_set() or bool(monitor.stop.set()))

    source.videos = stop_after_first  # type: ignore[method-assign]
    stopped = monitor.run_cycle([cfg.creator("MrBeast")])
    source.videos = original  # type: ignore[method-assign]
    monitor.stop.clear()
    clock.now += timedelta(minutes=10)
    source.meta[V2] = "blocked by platform (bot check)"  # V3 waits behind the block
    blocked = monitor.run_cycle([cfg.creator("MrBeast")])
    clock.now += timedelta(minutes=10)
    source.meta[V2] = video(V2)
    rest = monitor.run_cycle([cfg.creator("MrBeast")])

    assert [r.polls[0].new for r in (stopped, blocked, rest)] == [1, 1, 2]
    assert [r["items_new"] for r in runs(conn)] == [1, 0, 2]
    assert seen(conn) == {"zzzzzzzzzzz": "baseline", V1: "enqueued", V2: "enqueued", V3: "enqueued"}


# --- run_forever -------------------------------------------------------------------------------


def test_run_forever_sleeps_the_jittered_interval_and_survives_cycle_errors(
    conn, cfg, source, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = make(conn, cfg, source, clock)
    sleeps: list[float] = []
    cycles: list[int] = []

    def flaky_cycle(creators):
        cycles.append(len(creators))
        if len(cycles) == 1:
            raise sqlite3.OperationalError("database is locked")

    def fake_wait(seconds: float) -> bool:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            monitor.stop.set()
        return monitor.stop.is_set()

    monkeypatch.setattr(monitor, "run_cycle", flaky_cycle)
    monkeypatch.setattr(monitor.stop, "wait", fake_wait)

    monitor.run_forever(cfg.creators)

    assert len(cycles) == 3
    assert all(540 <= s <= 660 for s in sleeps)  # 10 min +- 10 %
    assert len(set(sleeps)) > 1


# --- CLI ----------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, source: FakeSource, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "creators.yaml"
    config.write_text(CONFIG)
    db = tmp_path / "scratch" / "state.db"

    def fake_source(secrets, http, cancel):
        source.cancel = cancel  # the monitor's stop event, for in-flight yt-dlp children
        return source

    monkeypatch.setattr(cli, "_youtube_source", fake_source)
    return ["--config", str(config), "--db", str(db)], db


def test_cli_once_with_creator_filter_and_emit(cli_env, source: FakeSource) -> None:
    args, db = cli_env
    now = datetime.now(UTC)
    source.feeds[BEAST] = [Upload(V1, now)]
    source.meta[V1] = VideoMeta(V1, "Big video", 1500, "not_live", now)

    result = runner.invoke(
        app, [*args, "run-monitor", "--once", "--creator", "mrbeast", "--first-run", "emit"]
    )

    assert result.exit_code == 0, result.output
    assert source.feed_calls == [BEAST]
    assert f"MrBeast: 1 new (first poll)\n  enqueued job 1: {V1} Big video\n" in result.output
    assert "cycle done: 1 job(s) enqueued, 0 quota unit(s), 0 error(s)" in result.output
    with closing(connect(db)) as c:
        assert [r[0] for r in c.execute("SELECT item_id FROM jobs")] == [V1]


def test_cli_once_exits_nonzero_when_a_creator_failed(cli_env, source: FakeSource) -> None:
    args, _ = cli_env
    source.feeds[BEAST] = SourceError("blocked")

    result = runner.invoke(app, [*args, "run-monitor", "--once", "--creator", "MrBeast"])

    assert result.exit_code == 1
    assert "MrBeast: error: SourceError: blocked" in result.output


def test_cli_unknown_creator_is_an_error(cli_env) -> None:
    args, _ = cli_env

    result = runner.invoke(app, [*args, "run-monitor", "--once", "--creator", "Nobody"])

    assert result.exit_code == 1
    assert "unknown creator: Nobody" in result.stderr


def test_cli_rejects_bad_first_run_value(cli_env) -> None:
    args, _ = cli_env

    result = runner.invoke(app, [*args, "run-monitor", "--once", "--first-run", "all"])

    assert result.exit_code == 2


def test_cli_warns_about_unresolved_creators(cli_env) -> None:
    args, _ = cli_env

    result = runner.invoke(app, [*args, "run-monitor", "--once", "--creator", "Unresolved"])

    assert result.exit_code == 0
    assert "Unresolved: no channel_id; run `clipper resolve`" in result.output


def test_cli_sigterm_finishes_the_poll_and_exits_quickly(
    cli_env, source: FakeSource, tmp_path: Path
) -> None:
    args, db = cli_env
    source.feeds[SPEED] = [Upload(V2, T0)]
    source.on_feed = lambda channel: (
        os.kill(os.getpid(), signal.SIGTERM) if channel == BEAST else None
    )
    previous = signal.getsignal(signal.SIGTERM)
    started = time.monotonic()

    result = runner.invoke(app, [*args, "run-monitor"])

    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 10
    assert source.feed_calls == [BEAST]  # the current creator's poll finished; then exit
    assert source.cancel.is_set()  # the same event cancels a running yt-dlp child
    assert signal.getsignal(signal.SIGTERM) == previous
    with closing(connect(db)) as c:
        [row] = c.execute("SELECT finished_at FROM runs").fetchall()
    assert row[0] is not None


def test_cli_invalid_config_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "run-monitor", "--once"])

    assert result.exit_code == 1
    assert "config file not found" in result.stderr


def test_cli_missing_cookies_file_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "creators.yaml"
    config.write_text(CONFIG)
    monkeypatch.setenv("CLIPPER_YTDLP_COOKIES", str(tmp_path / "missing.txt"))

    result = runner.invoke(
        app, ["--config", str(config), "--db", str(tmp_path / "s.db"), "run-monitor", "--once"]
    )

    assert result.exit_code == 1
    assert "CLIPPER_YTDLP_COOKIES points to a missing file" in result.stderr


def test_default_source_is_rss_gated_and_keyless() -> None:
    stop = threading.Event()
    with httpx.Client() as http:
        source = cli._youtube_source(Secrets(), http, stop)

    assert isinstance(source, YouTubeSource)
    assert isinstance(source.provider, YtDlpProvider)
    assert source.provider.ytdlp.cancel is stop


def test_http_timeouts_fit_the_shutdown_bound() -> None:
    from clipper.sources.youtube import HTTP_DEADLINE_S, HTTP_TIMEOUT

    # A request gives up at the first chunk past the deadline; the slowest it can get
    # there is the deadline plus one blocked read. All of it inside SIGTERM's 10 s.
    assert HTTP_TIMEOUT.connect <= HTTP_DEADLINE_S
    assert HTTP_DEADLINE_S + max(HTTP_TIMEOUT.read, HTTP_TIMEOUT.pool) < 10


def test_stop_kills_an_in_flight_probe_and_the_monitor_exits_quickly(
    conn, cfg, source, clock, tmp_path: Path
) -> None:
    from clipper.pipeline.ytdlp import YtDlp, run_command

    pid_file = tmp_path / "pid"
    slow = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    def slow_ytdlp(args, timeout_s, **kwargs):  # a yt-dlp probe that hangs
        return run_command([sys.executable, "-c", slow], timeout_s, **kwargs)

    # The monitor runs in its own thread; it gets its own connection to the same DB.
    thread_conn = connect(tmp_path / "state.db", check_same_thread=False)
    monitor = make(thread_conn, cfg, source, clock, first_run_mode=FirstRun.EMIT)
    provider = YtDlpProvider(YtDlp(runner=slow_ytdlp, cancel=monitor.stop))
    source.feeds[BEAST] = [Upload(V1, T0)]
    source.videos = provider.videos  # type: ignore[method-assign]
    thread = threading.Thread(target=monitor.run_forever, args=([cfg.creator("MrBeast")],))
    thread.start()
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "probe never started"

    stopped_at = time.monotonic()
    monitor.stop.set()
    thread.join(10)
    thread_conn.close()

    assert not thread.is_alive()
    assert time.monotonic() - stopped_at < 5
    assert seen(conn) == {}  # interrupted: V1 is probed again next start
    assert runs(conn)[-1]["finished_at"] is not None
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- CLP-84: video ids that start with "-" ----------------------------------------------

DASH_IDS = ("-4mo8fGTST0", "-HgaXKz0rtg")  # real MrBeast uploads


def test_upload_whose_id_starts_with_a_dash_is_enqueued(
    conn, cfg, clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Through the real RSS parser and the keyless yt-dlp provider (both faked at the
    transport), so the id validation of every layer is exercised."""
    from clipper.pipeline.ytdlp import RunResult, YtDlp
    from clipper.sources.youtube import FEED_URL

    entries = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId><published>{T0.isoformat()}</published></entry>"
        for vid in DASH_IDS
    )
    feed = (
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
        f' xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(FEED_URL)
        return httpx.Response(200, text=feed)

    probes: list[list[str]] = []

    def ytdlp(args, timeout_s):
        probes.append(list(args))
        vid = args[-1].rpartition("v=")[2]
        body = {"id": vid, "title": f"Title {vid}", "duration": 1500, "timestamp": T0.timestamp()}
        return RunResult(0, json.dumps(body), "")

    not_first_poll(conn)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        source = YouTubeSource(http, YtDlpProvider(YtDlp(runner=ytdlp)))
        with caplog.at_level(logging.WARNING):
            report = make(conn, cfg, source, clock).run_cycle([cfg.creator("MrBeast")])

    assert "ignoring invalid YouTube video id" not in caplog.text
    assert report.items_new == 2
    assert {vid: seen(conn)[vid] for vid in DASH_IDS} == dict.fromkeys(DASH_IDS, "enqueued")
    rows = sorted((j["item_id"], j["source_url"]) for j in jobs(conn))
    assert rows == sorted((v, f"https://www.youtube.com/watch?v={v}") for v in DASH_IDS)
    for args in probes:  # the id reaches yt-dlp only inside the canonical URL, after "--"
        assert args[-2] == "--"
        assert args[-1].startswith("https://www.youtube.com/watch?v=-")
