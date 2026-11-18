from functools import partial
from pathlib import Path

import httpx
import pytest

from clipper import cli, doctor
from clipper.pipeline.ytdlp import RunResult

STARTUP_CHECK = doctor.startup_check
ALERTS_WEBHOOK = "https://discord.com/api/webhooks/2/alerts-token"

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
        "YOUTUBE_OAUTH_CLIENT_ID",
        "YOUTUBE_OAUTH_CLIENT_SECRET",
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


@pytest.fixture(autouse=True)
def _no_boot_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """run-worker and run-monitor run doctor's Deno solver check at boot (CLP-129); tests
    never start the real Deno. A test of the real check imports `startup_check` by name at
    module level (as test_doctor.py does), before this stub replaces the module attribute."""
    monkeypatch.setattr(doctor, "startup_check", lambda service, alert: None)


@pytest.fixture
def failing_boot_check(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real boot check with a Deno that exits 1, and DISCORD_ALERTS_WEBHOOK_URL set to a
    mock webhook. Returns the messages the webhook received."""
    sent: list[str] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ALERTS_WEBHOOK
        sent.append(request.read().decode())
        return httpx.Response(204)

    def deno_exits_1(args: list[str], timeout_s: float, **_: object) -> RunResult:
        return RunResult(1, "", "")

    check = partial(
        STARTUP_CHECK,
        runner=deno_exits_1,
        which={"deno": "/usr/bin/deno"}.get,
        platform="linux",
        scripts_dir=Path(__file__).parent / "no-such-scripts-dir",
    )
    monkeypatch.setattr(doctor, "startup_check", check)
    monkeypatch.setenv("DISCORD_ALERTS_WEBHOOK_URL", ALERTS_WEBHOOK)
    monkeypatch.setattr(
        cli, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(webhook))
    )
    return sent
