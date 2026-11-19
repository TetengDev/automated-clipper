"""The weekly insights summary (CLP-154): one message per commentary channel through the
notify webhook (DISCORD_WEBHOOK_URL, else the log): the top 3 and bottom 3 Shorts of the
last 28 days, what to do more or less of by tag, and tracker progress.

Sent on the first insights run on or after Monday 09:00 in the channel's timezone, once a
week; the very first summary goes out on the first run. A `runs` row (component
`insights:weekly:<account_id>`) records each sent summary, so a restart never sends it
twice. A failed send records nothing and is retried on the next run (hourly).

The text names variables only, never a token or key value.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from clipper.accounts import token_env_name
from clipper.db import transaction
from clipper.insights import repo, report
from clipper.insights.collect import InsightsAccount
from clipper.jobs.models import from_iso, to_iso
from clipper.notify import Notify, NotifyError

SEND_WEEKDAY = 0  # Monday
SEND_TIME = time(9, 0)
COMPONENT_PREFIX = "insights:weekly:"
TOP = 3
TITLE_CHARS = 60
NO_DATA = "no data yet"

logger = logging.getLogger(__name__)


def week_start(now: datetime, timezone: str) -> datetime:
    """The latest Monday 09:00 (channel time) at or before `now`, as UTC."""
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    monday = local.date() - timedelta(days=(local.weekday() - SEND_WEEKDAY) % 7)
    start = datetime.combine(monday, SEND_TIME, tzinfo=zone)
    if local < start:
        start = datetime.combine(monday - timedelta(days=7), SEND_TIME, tzinfo=zone)
    return start.astimezone(UTC)


def _last_sent(conn: sqlite3.Connection, account_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(started_at) FROM runs WHERE component = ?", (COMPONENT_PREFIX + account_id,)
    ).fetchone()
    return from_iso(row[0]) if row and row[0] else None


def summary_due(conn: sqlite3.Connection, account: InsightsAccount, now: datetime) -> bool:
    last = _last_sent(conn, account.id)
    return last is None or last < week_start(now, account.timezone)


def _short(index: int, row: report.ShortRow) -> str:
    views = NO_DATA if row.views is None else f"{row.views:,} views"
    ratio = "" if row.ratio is None else f", {row.ratio:.1f}x median"
    return f"{index}. {row.title[:TITLE_CHARS]} ({views}{ratio})"


def _group(g: report.TagGroup) -> str:
    return f"{g.key}={g.value} ({(g.median_ratio or 0.0):.1f}x, {g.shorts} Shorts)"


def _goal(g: report.GoalProgress) -> str:
    if g.value is None:
        return f"{g.goal.label}: {NO_DATA}"
    return f"{g.goal.label} {g.value:,}/{g.goal.target:,} ({g.outlook})"


def _analytics_line(conn: sqlite3.Connection, account_id: str) -> str | None:
    state = repo.analytics_state(conn, account_id)
    if state is None or state.state not in (
        repo.AnalyticsState.NEEDS_REAUTH,
        repo.AnalyticsState.NO_TOKEN,
    ):
        return None
    name = token_env_name(account_id, "refresh_token")
    if state.state is repo.AnalyticsState.NEEDS_REAUTH:
        return (
            f"Analytics needs re-authorisation (scope yt-analytics.readonly): replace {name};"
            " see docs/DEPLOYMENT.md, Channel insights."
        )
    return (
        f"Analytics is off: {name} or the OAuth client is not set; see docs/DEPLOYMENT.md,"
        " Channel insights."
    )


def build_summary(conn: sqlite3.Connection, account: InsightsAccount, now: datetime) -> str:
    local_today = now.astimezone(ZoneInfo(account.timezone)).date()
    rows = report.shorts_table(conn, account.id, now)
    ranked = sorted((r for r in rows if r.views is not None), key=lambda r: -(r.views or 0))
    top = ranked[:TOP]
    shown = {r.video_id for r in top}
    bottom = [r for r in reversed(ranked) if r.video_id not in shown][:TOP]
    lines = [f"Weekly insights for {account.id} (Shorts of the last {report.TABLE_DAYS} days)"]
    if not ranked:
        lines.append("No public Shorts with numbers yet.")
    else:
        lines.append("Top:")
        lines += [_short(i, r) for i, r in enumerate(top, 1)]
        if bottom:
            lines.append("Bottom:")
            lines += [_short(i, r) for i, r in enumerate(bottom, 1)]
    more, less = report.findings(report.tag_breakdown(rows, now.astimezone(UTC).date()))
    if more:
        lines.append("Do more: " + "; ".join(_group(g) for g in more))
    if less:
        lines.append("Do less: " + "; ".join(_group(g) for g in less))
    if not more and not less:
        lines.append(
            f"Per tag: not enough settled Shorts per tag value yet ({report.MIN_TAG_SHORTS}"
            f" or more each, at least {report.FINDINGS_MIN_AGE_DAYS} days old)."
        )
    for tier in report.tracker(conn, account.id, local_today):
        goals = "; ".join(_goal(g) for g in tier.goals)
        lines.append(f"{tier.tier.name}: {tier.outlook}. {goals}")
    if (line := _analytics_line(conn, account.id)) is not None:
        lines.append(line)
    lines.append(f"Details: /insights/{account.id}")
    return "\n".join(lines)


def send_weekly(
    conn: sqlite3.Connection, account: InsightsAccount, now: datetime, notify: Notify
) -> bool:
    """Send the summary when it is due; True when it went out."""
    if not summary_due(conn, account, now):
        return False
    text = build_summary(conn, account, now)
    try:
        notify(text)
    except NotifyError as exc:
        logger.error("insights %s: weekly summary not sent: %s", account.id, exc)
        return False
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (component, started_at, finished_at, items_new) VALUES (?, ?, ?, 1)",
            (COMPONENT_PREFIX + account.id, to_iso(now), to_iso(now)),
        )
    return True
