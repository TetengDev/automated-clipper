"""Clipping campaigns (CLP-97): each paid campaign's brief as config, and the pre-flight.

`config/campaigns.yaml` holds one entry per campaign (Vyro, Whop, Reach or a sponsor). An
account joins one with `campaign: <id>` in accounts.yaml (CLP-91). `load_campaigns`
validates the briefs against creators.yaml and the accounts. The policy guard lives here
too: an account whose market is not one of the campaign's audience markets, whose platform
the campaign does not take, or that does not post the campaign's creator is a config error,
so it is never assigned to that campaign.

At scheduling (`clipper.posting.scheduler`), `campaign_preflight` checks a post against its
campaign's brief on top of CLP-96's platform pre-flight (`clipper.metadata.preflight`).
Anything it returns holds the post with the reason for the reviewer.

A brief copied from the epic's research notes is a placeholder until the owner checks it
against the live brief and sets `brief_verified: true`; until then every post for it is
held.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from clipper.accounts import (
    ACCOUNT_ID_PATTERN,
    Account,
    AccountsConfig,
    Platform,
    _yaml_problem,
    accounts_path,
    load_accounts,
)
from clipper.clips import resolve_media_path
from clipper.config import AppConfig, ConfigError, _format_errors, _UniqueKeyLoader, load_config
from clipper.edl import EDL, has_ai_elements, parse_edl
from clipper.metadata import HASHTAG, Brief, PostMetadata, preflight

CAMPAIGNS_ENV = "CLIPPER_CAMPAIGNS"
CAMPAIGNS_FILENAME = "campaigns.yaml"
MAX_SIDECAR_BYTES = 1_000_000
"""A render sidecar is a few KB; anything far larger is not ours and is not parsed."""

Program = Literal["vyro", "whop", "reach", "sponsor"]
Forbidden = Literal["logos", "negative_framing", "ai_content", "reposts"]
REVIEWER_CHECKS: frozenset[str] = frozenset({"logos", "negative_framing", "reposts"})
"""Forbidden elements the pipeline cannot see in the video: the reviewer checks them before
approving. `ai_content` is checked from the render; `forbidden_keywords` from the text."""

_MARKET = re.compile(r"[A-Z]{2}")


class AudienceRule(BaseModel):
    """At least `min_share` of the post's audience must be in `markets` (ISO 3166-1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    markets: list[str] = Field(min_length=1)
    min_share: float = Field(gt=0, le=1)

    @field_validator("markets")
    @classmethod
    def _country_codes(cls, value: list[str]) -> list[str]:
        for market in value:
            if not isinstance(market, str) or not _MARKET.fullmatch(market):
                raise ValueError(f"markets: {market!r} is not a 2-letter country code like US")
        if len(set(value)) != len(value):
            raise ValueError("markets: duplicate market")
        return value


def _campaign_tag(raw: object) -> str:
    """A campaign hashtag, `#` added if missing. Strict: a tag that is not one word is a
    config error, never silently joined or dropped."""
    if not isinstance(raw, str):
        raise ValueError(f"invalid hashtag {raw!r}")
    tag = raw.strip()
    tag = tag if tag.startswith("#") else f"#{tag}"
    if not HASHTAG.fullmatch(tag):
        raise ValueError(f"invalid hashtag {raw!r}: one word after #, at most 50 characters")
    return tag


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ACCOUNT_ID_PATTERN, max_length=64)
    program: Program
    creator: str = Field(min_length=1)
    """A name from creators.yaml; only that creator's clips count for the campaign."""
    platforms: list[Platform] = Field(min_length=1)
    brief_verified: bool = False
    """The owner has checked every rule against the live brief. False holds every post."""
    min_duration_s: float | None = Field(None, ge=0)
    max_duration_s: float | None = Field(None, gt=0)
    required_hashtags: list[str] = Field(default_factory=list)
    forbidden: list[Forbidden] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    audience_rule: AudienceRule | None = None
    submit_within_minutes: int | None = Field(None, ge=1, le=10_080)
    rate_per_1k: Decimal | None = Field(None, ge=0)
    per_clip_cap: Decimal | None = Field(None, ge=0)
    budget: Decimal | None = Field(None, ge=0)
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    ends_at: AwareDatetime | None = None
    paid_partnership: bool = False
    enabled: bool = True

    @field_validator("platforms", "forbidden")
    @classmethod
    def _distinct(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate {info.field_name.rstrip('s')}")
        return value

    @field_validator("required_hashtags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        tags = [_campaign_tag(raw) for raw in value]
        if len({t.casefold() for t in tags}) != len(tags):
            raise ValueError("duplicate hashtag")
        return tags

    @field_validator("forbidden_keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        if any(not k.strip() for k in value):
            raise ValueError("forbidden_keywords must not be empty strings")
        return list(dict.fromkeys(k.strip() for k in value))

    @model_validator(mode="after")
    def _duration_range(self) -> Campaign:
        low, high = self.min_duration_s, self.max_duration_s
        if low is not None and high is not None and low > high:
            raise ValueError(f"min_duration_s {low:g} is above max_duration_s {high:g}")
        return self

    def brief(self) -> Brief:
        """The rules CLP-96's `preflight` checks (tags, keywords, length, paid toggle)."""
        return Brief(
            name=self.id,
            required_tags=list(self.required_hashtags),
            forbidden_keywords=list(self.forbidden_keywords),
            min_seconds=self.min_duration_s,
            paid_partnership=self.paid_partnership,
        )

    @property
    def reviewer_checks(self) -> list[str]:
        return [f for f in self.forbidden if f in REVIEWER_CHECKS]


class CampaignsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaigns: list[Campaign] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> CampaignsConfig:
        seen: set[str] = set()
        for campaign in self.campaigns:
            if campaign.id in seen:
                raise ValueError(f"duplicate campaign id: {campaign.id}")
            seen.add(campaign.id)
        return self

    def get(self, campaign_id: str) -> Campaign | None:
        return next((c for c in self.campaigns if c.id == campaign_id), None)

    def by_id(self) -> dict[str, Campaign]:
        return {c.id: c for c in self.campaigns}


def campaigns_path(config_path: Path, env: Mapping[str, str] | None = None) -> Path:
    """`CLIPPER_CAMPAIGNS`, else campaigns.yaml next to the creators config."""
    env = os.environ if env is None else env
    override = env.get(CAMPAIGNS_ENV)
    return Path(override) if override else config_path.parent / CAMPAIGNS_FILENAME


def _account_problems(account: Account, campaign: Campaign | None) -> list[str]:
    """The policy guard: why `account` cannot be assigned to its campaign."""
    where = f"account {account.id!r}"
    if campaign is None:
        return [f"{where}: unknown campaign {account.campaign!r}"]
    problems = []
    if account.platform not in campaign.platforms:
        problems.append(
            f"{where}: campaign {campaign.id!r} does not take {account.platform} "
            f"(only {', '.join(campaign.platforms)})"
        )
    if campaign.creator.casefold() not in {c.casefold() for c in account.creators}:
        problems.append(f"{where}: does not post {campaign.creator}, the campaign's creator")
    rule = campaign.audience_rule
    if rule is not None and account.market not in rule.markets:
        problems.append(
            f"{where}: market {account.market} cannot meet campaign {campaign.id!r}'s "
            f"audience rule ({rule.min_share:.0%} in {', '.join(rule.markets)})"
        )
    return problems


def load_campaigns(path: Path, creators: AppConfig, accounts: AccountsConfig) -> CampaignsConfig:
    """Load and validate campaigns.yaml against the creators and the accounts that name a
    campaign. A missing file is an empty config: campaign accounts' posts are then held
    (see `unconfigured_campaigns`)."""
    if not path.exists():
        return CampaignsConfig()
    cfg = _read(path)
    problems: list[str] = []
    canonical: list[Campaign] = []
    for campaign in cfg.campaigns:
        try:
            name = creators.creator(campaign.creator).name
        except KeyError:
            problems.append(f"campaign {campaign.id!r}: unknown creator {campaign.creator!r}")
            continue
        canonical.append(campaign.model_copy(update={"creator": name}))
    by_id = {c.id: c for c in canonical}
    for account in accounts.accounts:
        if account.campaign is None:
            continue
        if account.campaign in {c.id for c in cfg.campaigns} and account.campaign not in by_id:
            continue  # its campaign's own error (an unknown creator) is reported above
        problems.extend(_account_problems(account, by_id.get(account.campaign)))
    if problems:
        details = "\n".join(f"  {p}" for p in problems)
        raise ConfigError(f"invalid campaigns config {path}:\n{details}")
    return CampaignsConfig(campaigns=canonical)


def unconfigured_campaigns(accounts: AccountsConfig, campaigns: CampaignsConfig) -> list[str]:
    """Warnings for accounts that name a campaign the config does not have (only possible
    without a campaigns file): every post for them is held."""
    known = {c.id for c in campaigns.campaigns}
    return [
        f"account {a.id!r} names campaign {a.campaign!r}, which is not configured; "
        "its posts are held until it is"
        for a in accounts.accounts
        if a.campaign is not None and a.campaign not in known
    ]


def _read(path: Path) -> CampaignsConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader) or {}  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {_yaml_problem(exc)}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"invalid YAML in {path}: top level must be a mapping")
    try:
        return CampaignsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid campaigns config {path}:\n{_format_errors(exc)}") from exc


# --- pre-flight ---------------------------------------------------------------------------


def render_edl(media_root: Path, video_path: str | None) -> EDL | None:
    """The EDL a clip was rendered from, read from the render sidecar next to the video
    (`<basename>.json`, see `clipper.render`). None when the video or sidecar is missing,
    outside `media_root`, or not a valid EDL: callers treat None as "unknown"."""
    if not video_path:
        return None
    try:
        sidecar = resolve_media_path(media_root, str(Path(video_path).with_suffix(".json")))
        if sidecar is None:
            return None
        with sidecar.open("rb") as fh:  # one bounded read: no stat-then-read race
            raw = fh.read(MAX_SIDECAR_BYTES + 1)
        if len(raw) > MAX_SIDECAR_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
        edl = data.get("edl") if isinstance(data, dict) else None
        return parse_edl(edl) if isinstance(edl, dict) else None
    except (OSError, UnicodeError, ValueError):  # ValidationError is a ValueError
        return None


def campaign_preflight(
    campaign: Campaign,
    metadata: PostMetadata,
    platform: str,
    clip_seconds: float,
    edl: EDL | None,
    when: datetime,
) -> list[str]:
    """Why the post would break `campaign`'s brief (or the platform's limits), or [] when
    it may be scheduled. `edl` is the rendered clip's (None: unknown, which fails an AI
    ban closed); `when` is the slot the post would go out at."""
    name = campaign.id
    problems: list[str] = []
    if not campaign.brief_verified:
        problems.append(
            f"{name}: the brief is a placeholder; copy the rules from the live brief into "
            "campaigns.yaml and set brief_verified: true"
        )
    if not campaign.enabled:
        problems.append(f"{name}: the campaign is disabled")
    if campaign.ends_at is not None and when >= campaign.ends_at:
        problems.append(f"{name}: the campaign ended at {campaign.ends_at.isoformat()}")
    if platform not in campaign.platforms:
        problems.append(f"{name}: the campaign does not take {platform}")
    problems.extend(preflight(metadata, platform, clip_seconds, campaign.brief()))
    if campaign.max_duration_s is not None and clip_seconds > campaign.max_duration_s:
        problems.append(
            f"{name}: clip length {clip_seconds:g} s is over the brief's"
            f" {campaign.max_duration_s:g} s"
        )
    if "ai_content" in campaign.forbidden:
        if edl is None:
            problems.append(
                f"{name}: cannot confirm the clip has no AI elements (no render sidecar);"
                " the brief forbids AI content"
            )
        elif has_ai_elements(edl):
            problems.append(f"{name}: the clip has AI-generated elements; the brief forbids them")
    return problems


def summary(campaign: Campaign) -> str:
    """One line for `clipper check-config`."""
    parts = [f"{campaign.id} {campaign.program} {campaign.creator}"]
    parts.append(",".join(campaign.platforms))
    low, high = campaign.min_duration_s, campaign.max_duration_s
    if low is not None and high is not None:
        parts.append(f"{low:g}-{high:g} s")
    elif low is not None:
        parts.append(f"at least {low:g} s")
    elif high is not None:
        parts.append(f"at most {high:g} s")
    if "ai_content" in campaign.forbidden:
        parts.append("no AI content")
    if campaign.required_hashtags:
        parts.append(" ".join(campaign.required_hashtags))
    if campaign.submit_within_minutes:
        parts.append(f"submit within {campaign.submit_within_minutes} min")
    if campaign.reviewer_checks:
        parts.append(f"reviewer checks: {', '.join(campaign.reviewer_checks)}")
    flags = [] if campaign.enabled else ["disabled"]
    if not campaign.brief_verified:
        flags.append("placeholder brief (brief_verified: false), posts are held")
    line = "; ".join(parts)
    return f"{line} ({'; '.join(flags)})" if flags else line


class HasCampaign(Protocol):
    campaign: str | None


def campaign_for(
    account: HasCampaign, creator: str, campaigns: Mapping[str, Campaign]
) -> tuple[str | None, Campaign | None]:
    """The account's campaign id (None: no campaign applies) and the campaign (None when
    the id is unknown). A campaign applies only to its own creator's clips."""
    campaign_id = account.campaign
    if not campaign_id:
        return None, None
    campaign = campaigns.get(campaign_id)
    if campaign is not None and campaign.creator.casefold() != creator.casefold():
        return None, None
    return campaign_id, campaign


class PostingConfig:
    """Accounts and campaigns as they are on disk now, for long-running processes (the
    worker's publish-time pre-flight). Reloaded whenever creators.yaml, accounts.yaml or
    campaigns.yaml changes (by mtime), so a campaign disabled or tightened without a
    restart still holds its posts. `current` raises ConfigError for an invalid config;
    callers fail closed."""

    def __init__(self, creators_path: Path) -> None:
        self._paths = (
            creators_path,
            accounts_path(creators_path),
            campaigns_path(creators_path),
        )
        self._stamp: tuple[int, ...] | None = None
        self._value: tuple[dict[str, Account], dict[str, Campaign]] = ({}, {})

    def _mtimes(self) -> tuple[int, ...]:
        return tuple(p.stat().st_mtime_ns if p.exists() else -1 for p in self._paths)

    def current(self) -> tuple[dict[str, Account], dict[str, Campaign]]:
        stamp = self._mtimes()
        if stamp != self._stamp:
            creators_path, accounts_file, campaigns_file = self._paths
            creators = load_config(creators_path)
            accounts = load_accounts(accounts_file, creators)
            campaigns = load_campaigns(campaigns_file, creators, accounts)
            self._value = ({a.id: a for a in accounts.accounts}, campaigns.by_id())
            self._stamp = stamp
        return self._value
