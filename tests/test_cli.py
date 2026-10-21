from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from clipper.cli import app
from clipper.db import connect

runner = CliRunner()

COMMANDS = [
    "run-monitor",
    "run-worker",
    "run-review",
    "enqueue",
    "resolve",
    "status",
    "retry",
    "migrate",
    "check-config",
]


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_check_config_ok(config_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(config_file), "check-config"])

    assert result.exit_code == 0
    assert "3 creators" in result.output


def test_check_config_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("creators:\n  - name: X\n    region: global\n")

    result = runner.invoke(app, ["--config", str(bad), "check-config"])

    assert result.exit_code == 1
    assert "at least one platform" in result.stderr


def test_migrate_creates_db(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "state.db"

    first = runner.invoke(app, ["--db", str(db_path), "migrate"])
    second = runner.invoke(app, ["--db", str(db_path), "migrate"])

    assert first.exit_code == 0, first.output
    assert "applied" in first.output
    assert db_path.exists()
    assert second.exit_code == 0
    assert "up to date" in second.output


def test_unimplemented_command_exits_nonzero() -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 70
    assert "not implemented yet" in result.stderr


def test_migrate_reports_db_errors_cleanly(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    result = runner.invoke(app, ["--db", str(blocker / "state.db"), "migrate"])

    assert result.exit_code == 1
    assert "migrate failed" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_enqueue_creates_one_job_per_url(tmp_path: Path) -> None:
    db = ["--db", str(tmp_path / "state.db")]

    first = runner.invoke(app, [*db, "enqueue", "https://youtu.be/dQw4w9WgXcQ"])
    again = runner.invoke(app, [*db, "enqueue", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3"])

    assert first.exit_code == 0, first.output
    assert "queued job 1: youtube dQw4w9WgXcQ" in first.output
    assert again.exit_code == 0
    assert "already queued: job 1 (detected)" in again.output


def test_enqueue_rejects_invalid_url(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    result = runner.invoke(
        app, ["--db", str(db_path), "enqueue", "https://www.youtube.com/shorts/dQw4w9WgXcQ"]
    )

    assert result.exit_code == 1
    assert "invalid URL: unsupported YouTube URL" in result.stderr
    assert not db_path.exists()


def test_enqueue_rejects_bad_creator(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--db",
            str(tmp_path / "s.db"),
            "enqueue",
            "https://youtu.be/dQw4w9WgXcQ",
            "--creator",
            "../x",
        ],
    )

    assert result.exit_code == 1
    assert "invalid creator" in result.stderr


def test_run_worker_once(tmp_path: Path, config_file: Path) -> None:
    opts = ["--db", str(tmp_path / "state.db"), "--config", str(config_file)]
    runner.invoke(app, [*opts, "enqueue", "https://www.twitch.tv/videos/123"])

    worked = runner.invoke(app, [*opts, "run-worker", "--once", "--media-dir", str(tmp_path)])
    idle = runner.invoke(app, [*opts, "run-worker", "--once", "--media-dir", str(tmp_path)])

    assert worked.exit_code == 0, worked.output
    assert idle.exit_code == 0
    assert "no due jobs" in idle.output  # fetching_audio has no stage yet (CLP-13)


def test_run_worker_requires_valid_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config", str(tmp_path / "missing.yaml"), "run-worker", "--once"]
    )

    assert result.exit_code == 1
    assert "config file not found" in result.stderr


def test_db_open_errors_are_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    result = runner.invoke(
        app, ["--db", str(blocker / "s.db"), "enqueue", "https://youtu.be/dQw4w9WgXcQ"]
    )

    assert result.exit_code == 1
    assert "cannot open database" in result.stderr


def test_enqueue_terminal_job_needs_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = ["--db", str(db_path)]
    url = "https://youtu.be/dQw4w9WgXcQ"
    runner.invoke(app, [*db, "enqueue", url])
    with closing(connect(db_path)) as conn:
        conn.execute("UPDATE jobs SET status = 'failed', attempts = 5, last_error = 'x'")

    plain = runner.invoke(app, [*db, "enqueue", url])
    retried = runner.invoke(app, [*db, "enqueue", url, "--retry"])
    again = runner.invoke(app, [*db, "enqueue", url, "--retry"])

    assert "job 1 is failed (terminal); use --retry" in plain.output
    assert retried.exit_code == 0
    assert "requeued job 1 (detected)" in retried.output
    assert "already queued: job 1 (detected)" in again.output
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT attempts, last_error FROM jobs").fetchone()
    assert (row["attempts"], row["last_error"]) == (0, None)


def test_enqueue_finished_job_reports_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    url = "https://youtu.be/dQw4w9WgXcQ"
    runner.invoke(app, ["--db", str(db_path), "enqueue", url])
    with closing(connect(db_path)) as conn:
        conn.execute("UPDATE jobs SET status = 'clips_ready'")

    result = runner.invoke(app, ["--db", str(db_path), "enqueue", url, "--retry"])

    assert "job 1 is clips_ready (terminal); nothing to do" in result.output


def test_run_worker_recover_id(tmp_path: Path, config_file: Path) -> None:
    db_path = tmp_path / "state.db"
    opts = ["--db", str(db_path), "--config", str(config_file)]
    runner.invoke(app, [*opts, "enqueue", "https://www.twitch.tv/videos/123"])
    with closing(connect(db_path)) as conn:
        conn.execute("UPDATE jobs SET worker_id = 'dead', lease_until = '2999-01-01T00:00:00Z'")

    blocked = runner.invoke(app, [*opts, "run-worker", "--once"])
    recovered = runner.invoke(app, [*opts, "run-worker", "--once", "--recover-id", "dead"])

    assert "no due jobs" in blocked.output
    assert recovered.exit_code == 0, recovered.output
    assert "no due jobs" not in recovered.output


def test_retry_requeues_failed_or_skipped_job(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = ["--db", str(db_path)]
    runner.invoke(app, [*db, "enqueue", "https://youtu.be/dQw4w9WgXcQ"])
    with closing(connect(db_path)) as conn:
        conn.execute("UPDATE jobs SET status = 'skipped', skip_reason = 'too short'")

    result = runner.invoke(app, [*db, "retry", "1"])
    again = runner.invoke(app, [*db, "retry", "1"])

    assert result.exit_code == 0, result.output
    assert "requeued job 1 (detected)" in result.output
    assert again.exit_code == 1
    assert "job 1 is detected; only failed/skipped jobs can be retried" in again.stderr
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT status, skip_reason FROM jobs").fetchone()
    assert (row["status"], row["skip_reason"]) == ("detected", None)


def test_retry_missing_job_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--db", str(tmp_path / "state.db"), "retry", "42"])

    assert result.exit_code == 1
    assert "job 42 not found" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
