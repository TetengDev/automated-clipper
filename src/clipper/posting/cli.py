"""`clipper posts`: list, cancel, reschedule and retry scheduled posts (CLP-92)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from clipper.db import MigrationError, connect, migrate
from clipper.jobs.models import to_iso, utcnow
from clipper.pipeline.common import strip_control
from clipper.posting import repo
from clipper.posting.models import OPEN_STATUSES, InvalidPostTransition, Post, PostStatus

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
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        _fail(f"invalid time {text!r}; use ISO 8601, e.g. 2027-01-16T09:30 (UTC) or +08:00")
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


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


@app.command("reschedule")
def reschedule_cmd(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(min=1)],
    at: Annotated[str, typer.Argument(help="New time, ISO 8601; without an offset it is UTC.")],
) -> None:
    """Move a scheduled post (or requeue a failed one) to a new time."""
    when = _parse_time(at)
    _act(
        ctx,
        post_id,
        "reschedule",
        lambda c: repo.reschedule(c, post_id, when, utcnow(), by=CLI_ACTOR),
    )


@app.command("retry")
def retry_cmd(ctx: typer.Context, post_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Requeue a post_failed post now, with a fresh attempt count."""
    _act(ctx, post_id, "retry", lambda c: repo.retry(c, post_id, utcnow(), by=CLI_ACTOR))
