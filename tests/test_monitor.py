"""Monitor loop (CLP-24): poll cycles, first-run modes, exactly-once enqueue, isolation.

The source is a fake (feeds and metadata per test) and the clock is fake; no network.
"""

import json
import os
import signal
import sqlite3
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
    conn.execute(  # not the creator's first poll
        "INSERT INTO seen_items VALUES ('youtube', 'zzzzzzzzzzz', 'MrBeast', 'baseline', 'x', 'x')"
    )
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


# --- isolation, runs rows, unresolved creators ---------------------------------------------


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
    monkeypatch.setattr(cli, "_youtube_source", lambda secrets, http: source)
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
    with httpx.Client() as http:
        source = cli._youtube_source(Secrets(), http)

    assert isinstance(source, YouTubeSource)
    assert isinstance(source.provider, YtDlpProvider)
