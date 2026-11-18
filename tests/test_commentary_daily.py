"""The monitor's daily commentary job (CLP-143): one job per channel per local day at
run_at, across cycles, restarts and DST changes. Fake clock, no network."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from clipper import cli
from clipper.accounts import AccountsConfig
from clipper.cli import app
from clipper.commentary.config import CommentaryConfig
from clipper.commentary.daily import DailyTarget, daily_targets
from clipper.config import AppConfig
from clipper.db import connect, migrate
from clipper.monitor import YouTubeMonitor
from config_support import VALID_YAML

NY = ZoneInfo("America/New_York")
RG = DailyTarget("random_gaming_yt", "America/New_York", time(6, 0))
runner = CliRunner()


def ny(*args: int) -> datetime:
    return datetime(*args, tzinfo=NY).astimezone(UTC)  # type: ignore[misc]


class NoUploads:
    """A YouTube source with nothing to poll: these tests only make commentary jobs."""

    quota_units = 0

    def recent_uploads(self, channel_id: str) -> list:
        return []

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool]):
        raise AssertionError("no creator is polled")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db)
    yield c
    c.close()


def monitor(conn: sqlite3.Connection, clock: Clock, *targets: DailyTarget) -> YouTubeMonitor:
    return YouTubeMonitor(conn, AppConfig(), NoUploads(), clock=clock, commentary=targets)


def commentary_jobs(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = conn.execute(
        "SELECT item_id, creator, status, source_url FROM jobs"
        " WHERE platform = 'commentary' ORDER BY id"
    )
    return [tuple(r) for r in rows]


def test_no_job_before_run_at_then_one_at_run_at(conn: sqlite3.Connection) -> None:
    clock = Clock(ny(2026, 11, 19, 5, 59))
    m = monitor(conn, clock, RG)

    early = m.run_cycle([])
    clock.now = ny(2026, 11, 19, 6, 0)
    due = m.run_cycle([])

    assert early.commentary == [] and early.items_new == 0
    assert commentary_jobs(conn) == [
        ("random_gaming_yt:2026-11-19", "random_gaming_yt", "detected", "")
    ]
    assert due.commentary == [(1, "random_gaming_yt:2026-11-19")]
    assert due.items_new == 1
    last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert (last_run["component"], last_run["items_new"]) == ("monitor:youtube", 1)


def test_exactly_one_job_per_account_per_local_day(conn: sqlite3.Connection) -> None:
    manila = DailyTarget("other_yt", "Asia/Manila", time(6, 0))
    start = ny(2026, 11, 19, 0, 0)  # 13:00 in Manila
    clock = Clock(start)
    m = monitor(conn, clock, RG, manila)

    while clock.now < start + timedelta(hours=72):  # a cycle every 10 minutes
        m.run_cycle([])
        clock.now += timedelta(minutes=10)

    items = [item for item, *_ in commentary_jobs(conn)]
    assert sorted(i for i in items if i.startswith("random_gaming_yt:")) == [
        "random_gaming_yt:2026-11-19",
        "random_gaming_yt:2026-11-20",
        "random_gaming_yt:2026-11-21",
    ]
    assert sorted(i for i in items if i.startswith("other_yt:")) == [
        "other_yt:2026-11-19",
        "other_yt:2026-11-20",
        "other_yt:2026-11-21",
        "other_yt:2026-11-22",
    ]


def test_a_restart_makes_no_second_job(conn: sqlite3.Connection, db: Path) -> None:
    monitor(conn, Clock(ny(2026, 11, 19, 6, 5)), RG).run_cycle([])

    with closing(connect(db)) as fresh:  # a new process: new connection, new monitor
        again = monitor(fresh, Clock(ny(2026, 11, 19, 6, 15)), RG).run_cycle([])

    assert again.commentary == [] and again.items_new == 0
    assert len(commentary_jobs(conn)) == 1


def test_the_day_is_the_accounts_local_date(conn: sqlite3.Connection) -> None:
    clock = Clock(datetime(2026, 11, 20, 3, 0, tzinfo=UTC))  # 22:00 on the 19th in New York
    m = monitor(conn, clock, RG)

    m.run_cycle([])
    clock.now = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)  # 06:00 on the 20th
    m.run_cycle([])

    assert [item for item, *_ in commentary_jobs(conn)] == [
        "random_gaming_yt:2026-11-19",
        "random_gaming_yt:2026-11-20",
    ]


def test_spring_forward_run_at_in_the_skipped_hour(conn: sqlite3.Connection) -> None:
    target = DailyTarget("random_gaming_yt", "America/New_York", time(2, 30))
    clock = Clock(datetime(2026, 3, 8, 6, 59, tzinfo=UTC))  # 01:59 EST
    m = monitor(conn, clock, target)

    m.run_cycle([])
    assert commentary_jobs(conn) == []
    clock.now = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)  # 03:00 EDT: 02:30 never happened
    m.run_cycle([])
    clock.now = datetime(2026, 3, 9, 6, 29, tzinfo=UTC)  # 02:29 EDT the next day
    m.run_cycle([])
    clock.now = datetime(2026, 3, 9, 6, 30, tzinfo=UTC)  # 02:30 EDT
    m.run_cycle([])

    assert [item for item, *_ in commentary_jobs(conn)] == [
        "random_gaming_yt:2026-03-08",
        "random_gaming_yt:2026-03-09",
    ]


def test_fall_back_repeated_hour_makes_no_second_job(conn: sqlite3.Connection) -> None:
    target = DailyTarget("random_gaming_yt", "America/New_York", time(1, 30))
    clock = Clock(datetime(2026, 11, 1, 5, 29, tzinfo=UTC))  # 01:29 EDT
    m = monitor(conn, clock, target)

    for minute_utc in ((5, 29), (5, 30), (6, 0), (6, 30), (6, 45)):  # 01:30 comes twice
        clock.now = datetime(2026, 11, 1, *minute_utc, tzinfo=UTC)
        m.run_cycle([])

    assert [item for item, *_ in commentary_jobs(conn)] == ["random_gaming_yt:2026-11-01"]


def test_a_failing_channel_does_not_stop_the_others(conn: sqlite3.Connection) -> None:
    broken = DailyTarget("bad_yt", "Mars/Olympus", time(6, 0))

    report = monitor(conn, Clock(ny(2026, 11, 19, 7, 0)), broken, RG).run_cycle([])

    assert [item for item, *_ in commentary_jobs(conn)] == ["random_gaming_yt:2026-11-19"]
    (error,) = report.errors
    assert error["creator"] == "bad_yt"
    assert error["error"].startswith("ZoneInfoNotFoundError")
    last_run = conn.execute("SELECT errors FROM runs ORDER BY id DESC").fetchone()
    assert json.loads(last_run["errors"])[0]["creator"] == "bad_yt"


def _configs(enabled: bool = True) -> tuple[CommentaryConfig, AccountsConfig]:
    accounts = AccountsConfig.model_validate(
        {
            "accounts": [
                {
                    "id": "random_gaming_yt",
                    "platform": "youtube",
                    "content": "commentary",
                    "channel_id": "UCcMYw3Ae2crqXE2yzFBSUig",
                    "handle": "@RandomGaming-f3w",
                    "market": "US",
                    "timezone": "America/New_York",
                    "enabled": enabled,
                }
            ]
        }
    )
    commentary = CommentaryConfig.model_validate(
        {
            "channels": [
                {
                    "account": "random_gaming_yt",
                    "source": "twitch_clips",
                    "prompt": "commentary_gaming_v1",
                    "games": [{"name": "Grand Theft Auto VI"}],
                    "overrides": {"run_at": "07:30"},
                }
            ]
        }
    )
    return commentary, accounts


def test_daily_targets_use_the_account_timezone_and_the_channel_run_at() -> None:
    assert daily_targets(*_configs()) == [
        DailyTarget("random_gaming_yt", "America/New_York", time(7, 30))
    ]


def test_a_disabled_account_gets_no_daily_job() -> None:
    assert daily_targets(*_configs(enabled=False)) == []


# --- CLI ---------------------------------------------------------------------------------------

ACCOUNTS_YAML = """\
accounts:
  - id: random_gaming_yt
    platform: youtube
    content: commentary
    channel_id: UCcMYw3Ae2crqXE2yzFBSUig
    handle: "@RandomGaming-f3w"
    market: US
    timezone: America/New_York
"""

COMMENTARY_YAML = """\
defaults:
  run_at: "00:00"
channels:
  - account: random_gaming_yt
    source: twitch_clips
    prompt: commentary_gaming_v1
    games:
      - name: Grand Theft Auto VI
"""


@pytest.fixture
def cli_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    (tmp_path / "creators.yaml").write_text(VALID_YAML)
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    monkeypatch.setattr(cli, "_youtube_source", lambda secrets, http, cancel: NoUploads())
    return ["--config", str(tmp_path / "creators.yaml"), "--db", str(tmp_path / "cli.db")]


def test_cli_run_monitor_makes_the_daily_commentary_job_once(
    tmp_path: Path, cli_args: list[str]
) -> None:
    (tmp_path / "commentary.yaml").write_text(COMMENTARY_YAML)  # run_at 00:00: always due

    first = runner.invoke(app, [*cli_args, "run-monitor", "--once"])
    second = runner.invoke(app, [*cli_args, "run-monitor", "--once"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "commentary: enqueued job 1: random_gaming_yt:" in first.output
    assert "commentary: enqueued job" not in second.output
    with closing(connect(tmp_path / "cli.db")) as c:
        rows = c.execute("SELECT platform, item_id, creator FROM jobs").fetchall()
    (row,) = rows
    assert row["platform"] == "commentary" and row["creator"] == "random_gaming_yt"
    assert row["item_id"].startswith("random_gaming_yt:")


def test_cli_run_monitor_fails_on_an_invalid_commentary_config(
    tmp_path: Path, cli_args: list[str]
) -> None:
    (tmp_path / "commentary.yaml").write_text("defaults:\n  surprise: 1\n")

    result = runner.invoke(app, [*cli_args, "run-monitor", "--once"])

    assert result.exit_code == 1
    assert "invalid commentary config" in result.stderr
