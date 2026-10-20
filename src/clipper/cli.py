"""`clipper` command-line entrypoint."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from clipper.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from clipper.db import DEFAULT_DB_PATH, MigrationError, connect, current_version, migrate

EXIT_NOT_IMPLEMENTED = 70  # EX_SOFTWARE; distinct from Click's usage-error code 2

app = typer.Typer(
    help="24/7 auto-clipper: detect, transcribe, select highlights, render, review.",
    no_args_is_help=True,
    add_completion=False,
)


@dataclass(frozen=True)
class AppContext:
    config_path: Path
    db_path: Path


def _app(ctx: typer.Context) -> AppContext:
    assert isinstance(ctx.obj, AppContext)
    return ctx.obj


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option(envvar="CLIPPER_CONFIG", help="Path to creators.yaml.")
    ] = DEFAULT_CONFIG_PATH,
    db: Annotated[Path, typer.Option(envvar="CLIPPER_DB", help="Path to SQLite DB.")] = (
        DEFAULT_DB_PATH
    ),
) -> None:
    ctx.obj = AppContext(config_path=config, db_path=db)


def _not_implemented(issue: str) -> NoReturn:
    _fail(f"not implemented yet ({issue})", code=EXIT_NOT_IMPLEMENTED)


@app.command("check-config")
def check_config(ctx: typer.Context) -> None:
    """Validate creators.yaml and print a summary."""
    try:
        cfg = load_config(_app(ctx).config_path)
    except ConfigError as exc:
        _fail(str(exc))
    typer.echo(f"ok: {len(cfg.creators)} creators")
    for creator in cfg.creators:
        platforms = [p for p in ("youtube", "twitch") if getattr(creator, p) is not None]
        typer.echo(f"  - {creator.name} [{creator.region}] {', '.join(platforms)}")


@app.command("migrate")
def migrate_cmd(ctx: typer.Context) -> None:
    """Create or upgrade the SQLite schema."""
    db_path = _app(ctx).db_path
    try:
        conn = connect(db_path)
        try:
            applied = migrate(conn)
            version = current_version(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, MigrationError) as exc:
        _fail(f"migrate failed for {db_path}: {exc}")
    if applied:
        typer.echo(f"applied migrations: {', '.join(map(str, applied))}")
    else:
        typer.echo(f"schema up to date (version {version})")


@app.command("run-monitor")
def run_monitor() -> None:
    """Poll creators 24/7 and enqueue new content."""
    _not_implemented("CLP-24")


@app.command("run-worker")
def run_worker() -> None:
    """Process queued jobs through the clip pipeline."""
    _not_implemented("CLP-12")


@app.command("run-review")
def run_review() -> None:
    """Serve the clip review web app."""
    _not_implemented("CLP-18")


@app.command("enqueue")
def enqueue(url: Annotated[str, typer.Argument(help="YouTube or Twitch video URL.")]) -> None:
    """Queue a single video for clipping."""
    _not_implemented("CLP-12")


@app.command("resolve")
def resolve() -> None:
    """Resolve creator handles to stable channel / user IDs."""
    _not_implemented("CLP-22")


@app.command("status")
def status() -> None:
    """Show job and clip counts."""
    _not_implemented("CLP-33")


@app.command("retry")
def retry(job_id: Annotated[int, typer.Argument(help="Job ID to retry.")]) -> None:
    """Re-queue a failed job."""
    _not_implemented("CLP-34")
