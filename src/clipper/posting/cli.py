"""`clipper posts`: list, cancel, reschedule and retry scheduled posts (CLP-92)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from clipper.config import ConfigError, load_config
from clipper.db import MigrationError, connect, migrate
from clipper.jobs.models import to_iso, utcnow
from clipper.pipeline.common import strip_control
from clipper.posting import repo
from clipper.posting.models import (
    OPEN_STATUSES,
    InvalidPostTransition,
    Post,
    PostStatus,
    parse_utc,
    reschedule_problem,
)
from clipper.posting.scheduler import MAX_DAYS_AHEAD, AccountLike, pick_slot, slot_problems

app = typer.Typer(
    help="Scheduled posts: list, cancel, reschedule, retry.",
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


def _accounts(ctx: typer.Context) -> dict[str, AccountLike]:
    """The posting accounts (CLP-91), so retry and reschedule keep to their slot rules
    (CLP-106). No accounts file: none, and the old unchecked behaviour."""
    from clipper.accounts import accounts_by_id, accounts_path

    config_path: Path = ctx.find_root().obj.config_path
    if not accounts_path(config_path).exists():
        return {}
    try:
        return dict(accounts_by_id(config_path, load_config(config_path)))
    except ConfigError as exc:
        _fail(f"cannot load the accounts config: {exc}")


def _get(conn: sqlite3.Connection, post_id: int) -> Post:
    post = repo.get_post(conn, post_id)
    if post is None:
        conn.close()
        _fail(f"post {post_id} not found")
    return post


def _confirm_problems(problems: list[str], yes: bool) -> None:
    """Flag the account rules a chosen time breaks; go on only if the operator agrees."""
    if not problems:
        return
    for problem in problems:
        typer.echo(f"warning: {problem}")
    if not yes:
        typer.confirm("Schedule it anyway?", abort=True)


def _problems(conn: sqlite3.Connection, account: AccountLike | None, post: Post, when: datetime):
    if account is None:
        return []
    others = repo.account_post_times(conn, account.id, exclude_post_id=post.id)
    return slot_problems(account, others, when)


def _line(post: Post) -> str:
    detail = post.url or post.error or ""
    return "  ".join(
        [
            f"{post.id:>5}",
            f"{post.status:<11}",
            f"{post.platform:<9}",
            f"{strip_control(post.account_id):<20}",
            f"clip {post.clip_id} v{post.clip_version}",
            to_iso(post.scheduled_at),
            f"attempts {post.attempts}",
            strip_control(detail),
        ]
    ).rstrip()


def _parse_time(text: str) -> datetime:
    when = parse_utc(text)
    if when is None:
        _fail(f"invalid time {text!r}; use ISO 8601, e.g. 2027-01-16T09:30 (UTC) or +08:00")
    problem = reschedule_problem(when, utcnow())
    if problem:
        _fail(f"invalid time {text!r}: {problem}")
    return when


def _act(
    ctx: typer.Context, post_id: int, action: str, change: Callable[[sqlite3.Connection], Post]
) -> None:
    conn = _open(ctx)
    try:
        post = change(conn)
    except LookupError:
        _fail(f"post {post_id} not found")
    except InvalidPostTransition as exc:
        _fail(f"cannot {action} post {post_id}: it is {exc.current}")
    finally:
        conn.close()
    typer.echo(_line(post))


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option(help="Only posts in this status.")] = None,
    clip: Annotated[int | None, typer.Option(help="Only posts of this clip id.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Include posted and cancelled.")] = False,
) -> None:
    """List posts (default: scheduled, uploading and post_failed), soonest first."""
    statuses: tuple[PostStatus, ...] | None
    if status is not None:
        try:
            statuses = (PostStatus(status),)
        except ValueError:
            _fail(f"unknown status {status!r}; one of: {', '.join(PostStatus)}")
    else:
        statuses = None if all_ else OPEN_STATUSES
    conn = _open(ctx)
    try:
        posts = repo.list_posts(conn, statuses=statuses, clip_id=clip)
    finally:
        conn.close()
    if not posts:
        typer.echo("no posts")
        return
    for post in posts:
        typer.echo(_line(post))


@app.command("cancel")
def cancel_cmd(ctx: typer.Context, post_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Cancel a scheduled or failed post."""
    _act(ctx, post_id, "cancel", lambda c: repo.cancel(c, post_id, utcnow(), by=CLI_ACTOR))


YES_HELP = "Do not ask when the time breaks the account's windows, gap or daily limit."


@app.command("reschedule")
def reschedule_cmd(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(min=1)],
    at: Annotated[str, typer.Argument(help="New time, ISO 8601; without an offset it is UTC.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help=YES_HELP)] = False,
) -> None:
    """Move a scheduled post (or requeue a failed one) to a new time. A time that breaks
    the account's rules is flagged and needs confirming."""
    when = _parse_time(at)
    accounts = _accounts(ctx)
    conn = _open(ctx)
    try:
        post = _get(conn, post_id)
        _confirm_problems(_problems(conn, accounts.get(post.account_id), post, when), yes)
    finally:
        conn.close()
    _act(
        ctx,
        post_id,
        "reschedule",
        lambda c: repo.reschedule(c, post_id, when, utcnow(), by=CLI_ACTOR),
    )


@app.command("retry")
def retry_cmd(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(min=1)],
    at: Annotated[
        str | None,
        typer.Option(help="Retry at this time (ISO 8601) instead of the next free slot."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help=YES_HELP)] = False,
) -> None:
    """Requeue a post_failed post with a fresh attempt count, in the account's next free
    slot (or at --at, flagged if it breaks the account's rules)."""
    when = _parse_time(at) if at is not None else None
    accounts = _accounts(ctx)
    conn = _open(ctx)
    try:
        post = _get(conn, post_id)
        if post.status != PostStatus.POST_FAILED:
            _fail(f"cannot retry post {post_id}: it is {post.status}")
        account = accounts.get(post.account_id)
        if when is not None:
            _confirm_problems(_problems(conn, account, post, when), yes)
        elif account is None:
            if accounts:
                typer.echo(
                    f"warning: account {strip_control(post.account_id)} is not in the accounts"
                    " config; requeued now without its slot rules"
                )
        else:
            others = repo.account_post_times(conn, account.id, exclude_post_id=post.id)
            when = pick_slot(account, others, utcnow())
            if when is None:
                _fail(
                    f"no free slot for account {account.id} in the next {MAX_DAYS_AHEAD} days;"
                    " pass --at TIME to choose one"
                )
    finally:
        conn.close()
    _act(ctx, post_id, "retry", lambda c: repo.retry(c, post_id, utcnow(), at=when, by=CLI_ACTOR))
