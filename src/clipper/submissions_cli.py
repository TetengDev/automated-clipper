"""`clipper campaigns`: list campaign submissions, mark one submitted, record the views and
payout a program reported, and print RPM per account and campaign (CLP-97).

Submitting on the program's site stays a manual step: no scraping, no login automation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from clipper import submissions
from clipper.db import MigrationError, connect, migrate
from clipper.jobs.models import to_iso, utcnow
from clipper.pipeline.common import strip_control

app = typer.Typer(
    help="Campaign submissions: list, mark submitted, record earnings, RPM.",
    no_args_is_help=True,
)
CLI_ACTOR = "cli"


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _open(ctx: typer.Context) -> sqlite3.Connection:
    db_path: Path = ctx.find_root().obj.db_path
    try:
        conn = connect(db_path)
        try:
            migrate(conn)
        except BaseException:
            conn.close()
            raise
    except (sqlite3.Error, OSError, MigrationError) as exc:
        _fail(f"cannot open database {db_path}: {exc}")
    return conn


def _line(s: submissions.Submission) -> str:
    now = utcnow()
    if s.status == submissions.SubmissionStatus.PENDING:
        state = "overdue" if s.overdue(now) else "pending"
    else:
        state = "late" if s.late else str(s.status)
    deadline = to_iso(s.deadline_at) if s.deadline_at else "no deadline"
    return "  ".join(
        [
            f"{s.id:>5}",
            f"{state:<9}",
            f"{strip_control(s.campaign_id):<20}",
            f"{strip_control(s.account_id):<20}",
            f"{s.platform:<9}",
            f"deadline {deadline}",
            strip_control(s.url),
        ]
    ).rstrip()


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    pending: Annotated[bool, typer.Option(help="Only links still to submit.")] = False,
) -> None:
    """List campaign submissions, oldest post first."""
    conn = _open(ctx)
    try:
        rows = submissions.list_submissions(conn, pending_only=pending)
    finally:
        conn.close()
    if not rows:
        typer.echo("no submissions")
    for s in rows:
        typer.echo(_line(s))


@app.command("submit")
def submit_cmd(ctx: typer.Context, submission_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Mark a link submitted after submitting it on the program's site."""
    conn = _open(ctx)
    try:
        done = submissions.mark_submitted(conn, submission_id, utcnow(), by=CLI_ACTOR)
    except LookupError:
        _fail(f"submission {submission_id} not found")
    except submissions.AlreadySubmitted:
        _fail(f"submission {submission_id} is already submitted")
    finally:
        conn.close()
    typer.echo(f"submission {submission_id} submitted{' (late)' if done.late else ''}")


@app.command("earnings")
def earnings_cmd(
    ctx: typer.Context,
    submission_id: Annotated[int, typer.Argument(min=1)],
    views: Annotated[int, typer.Option(min=0, help="Approved views the program reported.")],
    payout: Annotated[str, typer.Option(help="Payout for this submission, e.g. 12.50.")],
) -> None:
    """Record the approved views and payout for one submission."""
    try:
        amount = submissions.parse_payout(payout)
    except ValueError as exc:
        _fail(str(exc))
    conn = _open(ctx)
    try:
        submissions.record_earnings(conn, submission_id, views=views, payout=amount, now=utcnow())
    except LookupError:
        _fail(f"submission {submission_id} not found")
    finally:
        conn.close()
    typer.echo(f"submission {submission_id}: {views} views, payout {amount}")


@app.command("rpm")
def rpm_cmd(ctx: typer.Context) -> None:
    """RPM (payout per 1,000 approved views) per account and per campaign."""
    conn = _open(ctx)
    try:
        rollup = submissions.rpm_rollup(conn)
    finally:
        conn.close()
    if not rollup.by_account:
        typer.echo("no submissions")
        return
    for label, rows in (("account", rollup.by_account), ("campaign", rollup.by_campaign)):
        typer.echo(f"by {label}:")
        for r in rows:
            rpm = r.rpm if r.rpm is not None else "-"
            typer.echo(
                f"  {strip_control(r.key):<20}  submissions {r.submissions}"
                f"  with earnings {r.with_earnings}  views {r.views}  payout {r.payout}  rpm {rpm}"
            )
