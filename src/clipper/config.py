"""Configuration: creators + settings from YAML, secrets from the environment / .env.

Filesystem locations (data/output dirs, DB path) are deliberately NOT configurable from YAML:
they come from CLI options / env so a config edit can never point deletion jobs elsewhere.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/creators.yaml")
MIN_SESSION_SECRET_LENGTH = 32
# Letters/digits first; then letters, digits, space and a few punctuation marks. No path
# separators, no leading dot/dash, no control characters.
CREATOR_NAME_PATTERN = r"^\w[\w .'&()-]{0,99}$"
_RCLONE_REMOTE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*:\S*$")


class ConfigError(Exception):
    """Config file missing or invalid. Message is safe to show to the operator."""


class MissingSecretError(Exception):
    """A secret needed by the current operation is not set (or is unusable)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Settings(_Strict):
    poll_interval_youtube_minutes: int = Field(10, ge=1)
    poll_interval_twitch_minutes: int = Field(5, ge=1)
    first_run_mode: Literal["baseline", "emit"] = "baseline"
    lookback_days: int = Field(7, ge=0)
    min_duration_seconds: int = Field(300, ge=0)
    max_duration_seconds: int = Field(57_600, ge=60)
    clip_min_seconds: int = Field(20, ge=5)
    clip_max_seconds: int = Field(60, ge=5)
    max_clips_per_source: int = Field(3, ge=1)
    max_clips_per_stream: int = Field(10, ge=1)
    max_source_hours_per_day: float = Field(24, gt=0)
    daily_llm_usd_cap: float = Field(10.0, ge=0)
    max_llm_windows: int = Field(8, ge=1)
    daily_transcribe_usd_cap: float = Field(5.0, ge=0)
    disk_budget_gb: int = Field(150, ge=1)
    media_retention_hours: int = Field(48, ge=1)
    review_expiry_days: int = Field(7, ge=1)

    @model_validator(mode="after")
    def _clip_range(self) -> Settings:
        if self.clip_min_seconds >= self.clip_max_seconds:
            raise ValueError("clip_min_seconds must be less than clip_max_seconds")
        return self


class CreatorOverrides(_Strict):
    """Per-creator overrides; any field left unset falls back to global settings."""

    min_duration_seconds: int | None = Field(None, ge=0)
    max_duration_seconds: int | None = Field(None, ge=60)
    clip_min_seconds: int | None = Field(None, ge=5)
    clip_max_seconds: int | None = Field(None, ge=5)
    max_clips_per_source: int | None = Field(None, ge=1)
    max_clips_per_stream: int | None = Field(None, ge=1)
    max_source_hours_per_day: float | None = Field(None, gt=0)
    max_llm_windows: int | None = Field(None, ge=1)


class YouTubeChannel(_Strict):
    handle: str = Field(pattern=r"^@[\w.\-]{3,100}$")
    channel_id: str | None = Field(None, pattern=r"^UC[\w-]{22}$")


class TwitchChannel(_Strict):
    login: str = Field(pattern=r"^[a-z0-9_]{3,25}$")
    user_id: str | None = Field(None, pattern=r"^\d+$")


class Creator(_Strict):
    name: str = Field(pattern=CREATOR_NAME_PATTERN)
    region: Literal["global", "ph"]
    language: str = Field("en", pattern=r"^[a-z]{2,3}$")
    tags: tuple[str, ...] = ()
    youtube: YouTubeChannel | None = None
    twitch: TwitchChannel | None = None
    overrides: CreatorOverrides = CreatorOverrides()

    @model_validator(mode="after")
    def _has_platform(self) -> Creator:
        if self.youtube is None and self.twitch is None:
            raise ValueError("creator must have at least one platform (youtube or twitch)")
        return self


def _first_duplicate(values: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            return value
        seen.add(key)
    return None


class AppConfig(_Strict):
    settings: Settings = Settings()
    creators: tuple[Creator, ...] = ()

    @model_validator(mode="after")
    def _check_creators(self) -> AppConfig:
        identities: list[tuple[str, Callable[[Creator], str | None]]] = [
            ("creator name", lambda c: c.name),
            ("youtube handle", lambda c: c.youtube.handle if c.youtube else None),
            ("youtube channel_id", lambda c: c.youtube.channel_id if c.youtube else None),
            ("twitch login", lambda c: c.twitch.login if c.twitch else None),
            ("twitch user_id", lambda c: c.twitch.user_id if c.twitch else None),
        ]
        for label, get in identities:
            values = [v for v in (get(c) for c in self.creators) if v is not None]
            duplicate = _first_duplicate(values)
            if duplicate is not None:
                raise ValueError(f"duplicate {label}: {duplicate}")
        for creator in self.creators:
            try:
                self.effective_settings(creator.name)
            except ValidationError as exc:
                problems = "; ".join(e["msg"] for e in exc.errors())
                msg = f"creator {creator.name!r} overrides are invalid: {problems}"
                raise ValueError(msg) from None
        return self

    def creator(self, name: str) -> Creator:
        key = name.casefold()
        for creator in self.creators:
            if creator.name.casefold() == key:
                return creator
        raise KeyError(name)

    def effective_settings(self, name: str) -> Settings:
        overrides = self.creator(name).overrides.model_dump(exclude_none=True)
        return Settings.model_validate({**self.settings.model_dump(), **overrides})


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader) or {}  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"invalid YAML in {path}: top level must be a mapping")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{_format_errors(exc)}") from exc


class Secrets(BaseSettings):
    """API keys and credentials. Values are SecretStr so they never render in repr/logs.

    Reads the process environment and `.env` in the current directory (Docker injects env).
    Unknown keys in .env are ignored.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    youtube_api_key: SecretStr | None = None
    twitch_client_id: SecretStr | None = None
    twitch_client_secret: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    discord_alerts_webhook_url: SecretStr | None = None
    healthcheck_url_monitor: SecretStr | None = None
    healthcheck_url_worker: SecretStr | None = None
    review_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    rclone_remote: str | None = None

    @field_validator("rclone_remote")
    @classmethod
    def _valid_remote(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not _RCLONE_REMOTE.match(value):
            raise ValueError("RCLONE_REMOTE must look like 'remote:path' (no leading '-')")
        return value

    def require(self, name: str) -> str:
        """Return the secret's plain value, or raise if it is unset/empty/unusable."""
        field = name.lower()
        if field not in type(self).model_fields:
            raise KeyError(name)
        value = getattr(self, field)
        plain = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not plain:
            raise MissingSecretError(f"{name.upper()} is not set; add it to .env")
        if field == "session_secret" and len(plain) < MIN_SESSION_SECRET_LENGTH:
            raise MissingSecretError(
                f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters"
            )
        return plain
