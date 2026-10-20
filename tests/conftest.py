from pathlib import Path

import pytest

VALID_YAML = """\
settings:
  min_duration_seconds: 300
  max_clips_per_source: 3
creators:
  - name: MrBeast
    region: global
    youtube:
      handle: "@MrBeast"
  - name: Kai Cenat
    region: global
    twitch:
      login: kaicenat
    youtube:
      handle: "@KaiCenat"
  - name: Raffy Tulfo in Action
    region: ph
    language: tl
    youtube:
      handle: "@RaffyTulfoInAction"
    overrides:
      min_duration_seconds: 600
"""


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
        "CLIPPER_DB",
        "RCLONE_REMOTE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
