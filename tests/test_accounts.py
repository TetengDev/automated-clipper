"""Posting accounts config: accounts.yaml, licence rule, token secrets (CLP-91)."""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from clipper.accounts import (
    APP_ENV_NAMES,
    PLATFORM_DAILY_CAPS,
    Account,
    AccountsConfig,
    AccountTokens,
    account_tokens,
    accounts_path,
    default_plan,
    load_accounts,
    token_env_name,
)
from clipper.cli import app
from clipper.config import AppConfig, ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

CREATORS_YAML = """\
creators:
  - name: MrBeast
    region: global
    content_type: challenge_stunt
    youtube:
      handle: "@MrBeast"
  - name: Raffy Tulfo in Action
    region: ph
    language: tl
    content_type: public_service
    youtube:
      handle: "@RaffyTulfoInAction"
  - name: Ivana Alawi
    region: ph
    language: tl
    content_type: vlog_lifestyle
    youtube:
      handle: "@IvanaAlawi"
  - name: Cong TV
    region: ph
    language: tl
    content_type: comedy
    youtube:
      handle: "@CongTheVlogger"
  - name: No Type
    region: global
    youtube:
      handle: "@NoType"
"""

ACCOUNTS_YAML = """\
accounts:
  - id: mrbeast_clips_tt
    platform: tiktok
    handle: "@mrbeast.clips.daily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
    posts_per_day: 3
    min_gap_minutes: 180
    windows: ["14:00-17:00", "18:00-22:00"]
    warmup_start: 2026-11-01
    campaign: vyro_mrbeast
  - id: mrbeast_clips_yt
    platform: youtube
    handle: "@MrBeastClipsDaily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
"""

TOKEN = "tok_a1b2c3d4e5f6g7h8"
REFRESH = "ref_z9y8x7w6v5u4t3s2"


@pytest.fixture(autouse=True)
def _no_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPPER_ACCOUNTS", raising=False)
    for name in list(os.environ):
        if name.startswith(("CLIPPER_ACCESS_TOKEN_", "CLIPPER_REFRESH_TOKEN_")):
            monkeypatch.delenv(name)


@pytest.fixture
def creators_cfg(tmp_path: Path) -> AppConfig:
    path = tmp_path / "creators.yaml"
    path.write_text(CREATORS_YAML)
    return load_config(path)


def _write(tmp_path: Path, text: str, name: str = "accounts.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _account(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "acct_one",
        "platform": "tiktok",
        "handle": "@acct.one",
        "market": "US",
        "timezone": "America/New_York",
        "creators": ["MrBeast"],
    }
    base.update(overrides)
    return base


# --- AC1: accounts.yaml loads, validates and is summarised by check-config ---------------


def test_accounts_yaml_loads(tmp_path: Path, creators_cfg: AppConfig) -> None:
    cfg = load_accounts(_write(tmp_path, ACCOUNTS_YAML), creators_cfg)

    assert isinstance(cfg, AccountsConfig)
    assert [a.id for a in cfg.accounts] == ["mrbeast_clips_tt", "mrbeast_clips_yt"]
    tt = cfg.accounts[0]
    assert tt.platform == "tiktok"
    assert tt.posts_per_day == 3
    assert tt.min_gap_minutes == 180
    assert tt.windows == ["14:00-17:00", "18:00-22:00"]
    assert tt.warmup_start == date(2026, 11, 1)
    assert tt.warmup_days == 14
    assert tt.campaign == "vyro_mrbeast"
    assert tt.licence is None
    assert tt.enabled is True


def test_missing_accounts_file_is_empty_config(tmp_path: Path, creators_cfg: AppConfig) -> None:
    cfg = load_accounts(tmp_path / "nope.yaml", creators_cfg)

    assert cfg.accounts == []


def test_omitted_plan_uses_default_for_platform_and_market(
    tmp_path: Path, creators_cfg: AppConfig
) -> None:
    yt = load_accounts(_write(tmp_path, ACCOUNTS_YAML), creators_cfg).accounts[1]

    assert yt.posts_per_day == 2
    assert yt.min_gap_minutes == 240
    assert yt.windows == ["10:00-11:00", "13:00-14:00"]


@pytest.mark.parametrize(
    ("platform", "market", "posts", "gap", "windows"),
    [
        ("tiktok", "US", 3, 180, ["14:00-17:00", "18:00-22:00"]),
        ("youtube", "US", 2, 240, ["10:00-11:00", "13:00-14:00"]),
        ("instagram", "US", 2, 240, ["10:00-15:00"]),
        ("facebook", "US", 2, 240, ["10:00-15:00"]),
        ("tiktok", "PH", 2, 180, ["07:00-09:00", "12:00-13:00", "18:00-21:00"]),
        ("youtube", "PH", 2, 180, ["07:00-09:00", "12:00-13:00", "18:00-21:00"]),
        ("instagram", "PH", 2, 180, ["07:00-09:00", "12:00-13:00", "18:00-21:00"]),
        ("facebook", "PH", 2, 180, ["07:00-09:00", "12:00-13:00", "18:00-21:00"]),
    ],
)
def test_default_plan(platform: str, market: str, posts: int, gap: int, windows: list[str]) -> None:
    plan = default_plan(platform, market)  # type: ignore[arg-type]

    assert plan.posts_per_day == posts
    assert plan.min_gap_minutes == gap
    assert list(plan.windows) == windows


def test_market_without_default_plan_needs_explicit_plan() -> None:
    with pytest.raises(ValidationError, match="no default posting plan for market 'GB'"):
        Account.model_validate(_account(market="GB"))

    explicit = Account.model_validate(
        _account(market="GB", posts_per_day=1, min_gap_minutes=60, windows=["09:00-10:00"])
    )
    assert explicit.posts_per_day == 1


def test_platform_daily_caps() -> None:
    assert PLATFORM_DAILY_CAPS == {"tiktok": 15, "facebook": 30, "instagram": 50, "youtube": 100}


def test_posts_per_day_over_platform_cap_is_refused() -> None:
    Account.model_validate(_account(posts_per_day=15, min_gap_minutes=30))
    with pytest.raises(ValidationError, match="posts_per_day 16 exceeds the tiktok cap of 15"):
        Account.model_validate(_account(posts_per_day=16, min_gap_minutes=30))
    with pytest.raises(ValidationError):
        Account.model_validate(_account(posts_per_day=0))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "Bad-Id", "id"),
        ("platform", "myspace", "platform"),
        ("market", "usa", "market"),
        ("timezone", "Mars/Olympus_Mons", "unknown timezone"),
        ("min_gap_minutes", -1, "min_gap_minutes"),
        ("windows", ["9:00-10:00"], "HH:MM-HH:MM"),
        ("windows", ["25:00-26:00"], "HH:MM-HH:MM"),
        ("windows", ["17:00-14:00"], "start must be before end"),
        ("windows", ["10:00-12:00", "11:00-13:00"], "overlap"),
        ("windows", [], "windows"),
        ("windows", ["10:00-11:00\n"], "HH:MM-HH:MM"),
        ("market", False, "quote the country code"),
        ("market", "US\n", "market"),
        ("creators", ["MrBeast", "mrbeast"], "duplicate creator"),
        ("licence", "  ", "at least 3 characters"),
        ("licence", "ok\u202elicence", "invisible"),
        ("licence", "ok\x9b31m licence", "invisible"),
        ("licence", "line one\u2028line two", "invisible"),
        ("creators", [], "creators"),
        ("warmup_days", 0, "warmup_days"),
        ("surprise", 1, "surprise"),
    ],
)
def test_account_field_validation(field: str, value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Account.model_validate(_account(**{field: value}))


def test_touching_windows_are_not_an_overlap() -> None:
    account = Account.model_validate(
        _account(windows=["10:00-12:00", "12:00-13:00"], min_gap_minutes=60)
    )

    assert account.windows == ["10:00-12:00", "12:00-13:00"]


def test_partial_plan_fills_the_rest_from_the_default() -> None:
    account = Account.model_validate(_account(posts_per_day=2, windows=None))

    assert account.posts_per_day == 2
    assert account.min_gap_minutes == 180
    assert account.windows == ["14:00-17:00", "18:00-22:00"]


def test_plan_that_cannot_fit_is_refused() -> None:
    with pytest.raises(ValidationError, match="posts_per_day 15 cannot fit.*hold only 3"):
        Account.model_validate(_account(posts_per_day=15))
    with pytest.raises(ValidationError, match="cannot fit"):
        Account.model_validate(
            _account(posts_per_day=2, min_gap_minutes=600, windows=["10:00-10:30"])
        )


def test_shipped_and_default_plans_fit() -> None:
    for market in ("US", "PH"):
        for platform in PLATFORM_DAILY_CAPS:
            Account.model_validate(_account(platform=platform, market=market))


def test_duplicate_account_ids_are_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate account id: acct_one"):
        AccountsConfig.model_validate(
            {"accounts": [_account(), _account(handle="@other", platform="youtube")]}
        )


def test_duplicate_platform_handle_is_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate tiktok handle: @ACCT.ONE"):
        AccountsConfig.model_validate(
            {"accounts": [_account(), _account(id="acct_two", handle="@ACCT.ONE")]}
        )


def test_duplicate_handle_with_and_without_at_is_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate tiktok handle"):
        AccountsConfig.model_validate(
            {"accounts": [_account(), _account(id="acct_two", handle="acct.one")]}
        )


def test_same_handle_on_different_platforms_is_allowed() -> None:
    cfg = AccountsConfig.model_validate(
        {"accounts": [_account(), _account(id="acct_two", platform="youtube")]}
    )

    assert len(cfg.accounts) == 2


def test_account_id_that_ends_another_id_is_refused() -> None:
    """`CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT` would also end in `_CLIPS_TT`."""
    with pytest.raises(ValidationError, match="ambiguous"):
        AccountsConfig.model_validate(
            {
                "accounts": [
                    _account(id="mrbeast_clips_tt"),
                    _account(id="clips_tt", handle="@other"),
                ]
            }
        )


@pytest.mark.parametrize("account_id", ["id", "proxy", "dir", "tag"])
def test_account_id_that_makes_an_app_setting_look_like_a_token_is_refused(
    account_id: str,
) -> None:
    with pytest.raises(ValidationError, match="app setting CLIPPER_"):
        AccountsConfig.model_validate({"accounts": [_account(id=account_id)]})


def test_creator_names_are_stored_as_in_creators_yaml(
    tmp_path: Path, creators_cfg: AppConfig
) -> None:
    text = ACCOUNTS_YAML.replace("creators: [MrBeast]", "creators: [mrbeast]", 1)

    cfg = load_accounts(_write(tmp_path, text), creators_cfg)

    assert cfg.accounts[0].creators == ["MrBeast"]


def test_one_licence_covers_only_one_creator(tmp_path: Path, creators_cfg: AppConfig) -> None:
    text = ACCOUNTS_YAML.replace(
        "creators: [MrBeast]\n    posts_per_day",
        'creators: [Raffy Tulfo in Action, Ivana Alawi]\n    licence: "RTIA 2026-12"\n'
        "    posts_per_day",
    )

    with pytest.raises(ConfigError) as exc:
        load_accounts(_write(tmp_path, text), creators_cfg)

    assert "'Raffy Tulfo in Action', 'Ivana Alawi' each need their own licence" in str(exc.value)


def test_yaml_error_does_not_quote_the_file(tmp_path: Path, creators_cfg: AppConfig) -> None:
    text = f"accounts:\n  - id: x\n    access_token: {TOKEN}: [\n"

    with pytest.raises(ConfigError) as exc:
        load_accounts(_write(tmp_path, text), creators_cfg)

    assert "line 3" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_unknown_creator_is_refused_by_name(tmp_path: Path, creators_cfg: AppConfig) -> None:
    text = ACCOUNTS_YAML.replace(
        "creators: [MrBeast]\n    posts_per_day", "creators: [Nobody]\n    posts_per_day"
    )

    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*unknown creator 'Nobody'"):
        load_accounts(_write(tmp_path, text), creators_cfg)


def test_invalid_accounts_yaml_is_config_error(tmp_path: Path, creators_cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_accounts(_write(tmp_path, "accounts: [\n"), creators_cfg)
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_accounts(_write(tmp_path, "- 1\n"), creators_cfg)
    with pytest.raises(ConfigError, match="duplicate key"):
        load_accounts(_write(tmp_path, "accounts: []\naccounts: []\n"), creators_cfg)
    with pytest.raises(ConfigError, match=r"accounts\.0\.posts_per_day"):
        load_accounts(
            _write(tmp_path, ACCOUNTS_YAML.replace("posts_per_day: 3", "posts_per_day: 99")),
            creators_cfg,
        )


def test_accounts_path_defaults_next_to_creators_config(tmp_path: Path) -> None:
    creators = tmp_path / "conf" / "creators.yaml"

    assert accounts_path(creators, env={}) == tmp_path / "conf" / "accounts.yaml"
    assert accounts_path(creators, env={"CLIPPER_ACCOUNTS": "/x/a.yaml"}) == Path("/x/a.yaml")


def test_shipped_accounts_yaml_validates_against_shipped_creators() -> None:
    creators = load_config(REPO / "config" / "creators.yaml")
    cfg = load_accounts(REPO / "config" / "accounts.yaml", creators)

    ids = {a.id: a for a in cfg.accounts}
    assert set(ids) == {
        "mrbeast_clips_tt",
        "mrbeast_clips_yt",
        "mrbeast_clips_ig",
        "mrbeast_clips_fb",
    }
    assert {a.platform for a in cfg.accounts} == {"tiktok", "youtube", "instagram", "facebook"}
    for account in cfg.accounts:
        assert account.market == "US"
        assert account.timezone == "America/New_York"
        assert account.creators == ["MrBeast"]
        assert account.warmup_start == date(2026, 11, 1)
        assert account.campaign == "vyro_mrbeast"
    paused = {"raffy tulfo in action", "ivana alawi", "cong tv"}
    assert not any(c.casefold() in paused for a in cfg.accounts for c in a.creators)


def test_check_config_summarises_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creators = _write(tmp_path, CREATORS_YAML, "creators.yaml")
    _write(tmp_path, ACCOUNTS_YAML)
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT", TOKEN)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert "2 accounts" in result.output
    lines = [line for line in result.output.splitlines() if "mrbeast_clips_" in line]
    tt, yt = lines
    for part in ("tiktok", "@mrbeast.clips.daily", "US", "3/day", "14:00-17:00", "18:00-22:00"):
        assert part in tt
    assert "access_token=set" in tt
    assert "refresh_token=missing" in tt
    assert "2/day" in yt
    assert "access_token=missing" in yt


def test_check_config_without_accounts_file(config_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(config_file), "check-config"])

    assert result.exit_code == 0, result.output
    assert "0 accounts" in result.output


def test_check_config_honours_clipper_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creators = _write(tmp_path, CREATORS_YAML, "creators.yaml")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.setenv("CLIPPER_ACCOUNTS", str(_write(elsewhere, ACCOUNTS_YAML)))

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert "2 accounts" in result.output


def test_check_config_fails_on_invalid_accounts(tmp_path: Path) -> None:
    creators = _write(tmp_path, CREATORS_YAML, "creators.yaml")
    _write(
        tmp_path,
        ACCOUNTS_YAML.replace("timezone: America/New_York", "timezone: Nowhere/Land", 1),
    )

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 1
    assert "unknown timezone" in result.stderr


# --- AC2: a creator without a licence cannot be assigned to an account -------------------


@pytest.mark.parametrize("creator", ["Raffy Tulfo in Action", "Ivana Alawi", "No Type"])
def test_unlicensed_creator_is_refused_and_named(
    tmp_path: Path, creators_cfg: AppConfig, creator: str
) -> None:
    text = ACCOUNTS_YAML.replace(
        "creators: [MrBeast]\n    posts_per_day",
        f"creators: [MrBeast, {creator}]\n    posts_per_day",
    )

    with pytest.raises(ConfigError) as exc:
        load_accounts(_write(tmp_path, text), creators_cfg)

    assert repr(creator) in str(exc.value)
    assert "mrbeast_clips_tt" in str(exc.value)
    assert "licence" in str(exc.value)


@pytest.mark.parametrize("creator", ["Raffy Tulfo in Action", "Ivana Alawi"])
def test_licensed_creator_is_allowed(tmp_path: Path, creators_cfg: AppConfig, creator: str) -> None:
    text = ACCOUNTS_YAML.replace(
        "creators: [MrBeast]\n    posts_per_day",
        f'creators: [{creator}]\n    licence: "agreement 2026-11 #12"\n    posts_per_day',
    )

    cfg = load_accounts(_write(tmp_path, text), creators_cfg)

    assert cfg.accounts[0].licence == "agreement 2026-11 #12"


def test_comedy_creator_needs_no_licence(tmp_path: Path, creators_cfg: AppConfig) -> None:
    text = ACCOUNTS_YAML.replace(
        "creators: [MrBeast]\n    posts_per_day", "creators: [Cong TV]\n    posts_per_day"
    )

    cfg = load_accounts(_write(tmp_path, text), creators_cfg)

    assert cfg.accounts[0].creators == ["Cong TV"]


def test_check_config_names_unlicensed_creator(tmp_path: Path) -> None:
    creators = _write(tmp_path, CREATORS_YAML, "creators.yaml")
    _write(
        tmp_path,
        ACCOUNTS_YAML.replace(
            "creators: [MrBeast]\n    posts_per_day", "creators: [Ivana Alawi]\n    posts_per_day"
        ),
    )

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 1
    assert "'Ivana Alawi'" in result.stderr
    assert "mrbeast_clips_tt" in result.stderr


# --- AC3: token secrets come from the environment by account id, never logged -------------


def test_token_env_name() -> None:
    assert (
        token_env_name("mrbeast_clips_tt", "access_token")
        == "CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT"
    )
    assert (
        token_env_name("mrbeast_clips_tt", "refresh_token")
        == "CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT"
    )
    with pytest.raises(ValueError):
        token_env_name("bad id", "access_token")
    with pytest.raises(ValueError):
        token_env_name("ok", "bad kind!")


def test_account_tokens_read_from_env_by_account_id() -> None:
    env = {
        "CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT": TOKEN,
        "CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT": REFRESH,
        "CLIPPER_CLIENT_SECRET_MRBEAST_CLIPS_TT": "cs_secret_value",
        "CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_YT": "other_account_token",
        "CLIPPER_CONFIG": "/app/config/creators.yaml",
        "CLIPPER_Access_Token_MRBEAST_CLIPS_TT": "mixed_case_value",
    }

    tokens = account_tokens("mrbeast_clips_tt", env=env)

    assert isinstance(tokens, AccountTokens)
    assert tokens.access_token is not None
    assert tokens.access_token.get_secret_value() == TOKEN
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value() == REFRESH
    assert set(tokens.extra) == {"client_secret"}
    assert tokens.extra["client_secret"].get_secret_value() == "cs_secret_value"


def test_app_settings_are_never_read_as_tokens() -> None:
    env = {"CLIPPER_YTDLP_PROXY": "http://user:pw@proxy", "CLIPPER_WORKER_ID": "w1"}

    assert account_tokens("proxy", env=env).extra == {}
    assert account_tokens("id", env=env).extra == {}


def test_every_app_clipper_setting_is_reserved() -> None:
    """A new CLIPPER_* setting must join APP_ENV_NAMES, or an account id matching its
    suffix would read it as a token."""
    sources = [*(REPO / "src").rglob("*.py"), *(REPO / "scripts").rglob("*")]
    sources += [*(REPO / ".github").rglob("*.yml"), *REPO.glob("docker-compose*.yml")]
    sources += [REPO / "Dockerfile", REPO / ".env.example", *(REPO / "docs").rglob("*.md")]
    names: set[str] = set()
    for path in sources:
        if path.is_file():
            names |= set(re.findall(r"CLIPPER_[A-Z0-9_]*[A-Z0-9]", path.read_text("utf-8")))
    token_kinds = ("CLIPPER_ACCESS_TOKEN", "CLIPPER_REFRESH_TOKEN", "CLIPPER_CLIENT_SECRET")
    unreserved = {n for n in names if n not in APP_ENV_NAMES and not n.startswith(token_kinds)}

    assert unreserved == set()


def test_account_tokens_missing_or_empty_is_none() -> None:
    tokens = account_tokens("mrbeast_clips_tt", env={"CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT": ""})

    assert tokens.access_token is None
    assert tokens.refresh_token is None
    assert tokens.extra == {}


def test_account_tokens_default_to_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT", TOKEN)

    tokens = account_tokens("mrbeast_clips_tt")

    assert tokens.access_token is not None
    assert tokens.access_token.get_secret_value() == TOKEN


def test_account_tokens_read_dotenv(tmp_path: Path) -> None:
    """conftest chdirs into tmp_path, so this is the `.env` the process would read."""
    (tmp_path / ".env").write_text(f"CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT={REFRESH}\n")

    tokens = account_tokens("mrbeast_clips_tt")

    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value() == REFRESH


def test_tokens_never_render(caplog: pytest.LogCaptureFixture) -> None:
    env = {
        "CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT": TOKEN,
        "CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT": REFRESH,
        "CLIPPER_CLIENT_SECRET_MRBEAST_CLIPS_TT": "cs_secret_value",
    }
    tokens = account_tokens("mrbeast_clips_tt", env=env)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("tokens %s %r", tokens, tokens)
    rendered = [repr(tokens), str(tokens), caplog.text, tokens.model_dump_json()]
    for text in rendered:
        for secret in (TOKEN, REFRESH, "cs_secret_value"):
            assert secret not in text


def test_check_config_prints_token_status_not_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creators = _write(tmp_path, CREATORS_YAML, "creators.yaml")
    _write(tmp_path, ACCOUNTS_YAML)
    monkeypatch.setenv("CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT", TOKEN)
    monkeypatch.setenv("CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT", REFRESH)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert TOKEN not in result.output
    assert REFRESH not in result.output
    assert "access_token=set" in result.output
    assert "refresh_token=set" in result.output


# --- AC4: docs explain how to add an account and its tokens -------------------------------


def test_deployment_docs_explain_accounts_and_tokens() -> None:
    text = (REPO / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "## Posting accounts" in text
    section = text.split("## Posting accounts", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "config/accounts.yaml",
        "CLIPPER_ACCOUNTS",
        "CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT",
        "CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT",
        "licence",
        "clipper check-config",
    ):
        assert needle in section


def test_env_example_lists_account_token_names() -> None:
    text = (REPO / ".env.example").read_text(encoding="utf-8")

    assert "# CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT=" in text
    assert "# CLIPPER_REFRESH_TOKEN_MRBEAST_CLIPS_TT=" in text
