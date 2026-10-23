"""CLI wiring for the review app's media/export settings and `export-pending` (CLP-21)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from clipper import clips
from clipper.cli import app as cli_app
from review_support import PASSWORD_HASH, SECRET, Env, make_env

runner = CliRunner()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> dict[str, FastAPI]:
    calls: dict[str, FastAPI] = {}
    monkeypatch.setattr("uvicorn.run", lambda application, **_kw: calls.update(app=application))
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("CLIPPER_CONFIG", str(config_file))
    return calls


def test_run_review_loads_the_creator_config(captured: dict[str, FastAPI], tmp_path: Path) -> None:
    """CLP-87 review M2: the app needs each creator's content type for the faces check."""
    result = runner.invoke(cli_app, ["--db", str(tmp_path / "x.db"), "run-review"])

    assert result.exit_code == 0, result.output
    config = captured["app"].state.config
    assert [c.name for c in config.creators] == ["MrBeast", "Kai Cenat", "Raffy Tulfo in Action"]


def test_run_review_refuses_a_missing_config(
    captured: dict[str, FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIPPER_CONFIG", str(tmp_path / "nope.yaml"))

    result = runner.invoke(cli_app, ["--db", str(tmp_path / "x.db"), "run-review"])

    assert result.exit_code == 1
    assert "run-review: config file not found" in result.output
    assert "app" not in captured


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def test_run_review_threads_dirs_and_remote(
    captured: dict[str, FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIPPER_MEDIA_DIR", "rendered")
    monkeypatch.setenv("RCLONE_REMOTE", "gdrive:clips")

    result = runner.invoke(
        cli_app, ["--db", str(tmp_path / "x.db"), "run-review", "--output-dir", "out"]
    )

    assert result.exit_code == 0, result.output
    state = captured["app"].state
    assert state.media_root == (tmp_path / "rendered").resolve()
    assert state.output_dir == (tmp_path / "out").resolve()
    assert state.media_root.is_absolute() and state.output_dir.is_absolute()
    assert state.rclone_remote == "gdrive:clips"


def test_run_review_defaults(captured: dict[str, FastAPI], tmp_path: Path) -> None:
    result = runner.invoke(cli_app, ["--db", str(tmp_path / "x.db"), "run-review"])

    assert result.exit_code == 0, result.output
    state = captured["app"].state
    assert state.media_root == (tmp_path / "data" / "media").resolve()
    assert state.output_dir == (tmp_path / "output").resolve()
    assert state.rclone_remote is None


def test_run_review_rejects_bad_rclone_remote(
    captured: dict[str, FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RCLONE_REMOTE", "--config=/etc/x")

    result = runner.invoke(cli_app, ["--db", str(tmp_path / "x.db"), "run-review"])

    assert result.exit_code == 1
    assert "app" not in captured


def test_export_pending_exports_and_reports(env: Env) -> None:
    (env.media_root / "a.mp4").write_bytes(b"video")
    good = env.clip(video_path="a.mp4")
    clips.approve(env.conn, good, datetime(2027, 1, 15, tzinfo=UTC))

    result = runner.invoke(
        cli_app,
        [
            "--db",
            str(env.db_path),
            "export-pending",
            "--media-dir",
            str(env.media_root),
            "--output-dir",
            str(env.output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"clip {good} v1: exported" in result.output
    assert "1 exported, 0 failed" in result.output
    assert env.status(good) == "exported"
    assert len(list((env.output_dir / "ready").rglob("*.mp4"))) == 1


def test_export_pending_exits_nonzero_on_failure(env: Env) -> None:
    bad = env.clip(video_path="missing.mp4")
    clips.approve(env.conn, bad, datetime(2027, 1, 15, tzinfo=UTC))

    result = runner.invoke(
        cli_app,
        ["--db", str(env.db_path), "export-pending", "--media-dir", str(env.media_root)],
    )

    assert result.exit_code == 1
    assert "failed: ExportError" in result.output
    assert env.status(bad) == "approved"


def test_export_pending_rejects_bad_rclone_remote(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCLONE_REMOTE", "-bad")

    result = runner.invoke(cli_app, ["--db", str(env.db_path), "export-pending"])

    assert result.exit_code == 1
    assert "export-pending:" in result.output
