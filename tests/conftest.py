from pathlib import Path

import pytest

from config_support import VALID_YAML


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "creators.yaml"
    path.write_text(VALID_YAML)
    return path


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real .env and secrets out of tests."""
    for name in (
        "YOUTUBE_API_KEY",
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_ALERTS_WEBHOOK_URL",
        "SESSION_SECRET",
        "REVIEW_PASSWORD_HASH",
        "CLIPPER_CONFIG",
        "CLIPPER_ACCOUNTS",
        "CLIPPER_CAMPAIGNS",
        "CLIPPER_DB",
        "RCLONE_REMOTE",
        "CLIPPER_MEDIA_DIR",
        "CLIPPER_OUTPUT_DIR",
        "CLIPPER_WORKER_ID",
        "CLIPPER_FORWARDED_ALLOW_IPS",
        "CLIPPER_DEV",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
