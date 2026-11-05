"""Meta publishers in the worker and `clipper meta-tokens` (CLP-94): which publishers are
registered, and the token expiry report (names and dates only, never token values)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipper.accounts import load_accounts
from clipper.cli import app
from clipper.config import load_config
from clipper.publish.facebook import FacebookPublisher
from clipper.publish.instagram import InstagramPublisher
from clipper.publish.meta import meta_publishers
from meta_support import FB_TOKEN, IG_ID, IG_TOKEN, PAGE_ID, graph, recorded, reply

runner = CliRunner()

CREATORS_YAML = """\
creators:
  - name: MrBeast
    region: global
    content_type: challenge_stunt
    youtube:
      handle: "@MrBeast"
"""

ACCOUNTS_YAML = """\
accounts:
  - id: fb_main
    platform: facebook
    handle: "@mrbeastclipsdaily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
  - id: ig_main
    platform: instagram
    handle: "@mrbeast.clips.daily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
  - id: tt_main
    platform: tiktok
    handle: "@mrbeast.clips.daily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLIPPER_ACCOUNTS", raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    for name in list(os.environ):
        if name.startswith(("CLIPPER_ACCESS_TOKEN_", "CLIPPER_META_ID_")):
            monkeypatch.delenv(name)


@pytest.fixture
def creators(tmp_path: Path) -> Path:
    path = tmp_path / "creators.yaml"
    path.write_text(CREATORS_YAML)
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    return path


def _in_days(days: int) -> int:
    return int((datetime.now(UTC) + timedelta(days=days)).timestamp())


def _set_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_FB_MAIN", FB_TOKEN)
    monkeypatch.setenv("CLIPPER_META_ID_FB_MAIN", PAGE_ID)
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_IG_MAIN", IG_TOKEN)
    monkeypatch.setenv("CLIPPER_META_ID_IG_MAIN", IG_ID)


def test_publishers_are_registered_for_meta_platforms_with_accounts(creators: Path) -> None:
    accounts = load_accounts(creators.parent / "accounts.yaml", load_config(creators)).accounts

    with httpx.Client() as http:
        publishers = meta_publishers(accounts, http)

    assert set(publishers) == {"facebook", "instagram"}
    assert isinstance(publishers["facebook"], FacebookPublisher)
    assert isinstance(publishers["instagram"], InstagramPublisher)


def test_no_meta_accounts_no_meta_publishers() -> None:
    with httpx.Client() as http:
        assert meta_publishers([], http) == {}


def test_disabled_accounts_do_not_register_a_publisher(creators: Path) -> None:
    accounts = load_accounts(creators.parent / "accounts.yaml", load_config(creators)).accounts
    disabled = [a.model_copy(update={"enabled": False}) for a in accounts]

    with httpx.Client() as http:
        assert meta_publishers(disabled, http) == {}


@respx.mock
def test_meta_tokens_reports_expiry(creators: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_tokens(monkeypatch)
    far = recorded("debug_token_page")
    far["data"]["data_access_expires_at"] = _in_days(80)
    respx.get(graph("debug_token")).mock(return_value=httpx.Response(200, json=far))

    result = runner.invoke(app, ["--config", str(creators), "meta-tokens"])

    assert result.exit_code == 0, result.output
    assert "fb_main facebook: ok" in result.output
    assert "ig_main instagram: ok" in result.output
    assert "never expires" in result.output and "data access until" in result.output
    assert "tt_main" not in result.output
    assert FB_TOKEN not in result.output and IG_TOKEN not in result.output


@respx.mock
def test_meta_tokens_fails_on_a_token_expiring_soon(
    creators: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    soon = recorded("debug_token_page")
    soon["data"]["data_access_expires_at"] = _in_days(3)
    respx.get(graph("debug_token")).mock(return_value=httpx.Response(200, json=soon))

    result = runner.invoke(app, ["--config", str(creators), "meta-tokens"])

    assert result.exit_code == 1
    assert "expires soon" in result.output


@respx.mock
def test_meta_tokens_fails_on_missing_or_invalid_tokens(
    creators: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_FB_MAIN", FB_TOKEN)
    monkeypatch.setenv("CLIPPER_META_ID_FB_MAIN", PAGE_ID)
    respx.get(graph("debug_token")).mock(return_value=reply("debug_token_invalid"))

    result = runner.invoke(app, ["--config", str(creators), "meta-tokens"])

    assert result.exit_code == 1
    assert "fb_main facebook: invalid" in result.output
    assert "ig_main instagram: missing CLIPPER_ACCESS_TOKEN_IG_MAIN" in result.output
    assert FB_TOKEN not in result.output


@respx.mock
def test_meta_tokens_reports_a_failed_check(
    creators: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    respx.get(graph("debug_token")).mock(return_value=reply("error_transient", 500))

    result = runner.invoke(app, ["--config", str(creators), "meta-tokens"])

    assert result.exit_code == 1
    assert "check failed" in result.output


def test_meta_tokens_without_meta_accounts(tmp_path: Path) -> None:
    path = tmp_path / "creators.yaml"
    path.write_text(CREATORS_YAML)

    result = runner.invoke(app, ["--config", str(path), "meta-tokens"])

    assert result.exit_code == 0
    assert "no facebook or instagram accounts" in result.output
