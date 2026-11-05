"""Campaign briefs (CLP-97): campaigns.yaml, the audience policy guard, the pre-flight."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipper.accounts import APP_ENV_NAMES, AccountsConfig, load_accounts
from clipper.campaigns import (
    CAMPAIGNS_ENV,
    Campaign,
    CampaignsConfig,
    campaign_preflight,
    campaigns_path,
    load_campaigns,
    render_edl,
)
from clipper.config import AppConfig, ConfigError, load_config
from clipper.edl import parse_edl
from clipper.metadata import PostMetadata, build_post_metadata, derive_variants

REPO = Path(__file__).resolve().parents[1]
WHEN = datetime(2027, 1, 15, 15, 0, tzinfo=UTC)

CREATORS_YAML = """\
creators:
  - name: MrBeast
    region: global
    content_type: challenge_stunt
    youtube:
      handle: "@MrBeast"
  - name: Kai Cenat
    region: global
    content_type: streamer_reaction
    twitch:
      login: kaicenat
"""

ACCOUNTS_YAML = """\
accounts:
  - id: mrbeast_clips_tt
    platform: tiktok
    handle: "@mrbeast.clips"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
    campaign: vyro_mrbeast
"""

CAMPAIGNS_YAML = """\
campaigns:
  - id: vyro_mrbeast
    program: vyro
    creator: mrbeast
    platforms: [tiktok, youtube]
    brief_verified: true
    min_duration_s: 20
    max_duration_s: 90
    required_hashtags: ["#mrbeast", "mrbeastpartner"]
    forbidden: [ai_content, logos]
    forbidden_keywords: [scam]
    audience_rule:
      markets: [US, CA]
      min_share: 0.5
    submit_within_minutes: 30
    rate_per_1k: 1.25
    per_clip_cap: 500
    budget: 10000
    ends_at: 2027-03-01T00:00:00Z
"""


@pytest.fixture
def creators(tmp_path: Path) -> AppConfig:
    path = tmp_path / "creators.yaml"
    path.write_text(CREATORS_YAML)
    return load_config(path)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _accounts(tmp_path: Path, creators: AppConfig, text: str = ACCOUNTS_YAML) -> AccountsConfig:
    return load_accounts(_write(tmp_path, "accounts.yaml", text), creators)


def _campaign(**overrides: object) -> Campaign:
    base: dict[str, object] = {
        "id": "vyro_mrbeast",
        "program": "vyro",
        "creator": "MrBeast",
        "platforms": ["tiktok"],
        "brief_verified": True,
        "min_duration_s": 20,
        "required_hashtags": ["#mrbeast", "#mrbeastpartner"],
    }
    base.update(overrides)
    return Campaign.model_validate(base)


def _metadata(campaign: Campaign, platform: str = "tiktok", **kwargs: object) -> PostMetadata:
    variants = derive_variants(
        str(kwargs.get("title", "He gave away a private island")),
        str(kwargs.get("caption", "The craziest giveaway yet")),
        kwargs.get("hashtags", ["#giveaway", "#island", "#challenge"]),  # type: ignore[arg-type]
    )
    return build_post_metadata(
        variants,
        platform,
        campaign.required_hashtags,
        paid_partnership=campaign.paid_partnership,
    )


# --- campaigns.yaml -------------------------------------------------------------------


def test_campaigns_yaml_loads_with_the_brief_fields(tmp_path: Path, creators: AppConfig) -> None:
    cfg = load_campaigns(
        _write(tmp_path, "campaigns.yaml", CAMPAIGNS_YAML), creators, _accounts(tmp_path, creators)
    )

    campaign = cfg.get("vyro_mrbeast")
    assert campaign is not None
    assert campaign.creator == "MrBeast"  # spelled as in creators.yaml
    assert campaign.program == "vyro"
    assert campaign.platforms == ["tiktok", "youtube"]
    assert campaign.required_hashtags == ["#mrbeast", "#mrbeastpartner"]
    assert campaign.forbidden == ["ai_content", "logos"]
    assert campaign.audience_rule is not None
    assert campaign.audience_rule.markets == ["US", "CA"]
    assert campaign.submit_within_minutes == 30
    assert campaign.rate_per_1k == Decimal("1.25")
    assert campaign.ends_at == datetime(2027, 3, 1, tzinfo=UTC)


def test_missing_campaigns_file_is_empty_when_no_account_names_one(
    tmp_path: Path, creators: AppConfig
) -> None:
    cfg = load_campaigns(tmp_path / "nope.yaml", creators, AccountsConfig())

    assert cfg.campaigns == []


def test_missing_campaigns_file_with_an_account_campaign_is_an_error(
    tmp_path: Path, creators: AppConfig
) -> None:
    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*vyro_mrbeast"):
        load_campaigns(tmp_path / "nope.yaml", creators, _accounts(tmp_path, creators))


def test_campaigns_path_defaults_next_to_creators_and_honours_the_env(tmp_path: Path) -> None:
    config = tmp_path / "cfg" / "creators.yaml"

    assert campaigns_path(config, {}) == tmp_path / "cfg" / "campaigns.yaml"
    assert campaigns_path(config, {CAMPAIGNS_ENV: "/x/c.yaml"}) == Path("/x/c.yaml")


def test_the_campaigns_env_name_is_never_read_as_an_account_token() -> None:
    assert CAMPAIGNS_ENV in APP_ENV_NAMES


def test_shipped_campaigns_are_unverified_placeholders() -> None:
    creators = load_config(REPO / "config" / "creators.yaml")
    accounts = load_accounts(REPO / "config" / "accounts.yaml", creators)

    cfg = load_campaigns(REPO / "config" / "campaigns.yaml", creators, accounts)

    assert {c.id for c in cfg.campaigns} >= {"vyro_mrbeast", "reach_kai_cenat"}
    assert all(not c.brief_verified for c in cfg.campaigns)
    reach = cfg.get("reach_kai_cenat")
    assert reach is not None
    assert "ai_content" in reach.forbidden
    assert reach.submit_within_minutes == 30


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"program": "patreon"}, "program"),
        ({"platforms": []}, "platforms"),
        ({"platforms": ["tiktok", "tiktok"]}, "duplicate platform"),
        ({"min_duration_s": 60, "max_duration_s": 30}, "min_duration_s"),
        ({"required_hashtags": ["#two words"]}, "hashtag"),
        ({"forbidden": ["swearing"]}, "forbidden"),
        ({"audience_rule": {"markets": ["usa"], "min_share": 0.5}}, "markets"),
        ({"audience_rule": {"markets": ["US"], "min_share": 1.5}}, "min_share"),
        ({"submit_within_minutes": 0}, "submit_within_minutes"),
        ({"rate_per_1k": -1}, "rate_per_1k"),
        ({"ends_at": "2027-03-01T00:00:00"}, "ends_at"),
        ({"currency": "usd"}, "currency"),
        ({"surprise": True}, "surprise"),
    ],
)
def test_campaign_field_validation(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _campaign(**overrides)


def test_duplicate_campaign_ids_are_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate campaign id"):
        CampaignsConfig.model_validate(
            {"campaigns": [_campaign().model_dump(), _campaign().model_dump()]}
        )


def test_unknown_creator_is_refused(tmp_path: Path, creators: AppConfig) -> None:
    text = CAMPAIGNS_YAML.replace("creator: mrbeast", "creator: Nobody")

    with pytest.raises(ConfigError, match="unknown creator 'Nobody'"):
        load_campaigns(_write(tmp_path, "campaigns.yaml", text), creators, AccountsConfig())


def test_account_naming_an_unknown_campaign_is_refused(tmp_path: Path, creators: AppConfig) -> None:
    accounts = _accounts(tmp_path, creators, ACCOUNTS_YAML.replace("vyro_mrbeast", "vyro_other"))

    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*unknown campaign 'vyro_other'"):
        load_campaigns(_write(tmp_path, "campaigns.yaml", CAMPAIGNS_YAML), creators, accounts)


def test_account_on_a_platform_the_campaign_does_not_take_is_refused(
    tmp_path: Path, creators: AppConfig
) -> None:
    text = CAMPAIGNS_YAML.replace("[tiktok, youtube]", "[youtube]")

    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*tiktok"):
        load_campaigns(
            _write(tmp_path, "campaigns.yaml", text), creators, _accounts(tmp_path, creators)
        )


def test_account_whose_market_cannot_meet_the_audience_rule_is_refused(
    tmp_path: Path, creators: AppConfig
) -> None:
    # Policy guard: a PH account cannot deliver a US/CA audience.
    accounts = _accounts(
        tmp_path,
        creators,
        ACCOUNTS_YAML.replace("market: US", "market: PH").replace(
            "America/New_York", "Asia/Manila"
        ),
    )

    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*market PH.*US, CA"):
        load_campaigns(_write(tmp_path, "campaigns.yaml", CAMPAIGNS_YAML), creators, accounts)


def test_account_that_does_not_post_the_campaign_creator_is_refused(
    tmp_path: Path, creators: AppConfig
) -> None:
    accounts = _accounts(tmp_path, creators, ACCOUNTS_YAML.replace("[MrBeast]", "[Kai Cenat]"))

    with pytest.raises(ConfigError, match="mrbeast_clips_tt.*does not post MrBeast"):
        load_campaigns(_write(tmp_path, "campaigns.yaml", CAMPAIGNS_YAML), creators, accounts)


def test_invalid_yaml_does_not_echo_the_line(tmp_path: Path, creators: AppConfig) -> None:
    path = _write(tmp_path, "campaigns.yaml", 'campaigns:\n  - id: "secret_value_xyz\n')

    with pytest.raises(ConfigError) as exc:
        load_campaigns(path, creators, AccountsConfig())

    assert "secret_value_xyz" not in str(exc.value)


# --- pre-flight -----------------------------------------------------------------------


def test_a_clip_that_meets_the_brief_passes() -> None:
    campaign = _campaign()

    assert campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, None, WHEN) == []


def test_too_short_clip_is_held_with_the_reason() -> None:
    campaign = _campaign()

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 12.0, None, WHEN)

    assert any("12 s is under the brief's 20 s" in p for p in problems)


def test_too_long_clip_is_held_with_the_reason() -> None:
    campaign = _campaign(max_duration_s=60)

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 75.0, None, WHEN)

    assert any("75 s is over the brief's 60 s" in p for p in problems)


def test_missing_required_tag_is_held_with_the_reason() -> None:
    campaign = _campaign()
    metadata = build_post_metadata(
        derive_variants("Title", "Caption", ["#giveaway", "#island", "#challenge"]),
        "tiktok",
        ["#mrbeast"],
    )

    problems = campaign_preflight(campaign, metadata, "tiktok", 30.0, None, WHEN)

    assert any("required tag #mrbeastpartner is missing" in p for p in problems)


def test_forbidden_keyword_is_held_with_the_reason() -> None:
    campaign = _campaign(forbidden_keywords=["scam"])
    metadata = _metadata(campaign, title="Is this giveaway a scam?")

    problems = campaign_preflight(campaign, metadata, "tiktok", 30.0, None, WHEN)

    assert any("forbidden keyword 'scam'" in p for p in problems)


def test_ai_content_is_held_when_the_brief_forbids_it(tmp_path: Path) -> None:
    campaign = _campaign(forbidden=["ai_content"])
    edl = parse_edl(
        {
            "source": {"path": "a.mp4", "in_s": 0, "out_s": 30},
            "hook": {"text": "Wait for it", "ai_generated": True},
        }
    )

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, edl, WHEN)

    assert any("AI-generated" in p for p in problems)


def test_unknown_ai_status_is_held_when_the_brief_forbids_ai(tmp_path: Path) -> None:
    # No render sidecar: we cannot show the clip is free of AI elements, so fail closed.
    campaign = _campaign(forbidden=["ai_content"])

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, None, WHEN)

    assert any("cannot confirm" in p for p in problems)


def test_clean_edl_passes_an_ai_ban() -> None:
    campaign = _campaign(forbidden=["ai_content"])
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 30}})

    assert campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, edl, WHEN) == []


def test_placeholder_brief_holds_every_post() -> None:
    campaign = _campaign(brief_verified=False)

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, None, WHEN)

    assert any("placeholder" in p and "brief_verified" in p for p in problems)


def test_disabled_or_ended_campaign_holds_the_post() -> None:
    disabled = _campaign(enabled=False)
    ended = _campaign(ends_at="2027-01-01T00:00:00Z")

    assert any(
        "disabled" in p
        for p in campaign_preflight(disabled, _metadata(disabled), "tiktok", 30.0, None, WHEN)
    )
    problems = campaign_preflight(ended, _metadata(ended), "tiktok", 30.0, None, WHEN)
    assert any("ended" in p for p in problems)


def test_platform_the_campaign_does_not_take_is_held() -> None:
    campaign = _campaign(platforms=["youtube"])

    problems = campaign_preflight(campaign, _metadata(campaign), "tiktok", 30.0, None, WHEN)

    assert any("does not take tiktok" in p for p in problems)


def test_paid_partnership_brief_needs_the_toggle() -> None:
    campaign = _campaign(paid_partnership=True)
    metadata = build_post_metadata(
        derive_variants("T", "C", ["#giveaway", "#island", "#challenge"]),
        "tiktok",
        campaign.required_hashtags,
        paid_partnership=False,
    )

    problems = campaign_preflight(campaign, metadata, "tiktok", 30.0, None, WHEN)

    assert any("paid partnership" in p for p in problems)


# --- render sidecar -------------------------------------------------------------------


def _sidecar(media: Path, edl: dict[str, object] | None, name: str = "clip_v1") -> str:
    video = media / "job" / f"{name}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00")
    if edl is not None:
        (media / "job" / f"{name}.json").write_text(json.dumps({"sidecar_version": 1, "edl": edl}))
    return f"job/{name}.mp4"


def test_render_edl_reads_the_sidecar(tmp_path: Path) -> None:
    edl = parse_edl(
        {
            "source": {"path": "a.mp4", "in_s": 0, "out_s": 30},
            "hook": {"text": "Wait", "ai_generated": True},
        }
    )
    stored = _sidecar(tmp_path, edl.to_props())

    found = render_edl(tmp_path, stored)

    assert found is not None
    assert found.hook is not None and found.hook.ai_generated


@pytest.mark.parametrize("edl", [None, {"source": "broken"}])
def test_render_edl_is_none_without_a_valid_sidecar(
    tmp_path: Path, edl: dict[str, object] | None
) -> None:
    assert render_edl(tmp_path, _sidecar(tmp_path, edl)) is None


def test_render_edl_never_reads_outside_the_media_root(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    _sidecar(tmp_path, {"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}}, "outside")

    assert render_edl(media, "../job/outside.mp4") is None
    assert render_edl(media, None) is None
