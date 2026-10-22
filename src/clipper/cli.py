"""`clipper` command-line entrypoint."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import signal
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from clipper import jobs
from clipper.config import (
    CREATOR_NAME_PATTERN,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    MissingSecretError,
    Secrets,
    load_config,
)
from clipper.db import DEFAULT_DB_PATH, MigrationError, connect, current_version, migrate
from clipper.export import DEFAULT_OUTPUT_DIR
from clipper.pipeline import build_stages
from clipper.urls import InvalidSourceURL, parse_source_url

EXIT_NOT_IMPLEMENTED = 70  # EX_SOFTWARE; distinct from Click's usage-error code 2
MANUAL_CREATOR = "manual"

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


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


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


def _open_db(db_path: Path) -> sqlite3.Connection:
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


@app.command("run-worker")
def run_worker(
    ctx: typer.Context,
    media_dir: Annotated[
        Path, typer.Option(envvar="CLIPPER_MEDIA_DIR", help="Per-job stage artifacts.")
    ] = jobs.DEFAULT_MEDIA_ROOT,
    worker_id: Annotated[
        str | None,
        typer.Option(envvar="CLIPPER_WORKER_ID", help="Claim id (default: unique per process)."),
    ] = None,
    recover_id: Annotated[
        str | None,
        typer.Option(help="Release jobs claimed by this dead worker id before starting."),
    ] = None,
    poll_seconds: Annotated[float, typer.Option(min=0.1, help="Idle sleep.")] = (
        jobs.DEFAULT_POLL_SECONDS
    ),
    once: Annotated[bool, typer.Option(help="Run one stage of one job, then exit.")] = False,
) -> None:
    """Process queued jobs through the clip pipeline."""
    app_ctx = _app(ctx)
    try:
        cfg = load_config(app_ctx.config_path)
    except ConfigError as exc:
        _fail(str(exc))
    _setup_logging()
    conn = _open_db(app_ctx.db_path)
    worker = jobs.Worker(
        conn=conn,
        config=cfg,
        worker_id=worker_id or jobs.default_worker_id(),
        stages=build_stages(),
        media_root=media_dir,
    )
    try:
        if recover_id:
            worker.recover(recover_id)
        if once:
            if not worker.run_once():
                typer.echo("no due jobs")
            return
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        worker.run_forever(stop, poll_seconds=poll_seconds)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    finally:
        conn.close()


def _trusts_every_client(forwarded_allow_ips: str) -> bool:
    """'*' or any prefix-length-0 network (0.0.0.0/0, ::/0, ...) trusts X-Forwarded-For from
    anyone, letting clients spoof their IP and dodge the per-IP login throttle."""
    for part in (p.strip() for p in forwarded_allow_ips.split(",")):
        if part == "*":
            return True
        try:
            if ipaddress.ip_network(part, strict=False).prefixlen == 0:
                return True
        except ValueError:
            continue
    return False


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@app.command("run-review")
def run_review(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    insecure_cookies: Annotated[
        bool,
        typer.Option(
            "--insecure-cookies",
            help=(
                "Drop the Secure cookie flag (plain-HTTP local development only; needs a"
                " loopback --host or CLIPPER_DEV=1)."
            ),
        ),
    ] = False,
    forwarded_allow_ips: Annotated[
        str,
        typer.Option(
            envvar="CLIPPER_FORWARDED_ALLOW_IPS",
            help=(
                "Comma-separated proxy IPs whose X-Forwarded-For is trusted"
                " ('*' and /0 networks refused)."
            ),
        ),
    ] = "127.0.0.1",
    media_dir: Annotated[
        Path, typer.Option(envvar="CLIPPER_MEDIA_DIR", help="Rendered clips (served/exported).")
    ] = jobs.DEFAULT_MEDIA_ROOT,
    output_dir: Annotated[
        Path, typer.Option(envvar="CLIPPER_OUTPUT_DIR", help="Export root (ready/ lives here).")
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Serve the clip review web app."""
    import uvicorn

    from clipper.review.app import create_app

    if _trusts_every_client(forwarded_allow_ips):
        _fail(
            "run-review: --forwarded-allow-ips must list proxy IPs explicitly,"
            " not '*' or a /0 network"
        )
    if insecure_cookies and not _is_loopback(host) and os.environ.get("CLIPPER_DEV") != "1":
        _fail(
            f"run-review: --insecure-cookies is only allowed on a loopback --host"
            f" (got {host!r}) or with CLIPPER_DEV=1"
        )
    try:
        secrets = Secrets()
        application = create_app(
            password_hash=secrets.require("REVIEW_PASSWORD_HASH"),
            session_secret=secrets.require("SESSION_SECRET"),
            db_path=_app(ctx).db_path,
            secure_cookies=not insecure_cookies,
            media_root=media_dir.resolve(),
            output_dir=output_dir.resolve(),
            rclone_remote=secrets.rclone_remote,
        )
    except (MissingSecretError, ValidationError, ValueError) as exc:
        _fail(f"run-review: {exc}")
    _setup_logging()
    uvicorn.run(
        application,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


@app.command("export-pending")
def export_pending_cmd(
    ctx: typer.Context,
    media_dir: Annotated[
        Path, typer.Option(envvar="CLIPPER_MEDIA_DIR", help="Rendered clips.")
    ] = jobs.DEFAULT_MEDIA_ROOT,
    output_dir: Annotated[
        Path, typer.Option(envvar="CLIPPER_OUTPUT_DIR", help="Export root (ready/ lives here).")
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Export (or retry) every approved clip that has not been exported yet."""
    from clipper.export import export_pending
    from clipper.jobs.models import utcnow

    try:
        remote = Secrets().rclone_remote
    except ValidationError as exc:
        _fail(f"export-pending: {exc}")
    _setup_logging()
    conn = _open_db(_app(ctx).db_path)
    try:
        results = export_pending(
            conn,
            output_dir=output_dir.resolve(),
            media_root=media_dir.resolve(),
            now=utcnow(),
            rclone_remote=remote,
        )
    finally:
        conn.close()
    failed = [r for r in results if r.status == "failed"]
    for result in results:
        detail = f": {result.error}" if result.error else ""
        typer.echo(f"clip {result.clip_id} v{result.version}: {result.status}{detail}")
    typer.echo(f"{len(results) - len(failed)} exported, {len(failed)} failed")
    if failed:
        raise typer.Exit(code=1)


@app.command("hash-password")
def hash_password() -> None:
    """Prompt for the review password and print its bcrypt hash for REVIEW_PASSWORD_HASH."""
    import bcrypt

    from clipper.review.app import BCRYPT_MAX_BYTES

    password = typer.prompt(
        "Review password",
        hide_input=True,
        confirmation_prompt=True,
        default="",
        show_default=False,
    )
    raw = password.encode("utf-8")
    if not raw:
        _fail("password must not be empty")
    if len(raw) > BCRYPT_MAX_BYTES:
        _fail(f"password must be at most {BCRYPT_MAX_BYTES} bytes (bcrypt limit)")
    hashed = bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")
    typer.echo(hashed)
    typer.echo(
        "Add it to .env wrapped in single quotes, because Docker Compose interpolates '$':\n"
        "  REVIEW_PASSWORD_HASH='<hash>'",
        err=True,
    )


@app.command("enqueue")
def enqueue(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="YouTube or Twitch video URL.")],
    creator: Annotated[str, typer.Option(help="Creator name to record.")] = MANUAL_CREATOR,
    retry: Annotated[bool, typer.Option(help="Requeue the job if it failed/was skipped.")] = False,
) -> None:
    """Queue a single video for clipping."""
    try:
        ref = parse_source_url(url)
    except InvalidSourceURL as exc:
        _fail(f"invalid URL: {exc}")
    if not re.fullmatch(CREATOR_NAME_PATTERN, creator):
        _fail(f"invalid creator name: {creator!r}")
    conn = _open_db(_app(ctx).db_path)
    requeued = False
    try:
        job, created = jobs.enqueue(conn, ref, jobs.utcnow(), creator=creator)
        if retry and job.status in jobs.RETRYABLE:
            job, requeued = jobs.requeue(conn, job.id, jobs.utcnow()), True
    except (sqlite3.Error, jobs.JobConflict) as exc:
        _fail(f"enqueue failed: {exc}")
    finally:
        conn.close()
    if created:
        typer.echo(f"queued job {job.id}: {ref.platform} {ref.item_id}")
    elif requeued:
        typer.echo(f"requeued job {job.id} ({job.status})")
    elif job.status in jobs.RETRYABLE:
        typer.echo(f"job {job.id} is {job.status} (terminal); use --retry to requeue it")
    elif job.status in jobs.TERMINAL:
        typer.echo(f"job {job.id} is {job.status} (terminal); nothing to do")
    else:
        typer.echo(f"already queued: job {job.id} ({job.status})")


@app.command("bench-transcribe")
def bench_transcribe(
    audio: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Audio file.")
    ],
    models: Annotated[
        str, typer.Option(help="Comma-separated local Whisper model names.")
    ] = "large-v3-turbo,large-v3",
    compute_type: Annotated[str, typer.Option(help="int8, int8_float16, float16, float32.")] = (
        "int8"
    ),
    cpu_threads: Annotated[int, typer.Option(help="CPU threads per model run.")] = 4,
    language: Annotated[
        str | None, typer.Option(help="Language code (e.g. tl); default: auto-detect.")
    ] = None,
) -> None:
    """Compare local Whisper models: real-time factor and a transcript preview each.

    Runs the pipeline's own child process (model load included), on the whole file."""
    import time

    from clipper.config import Settings
    from clipper.jobs import PermanentError
    from clipper.pipeline import transcribe as tr
    from clipper.pipeline.common import MediaError, strip_control

    try:
        settings = [
            Settings(
                whisper_model=m.strip(),
                whisper_compute_type=compute_type,
                whisper_cpu_threads=cpu_threads,
            )
            for m in models.split(",")
        ]
    except ValidationError as exc:
        problems = "; ".join(e["msg"] for e in exc.errors())
        _fail(f"invalid model, compute type or thread count: {problems}")
    if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
        _fail(f"invalid language code: {language!r}")
    try:
        duration = tr.probe_duration(audio)
        if duration <= 0:
            _fail(f"bench-transcribe: {audio.name} has no audio")
        for s in settings:
            backend = tr.FasterWhisperTranscriber(
                model=s.whisper_model,
                compute_type=s.whisper_compute_type,
                cpu_threads=s.whisper_cpu_threads,
            )
            backend.ensure_model()  # a first-run download is not timed
            started = time.monotonic()
            result = backend.transcribe(
                audio.resolve(),
                language=language,
                timeout_s=tr.call_timeout(backend, duration),
            )
            rtf = (time.monotonic() - started) / duration
            text = strip_control(" ".join(seg.text for seg in result.segments))
            typer.echo(f"{s.whisper_model}  rtf={rtf:.3f}  words={len(result.words)}")
            typer.echo(f"  {text[:300]}")
    except (PermanentError, MediaError, tr.BilledChunkError) as exc:
        _fail(f"bench-transcribe: {exc}")


@app.command("resolve")
def resolve() -> None:
    """Resolve creator handles to stable channel / user IDs."""
    _not_implemented("CLP-22")


@app.command("status")
def status() -> None:
    """Show job and clip counts."""
    _not_implemented("CLP-33")


@app.command("retry")
def retry(
    ctx: typer.Context, job_id: Annotated[int, typer.Argument(help="Job ID to retry.")]
) -> None:
    """Re-queue a failed or skipped job."""
    conn = _open_db(_app(ctx).db_path)
    try:
        job = jobs.get_job(conn, job_id)
        if job is None:
            _fail(f"job {job_id} not found")
        if job.status not in jobs.RETRYABLE:
            _fail(f"job {job_id} is {job.status}; only failed/skipped jobs can be retried")
        job = jobs.requeue(conn, job_id, jobs.utcnow())
    except (sqlite3.Error, jobs.JobConflict) as exc:
        _fail(f"retry failed: {exc}")
    finally:
        conn.close()
    typer.echo(f"requeued job {job.id} ({job.status})")
