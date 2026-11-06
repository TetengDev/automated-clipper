"""Worker wiring for campaign submissions (CLP-97): the live campaign view and the Discord
notifiers the run-worker command builds."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import typer

from clipper import cli
from clipper.campaigns import Campaign, LiveCampaigns
from clipper.config import ConfigError
from clipper.notify import DiscordWebhook, log_notify

PINGS = "https://discord.com/api/webhooks/1/pings-token"
ALERTS = "https://discord.com/api/webhooks/2/alerts-token"


def _campaign(campaign_id: str) -> Campaign:
    return Campaign.model_validate(
        {"id": campaign_id, "program": "reach", "creator": "Kai Cenat", "platforms": ["tiktok"]}
    )


class FakeConfig:
    def __init__(self) -> None:
        self.campaigns: dict[str, Campaign] = {"a": _campaign("a")}
        self.broken = False

    def current(self) -> tuple[dict[str, Any], dict[str, Campaign]]:
        if self.broken:
            raise ConfigError("campaigns.yaml: bad")
        return {}, self.campaigns


def test_live_campaigns_follow_the_config() -> None:
    config = FakeConfig()
    live = LiveCampaigns(config)  # type: ignore[arg-type]

    assert list(live) == ["a"] and len(live) == 1 and live["a"].id == "a"
    config.campaigns = {"b": _campaign("b")}
    assert "a" not in live and live.get("b") is not None
    config.broken = True
    with pytest.raises(ConfigError):
        live.get("b")


@pytest.fixture
def http() -> httpx.Client:
    return httpx.Client()


def test_notifiers_fall_back_to_the_log(
    monkeypatch: pytest.MonkeyPatch, http: httpx.Client
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("DISCORD_ALERTS_WEBHOOK_URL", "")

    assert cli._notifiers(http) == (log_notify, log_notify)


def test_alerts_use_the_ping_webhook_when_unset(
    monkeypatch: pytest.MonkeyPatch, http: httpx.Client
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", PINGS)
    monkeypatch.setenv("DISCORD_ALERTS_WEBHOOK_URL", "")

    notify, alert = cli._notifiers(http)

    assert isinstance(notify, DiscordWebhook) and alert is notify


def test_separate_alerts_webhook(monkeypatch: pytest.MonkeyPatch, http: httpx.Client) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", PINGS)
    monkeypatch.setenv("DISCORD_ALERTS_WEBHOOK_URL", ALERTS)

    notify, alert = cli._notifiers(http)

    assert isinstance(alert, DiscordWebhook) and alert is not notify


def test_a_malformed_webhook_stops_the_worker_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, http: httpx.Client, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://evil.example/hook-secret")

    with pytest.raises(typer.Exit):
        cli._notifiers(http)

    err = capsys.readouterr().err
    assert "DISCORD_WEBHOOK_URL" in err and "hook-secret" not in err
