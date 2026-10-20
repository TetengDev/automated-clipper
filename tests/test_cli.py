from pathlib import Path

from typer.testing import CliRunner

from clipper.cli import app

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
    result = runner.invoke(app, ["run-worker"])

    assert result.exit_code == 70
    assert "not implemented yet" in result.stderr


def test_migrate_reports_db_errors_cleanly(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")

    result = runner.invoke(app, ["--db", str(blocker / "state.db"), "migrate"])

    assert result.exit_code == 1
    assert "migrate failed" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
