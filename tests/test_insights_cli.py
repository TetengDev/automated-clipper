"""`clipper insights collect | weekly | tag` and run-monitor's hourly insights hook
(CLP-154). HTTP goes through an injected MockTransport: tests never reach the network."""

from __future__ import annotations

import json
import logging
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from clipper import cli, notify
from clipper.cli import _after_cycle, app
from clipper.db import connect
from clipper.insights import repo
from clipper.monitor import YouTubeMonitor
from config_support import VALID_YAML

runner = CliRunner()

ACCOUNTS_YAML = """\
accounts:
  - id: random_gaming_yt
    platform: youtube
    content: commentary
    channel_id: UCcMYw3Ae2crqXE2yzFBSUig
    handle: "@RandomGaming-f3w"
    market: US
    timezone: America/New_York
"""
REFRESH_MARKER = "1//refresh-marker-value-0000"
SECRET_MARKER = "GOCSPX-secret-marker-0000"
KEY_MARKER = "AIzaSy-key-marker-value-0000"
CLIENT_MARKER = "client-id-marker-0000.apps.googleusercontent.com"
WEBHOOK = "https://discord.com/api/webhooks/1/pings-token"


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request the CLI makes, answered by `handler` (default: HTTP 599)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(599)

    monkeypatch.setattr(
        cli, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return seen


@pytest.fixture
def args(tmp_path: Path, requests: list[httpx.Request]) -> list[str]:
    (tmp_path / "creators.yaml").write_text(VALID_YAML)
    return ["--config", str(tmp_path / "creators.yaml"), "--db", str(tmp_path / "cli.db")]


def test_collect_without_credentials_flags_no_token_and_prints_no_secrets(
    tmp_path: Path,
    args: list[str],
    requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    monkeypatch.setenv("CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT", REFRESH_MARKER)
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", SECRET_MARKER)

    with caplog.at_level(logging.INFO):
        result = runner.invoke(app, [*args, "insights", "collect"])

    assert result.exit_code == 0, result.output
    assert "insights: 0 channel row(s), 0 video row(s), 0 error(s)" in result.output
    for marker in (REFRESH_MARKER, SECRET_MARKER):
        assert marker not in result.output and marker not in caplog.text
    with closing(connect(tmp_path / "cli.db")) as conn:
        row = conn.execute("SELECT analytics, detail FROM insights_state").fetchone()
    assert row["analytics"] == "no_token" and "YOUTUBE_OAUTH_CLIENT_ID" in row["detail"]
    assert requests == []


def test_refused_credentials_never_leak_a_key_or_token(
    tmp_path: Path,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY_MARKER)
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", CLIENT_MARKER)
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", SECRET_MARKER)
    monkeypatch.setenv("CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT", REFRESH_MARKER)
    hosts: list[str] = []

    def google_says_no(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "revoked"}
            )
        error = {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}
        return httpx.Response(403, json={"error": error})

    monkeypatch.setattr(
        cli, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(google_says_no))
    )

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(app, [*args, "insights", "collect"])

    assert result.exit_code == 1  # the public step failed
    assert "quotaExceeded" in result.output
    assert set(hosts) == {"www.googleapis.com", "oauth2.googleapis.com"}
    with closing(connect(tmp_path / "cli.db")) as conn:
        state = conn.execute("SELECT analytics, detail FROM insights_state").fetchone()
        stored = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM runs")])
    assert state["analytics"] == "needs_reauth"
    assert "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT" in state["detail"]
    seen = result.output + caplog.text + stored + state["detail"]
    for marker in (KEY_MARKER, SECRET_MARKER, REFRESH_MARKER, CLIENT_MARKER):
        assert marker not in seen


def test_without_youtube_api_key_public_numbers_are_skipped_by_name(
    tmp_path: Path,
    args: list[str],
    requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)

    with caplog.at_level(logging.INFO):
        result = runner.invoke(app, [*args, "insights", "collect"])

    assert result.exit_code == 0, result.output
    notices = [r.getMessage() for r in caplog.records if "YOUTUBE_API_KEY" in r.getMessage()]
    assert notices == [
        "insights: YOUTUBE_API_KEY is not set; public numbers and hand uploads are off"
    ]
    assert requests == []


def test_collect_without_commentary_accounts(args: list[str]) -> None:
    result = runner.invoke(app, [*args, "insights", "collect"])

    assert result.exit_code == 0
    assert "no enabled YouTube commentary accounts" in result.output


def test_weekly_prints_the_summary(
    tmp_path: Path,
    args: list[str],
    requests: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)

    with caplog.at_level(logging.INFO):
        result = runner.invoke(app, [*args, "insights", "weekly"])

    assert result.exit_code == 0, result.output
    assert "Weekly insights for random_gaming_yt" in result.output
    assert "Subscribers: no data yet" in result.output
    assert "YOUTUBE_API_KEY" not in caplog.text  # printing needs no API client
    assert requests == []


def test_weekly_without_commentary_accounts(args: list[str]) -> None:
    result = runner.invoke(app, [*args, "insights", "weekly"])

    assert result.exit_code == 0
    assert "no enabled YouTube commentary accounts" in result.output


def test_the_owner_tags_a_hand_uploaded_short(tmp_path: Path, args: list[str]) -> None:
    result = runner.invoke(
        app, [*args, "insights", "tag", "2BpbZneO_To", "hook_type=Question", "topic=GTA VI"]
    )

    assert result.exit_code == 0, result.output
    assert "hook_type=question" in result.output and "topic=gta vi" in result.output
    assert "palette=unknown" in result.output
    assert "not known to the insights job yet" in result.output
    with closing(connect(tmp_path / "cli.db")) as conn:
        tags = repo.tags_for_videos(conn, ["2BpbZneO_To"])["2BpbZneO_To"]
    assert tags["hook_type"] == "question" and tags["structure"] == "unknown"


@pytest.mark.parametrize(
    ("words", "error"),
    [
        (["2BpbZneO_To", "mood=happy"], "unknown tag key 'mood'"),
        (["bad id!", "topic=x"], "not a YouTube video id"),
        (["2BpbZneO_To"], "key=value"),
    ],
)
def test_bad_tags_are_refused(args: list[str], words: list[str], error: str) -> None:
    result = runner.invoke(app, [*args, "insights", "tag", *words])

    assert result.exit_code == 1
    assert error in result.stderr


def test_after_cycle_runs_each_hook_in_order() -> None:
    calls: list[str] = []

    _after_cycle(lambda: calls.append("janitor"), None, lambda: calls.append("insights"))()

    assert calls == ["janitor", "insights"]


def test_a_failing_hook_is_logged_and_the_next_one_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def janitor() -> None:
        raise RuntimeError("disk gone")

    _after_cycle(janitor, lambda: calls.append("insights"))()

    assert calls == ["insights"]
    assert "run-monitor: an after-cycle hook failed: RuntimeError: disk gone" in caplog.text


def test_run_monitor_calls_the_insights_tick_after_each_cycle(
    tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    monkeypatch.setenv("CLIPPER_MEDIA_DIR", str(tmp_path / "media"))
    ticks: list[str] = []

    class FakeInsights:
        def tick(self) -> None:
            ticks.append("insights")

    monkeypatch.setattr(cli, "_insights", lambda *a, **k: FakeInsights())
    monkeypatch.setattr(cli, "_youtube_source", lambda secrets, http, cancel: object())

    def one_round(self: YouTubeMonitor, creators) -> None:
        assert self.janitor is not None
        self.janitor()

    monkeypatch.setattr(YouTubeMonitor, "run_forever", one_round)

    result = runner.invoke(app, [*args, "run-monitor"])

    assert result.exit_code == 0, result.output
    assert ticks == ["insights"]


@pytest.mark.parametrize("webhook", [True, False])
def test_run_monitor_sends_the_weekly_summary_to_the_ping_webhook_built_once(
    tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch, webhook: bool
) -> None:
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    monkeypatch.setenv("CLIPPER_MEDIA_DIR", str(tmp_path / "media"))
    if webhook:
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    built: list[notify.DiscordWebhook] = []

    class CountedWebhook(notify.DiscordWebhook):
        def __init__(self, url: str, http: httpx.Client) -> None:
            super().__init__(url, http)
            built.append(self)

    monkeypatch.setattr(notify, "DiscordWebhook", CountedWebhook)
    collectors: list[object] = []
    real = cli._insights

    def spy(*a: object, **k: object) -> object:
        collector = real(*a, **k)  # type: ignore[arg-type]
        collectors.append(collector)
        return collector

    monkeypatch.setattr(cli, "_insights", spy)
    monkeypatch.setattr(cli, "_youtube_source", lambda secrets, http, cancel: object())
    monkeypatch.setattr(YouTubeMonitor, "run_forever", lambda self, creators: None)

    result = runner.invoke(app, [*args, "run-monitor"])

    assert result.exit_code == 0, result.output
    (collector,) = collectors
    if webhook:
        assert len(built) == 1 and collector.notify is built[0]  # type: ignore[attr-defined]
    else:
        assert built == [] and collector.notify is notify.log_notify  # type: ignore[attr-defined]
