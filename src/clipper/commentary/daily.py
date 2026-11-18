"""The daily commentary job (CLP-143): one job per commentary channel per local day.

Once the channel's `run_at` has passed in its account's timezone, the monitor enqueues
`platform = 'commentary'`, `item_id = '<account_id>:<YYYY-MM-DD>'` (the account's local
date), `creator = <account_id>`. `jobs` is unique on (platform, item_id), so later
cycles, restarts and a second monitor never make a second job for that day. The day is
the local calendar date: DST changes move the job's UTC hour but never add or drop a
day, and a `run_at` in the spring-forward gap counts from the first local time after it.
A day on which the monitor is down from `run_at` until local midnight gets no job: there
is no back-fill. A channel whose account is disabled gets no job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from clipper.accounts import AccountsConfig
from clipper.commentary.config import CommentaryConfig
from clipper.db import transaction
from clipper.jobs.queue import insert_job
from clipper.urls import COMMENTARY, SourceRef


@dataclass(frozen=True)
class DailyTarget:
    """A commentary channel the monitor makes a daily job for."""

    account_id: str
    timezone: str
    run_at: time


def daily_targets(commentary: CommentaryConfig, accounts: AccountsConfig) -> list[DailyTarget]:
    """One target per channel whose account is enabled. `load_commentary` has checked that
    each channel's account exists and is `content: commentary`."""
    by_id = {a.id: a for a in accounts.accounts}
    targets = []
    for channel in commentary.channels:
        account = by_id[channel.account]
        if account.enabled:
            run_at = commentary.settings_for(channel).run_at_time
            targets.append(DailyTarget(account.id, account.timezone, run_at))
    return targets


def day_item_id(account_id: str, day: date) -> str:
    return f"{account_id}:{day.isoformat()}"


def due_day(target: DailyTarget, now: datetime) -> date | None:
    """The account's local date when `now` is at or after its `run_at` that day, else None."""
    local = now.astimezone(ZoneInfo(target.timezone))
    return local.date() if local.time() >= target.run_at else None


def enqueue_daily(
    conn: sqlite3.Connection, target: DailyTarget, now: datetime
) -> tuple[int, str] | None:
    """Make the target's job for today once it is due. Returns (job id, item id) for a new
    job; None before `run_at` or when today's job already exists."""
    day = due_day(target, now)
    if day is None:
        return None
    item_id = day_item_id(target.account_id, day)
    with transaction(conn):
        job_id = insert_job(conn, SourceRef(COMMENTARY, item_id), now, creator=target.account_id)
    return None if job_id is None else (job_id, item_id)
