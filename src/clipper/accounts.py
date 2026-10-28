"""Posting accounts (CLP-91): where clips are posted, when, and with which tokens.

`config/accounts.yaml` lists the destination accounts. Token secrets never live in YAML:
they come from the environment (or `.env`), keyed by account id, as
`CLIPPER_<KIND>_<ACCOUNT_ID>` (see `token_env_name`), and are held as `SecretStr` so they
never render in repr, str, logs or JSON.
"""

from __future__ import annotations

import os
import re
import unicodedata
import zoneinfo
from collections.abc import Mapping
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from clipper.config import (
    AppConfig,
    ConfigError,
    ContentType,
    Creator,
    _first_duplicate,
    _format_errors,
    _UniqueKeyLoader,
    always_sensitive,
)

ACCOUNTS_ENV = "CLIPPER_ACCOUNTS"
ACCOUNTS_FILENAME = "accounts.yaml"
ACCOUNT_ID_PATTERN = r"^[a-z0-9_]+$"
_ACCOUNT_ID = re.compile(r"[a-z0-9_]+")
TOKEN_KIND_PATTERN = re.compile(r"[A-Za-z0-9_]+")
TOKEN_ENV_PREFIX = "CLIPPER_"  # noqa: S105 - a name prefix, not a secret

Platform = Literal["tiktok", "instagram", "youtube", "facebook"]

# Most posts a day an account may be configured for. Platform limits are higher or
# unpublished; these keep a misconfigured account from looking like spam.
PLATFORM_DAILY_CAPS: dict[str, int] = {
    "tiktok": 15,
    "facebook": 30,
    "instagram": 50,
    "youtube": 100,
}

# Patterns are used with fullmatch: `$` in `re` would also accept a trailing newline.
_MARKET = re.compile(r"[A-Z]{2}")
_WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")

# The app's own CLIPPER_* settings. None of them is ever read as an account token, and an
# account id that would make one of them look like a token is refused.
APP_ENV_NAMES = frozenset(
    {
        "CLIPPER_ACCOUNTS",
        "CLIPPER_CONFIG",
        "CLIPPER_DB",
        "CLIPPER_DEV",
        "CLIPPER_FORWARDED_ALLOW_IPS",
        "CLIPPER_IMAGE",
        "CLIPPER_IMAGE_TAG",
        "CLIPPER_MEDIA_DIR",
        "CLIPPER_MODELS_DIR",
        "CLIPPER_OUTPUT_DIR",
        "CLIPPER_RENDER_DIR",
        "CLIPPER_SLOW_TESTS",
        "CLIPPER_WORKER_ID",
        "CLIPPER_YTDLP_COOKIES",
        "CLIPPER_YTDLP_PROXY",
    }
)


class Plan(BaseModel):
    """How often and when an account posts, in the account's local time."""

    model_config = ConfigDict(frozen=True)

    posts_per_day: int
    min_gap_minutes: int
    windows: tuple[str, ...]


# Default posting plans from the research in the epic (CLP-90), by market then platform.
_PH_PLAN = Plan(
    posts_per_day=2,
    min_gap_minutes=180,
    windows=("07:00-09:00", "12:00-13:00", "18:00-21:00"),
)
_DEFAULT_PLANS: dict[str, dict[str, Plan]] = {
    "US": {
        "tiktok": Plan(
            posts_per_day=3, min_gap_minutes=180, windows=("14:00-17:00", "18:00-22:00")
        ),
        "youtube": Plan(
            posts_per_day=2, min_gap_minutes=240, windows=("10:00-11:00", "13:00-14:00")
        ),
        "instagram": Plan(posts_per_day=2, min_gap_minutes=240, windows=("10:00-15:00",)),
        "facebook": Plan(posts_per_day=2, min_gap_minutes=240, windows=("10:00-15:00",)),
    },
    "PH": {platform: _PH_PLAN for platform in PLATFORM_DAILY_CAPS},
}
_PLAN_FIELDS = ("posts_per_day", "min_gap_minutes", "windows")


def default_plan(platform: Platform, market: str) -> Plan:
    """The research-backed posting plan for a platform in a market. KeyError when there
    is none: the account must then set posts_per_day, min_gap_minutes and windows."""
    return _DEFAULT_PLANS[market][platform]


@cache
def _timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _window_bounds(window: str) -> tuple[int, int]:
    if not _WINDOW.fullmatch(window):
        raise ValueError(f"window {window!r} must look like HH:MM-HH:MM (24-hour clock)")
    start, end = (_minutes(part) for part in window.split("-"))
    if start >= end:
        raise ValueError(f"window {window!r}: start must be before end")
    return start, end


def _posts_that_fit(windows: list[str], posts_per_day: int, min_gap_minutes: int) -> int:
    """How many posts fit in the windows with at least `min_gap_minutes` between them.
    Greedy from the earliest window; a post may go out at any minute from a window's
    start through its end."""
    count, earliest = 0, 0
    for start, end in sorted(_window_bounds(w) for w in windows):
        t = max(earliest, start)
        while t <= end and count < posts_per_day:
            count += 1
            t += max(min_gap_minutes, 1)
        earliest = t
    return count


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ACCOUNT_ID_PATTERN, max_length=64)
    platform: Platform
    handle: str = Field(pattern=r"^@?[A-Za-z0-9_.-]{1,100}$")
    market: str = Field(pattern=r"^[A-Z]{2}$")
    """ISO 3166-1 alpha-2 country code, e.g. US or PH."""
    timezone: str
    creators: list[str] = Field(min_length=1)
    """Names from creators.yaml whose clips this account may post."""
    posts_per_day: int = Field(ge=1)
    min_gap_minutes: int = Field(ge=0)
    windows: list[str] = Field(min_length=1)
    """Local-time posting windows, `HH:MM-HH:MM`, non-overlapping."""
    warmup_start: date | None = None
    warmup_days: int = Field(14, ge=1)
    campaign: str | None = Field(None, pattern=ACCOUNT_ID_PATTERN, max_length=64)
    licence: str | None = Field(None, max_length=200)
    """Reference to the signed licence for the one creator on this account that needs it."""
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _fill_default_plan(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        missing = [name for name in _PLAN_FIELDS if data.get(name) is None]
        if not missing:
            return data
        platform, market = data.get("platform"), data.get("market")
        if platform not in PLATFORM_DAILY_CAPS or not _MARKET.fullmatch(str(market)):
            return data  # the field validators report the bad platform or market
        try:
            plan = default_plan(platform, market)
        except KeyError:
            raise ValueError(
                f"no default posting plan for market {market!r}; "
                f"set {', '.join(missing)} on the account"
            ) from None
        defaults = plan.model_dump()
        defaults["windows"] = list(plan.windows)
        return {**data, **{name: defaults[name] for name in missing}}

    @field_validator("market", mode="before")
    @classmethod
    def _quoted_market(cls, value: Any) -> Any:
        if isinstance(value, bool):  # YAML 1.1 reads a bare NO (Norway) as false
            raise ValueError('quote the country code in YAML, e.g. market: "NO"')
        return value

    @field_validator("creators")
    @classmethod
    def _distinct_creators(cls, value: list[str]) -> list[str]:
        duplicate = _first_duplicate(value)
        if duplicate is not None:
            raise ValueError(f"duplicate creator: {duplicate}")
        return value

    @field_validator("licence")
    @classmethod
    def _readable_licence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # No control, format (bidi overrides, zero-width) or other C* characters: the
        # reference is shown to the operator and must read as what it is.
        if any(unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("licence must not contain control or invisible characters")
        if len(value.strip()) < 3:
            raise ValueError("licence must name the signed licence (at least 3 characters)")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value not in _timezones():
            raise ValueError(f"unknown timezone {value!r} (use an IANA name like America/New_York)")
        return value

    @field_validator("windows")
    @classmethod
    def _valid_windows(cls, value: list[str]) -> list[str]:
        bounds = sorted((*_window_bounds(w), w) for w in value)
        for (_, prev_end, prev), (start, _, window) in zip(bounds, bounds[1:], strict=False):
            if start < prev_end:
                raise ValueError(f"windows {prev!r} and {window!r} overlap")
        return value

    @field_validator("posts_per_day")
    @classmethod
    def _within_cap(cls, value: int, info: ValidationInfo) -> int:
        platform = info.data.get("platform")  # absent when the platform itself is invalid
        cap = PLATFORM_DAILY_CAPS.get(platform) if platform else None
        if cap is not None and value > cap:
            raise ValueError(f"posts_per_day {value} exceeds the {platform} cap of {cap}")
        return value

    @model_validator(mode="after")
    def _plan_fits(self) -> Account:
        fits = _posts_that_fit(self.windows, self.posts_per_day, self.min_gap_minutes)
        if fits < self.posts_per_day:
            raise ValueError(
                f"posts_per_day {self.posts_per_day} cannot fit: windows "
                f"{', '.join(self.windows)} with a {self.min_gap_minutes}-minute gap hold "
                f"only {fits}"
            )
        return self


class AccountsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accounts: list[Account] = []

    @model_validator(mode="after")
    def _unique(self) -> AccountsConfig:
        ids: set[str] = set()
        handles: set[tuple[str, str]] = set()
        for account in self.accounts:
            if account.id in ids:
                raise ValueError(f"duplicate account id: {account.id}")
            ids.add(account.id)
            key = (account.platform, account.handle.removeprefix("@").casefold())
            if key in handles:
                raise ValueError(f"duplicate {account.platform} handle: {account.handle}")
            handles.add(key)
        for account_id in sorted(ids):
            for other in sorted(ids):
                if other != account_id and other.endswith(f"_{account_id}"):
                    raise ValueError(
                        f"account id {account_id!r} ends account id {other!r}: their token "
                        "environment variable names would be ambiguous; rename one"
                    )
            suffix = f"_{account_id}".upper()
            for name in sorted(APP_ENV_NAMES):
                if name.endswith(suffix):
                    raise ValueError(
                        f"account id {account_id!r} is ambiguous: the app setting {name} "
                        "would read as one of its tokens; rename the account"
                    )
        return self


def needs_licence(creator: Creator) -> bool:
    """Whether posting this creator's clips needs a licence reference on the account.

    Builds on the CLP-87 rule (`always_sensitive`: public_service, or no content type,
    which fails closed) and adds vlog_lifestyle, whose clips are the creator's private life.
    """
    return always_sensitive(creator) or creator.content_type == ContentType.VLOG_LIFESTYLE


def _creator_problems(account: Account, creators: AppConfig) -> list[str]:
    problems = []
    licensed: list[str] = []
    for name in account.creators:
        try:
            creator = creators.creator(name)
        except KeyError:
            problems.append(f"account {account.id!r}: unknown creator {name!r}")
            continue
        if not needs_licence(creator):
            continue
        licensed.append(name)
        if account.licence is None:
            kind = creator.content_type.value if creator.content_type else "no content_type"
            problems.append(
                f"account {account.id!r}: creator {name!r} ({kind}) needs a licence; "
                "set `licence:` on the account or remove the creator"
            )
    if account.licence is not None and len(licensed) > 1:
        names = ", ".join(repr(n) for n in licensed)
        problems.append(
            f"account {account.id!r}: creators {names} each need their own licence, but an "
            "account holds one; give each of them a separate account"
        )
    return problems


def _canonical(account: Account, creators: AppConfig) -> Account:
    """The account with creator names spelled as in creators.yaml (matching ignores case)."""
    names = [creators.creator(name).name for name in account.creators]
    return account.model_copy(update={"creators": names})


def accounts_path(config_path: Path, env: Mapping[str, str] | None = None) -> Path:
    """`CLIPPER_ACCOUNTS`, else accounts.yaml next to the creators config."""
    env = os.environ if env is None else env
    override = env.get(ACCOUNTS_ENV)
    return Path(override) if override else config_path.parent / ACCOUNTS_FILENAME


def load_accounts(path: Path, creators: AppConfig) -> AccountsConfig:
    """Load and validate accounts.yaml against the creators config. A missing file is an
    empty config: the pipeline runs without posting."""
    if not path.exists():
        return AccountsConfig()
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
        cfg = AccountsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid accounts config {path}:\n{_format_errors(exc)}") from exc
    problems = [p for account in cfg.accounts for p in _creator_problems(account, creators)]
    if problems:
        details = "\n".join(f"  {p}" for p in problems)
        raise ConfigError(f"invalid accounts config {path}:\n{details}")
    return AccountsConfig(accounts=[_canonical(a, creators) for a in cfg.accounts])


def _yaml_problem(exc: yaml.YAMLError) -> str:
    """The parser's problem and position, without PyYAML's quote of the offending line: a
    token pasted into the file by mistake must not end up in the terminal or CI logs."""
    problem = getattr(exc, "problem", None) or type(exc).__name__
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return problem
    return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"


def token_env_name(account_id: str, kind: str) -> str:
    """`CLIPPER_<KIND>_<ACCOUNT_ID>`, e.g. CLIPPER_ACCESS_TOKEN_MRBEAST_CLIPS_TT."""
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError(f"invalid account id {account_id!r}")
    if not TOKEN_KIND_PATTERN.fullmatch(kind):
        raise ValueError(f"invalid token kind {kind!r}")
    return f"{TOKEN_ENV_PREFIX}{kind}_{account_id}".upper()


class AccountTokens(BaseModel):
    """An account's token secrets. SecretStr: repr, str, logs and JSON show `**********`."""

    model_config = ConfigDict(frozen=True)

    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    extra: dict[str, SecretStr] = {}
    """Any other `CLIPPER_<KIND>_<ACCOUNT_ID>` values, keyed by lower-case kind."""


_NAMED_KINDS = ("access_token", "refresh_token")


def token_environment() -> dict[str, str]:
    """The process environment over `.env` in the current directory, the same sources and
    precedence as `config.Secrets`."""
    dotenv = {k: v for k, v in dotenv_values(".env").items() if v is not None}
    return {**dotenv, **os.environ}


def account_tokens(account_id: str, env: Mapping[str, str] | None = None) -> AccountTokens:
    """Read an account's token secrets from `env` (default: process env and `.env`).
    Empty values count as missing."""
    env = token_environment() if env is None else env
    suffix = f"_{account_id}".upper()
    found: dict[str, SecretStr] = {}
    for name, value in env.items():
        if not value or not name.startswith(TOKEN_ENV_PREFIX) or not name.endswith(suffix):
            continue
        if name != name.upper() or name in APP_ENV_NAMES:
            continue  # only the exact upper-case names, and never the app's own settings
        kind = name[len(TOKEN_ENV_PREFIX) : -len(suffix)].lower()
        if kind and TOKEN_KIND_PATTERN.fullmatch(kind):
            found[kind] = SecretStr(value)
    named = {kind: found.pop(kind, None) for kind in _NAMED_KINDS}
    # A kind that itself starts with a named token kind belongs to another account whose
    # id ends with this one (AccountsConfig refuses such ids; this guards direct calls).
    extra = {
        k: v for k, v in found.items() if not k.startswith(tuple(f"{n}_" for n in _NAMED_KINDS))
    }
    return AccountTokens(**named, extra=extra)


def token_status(tokens: AccountTokens) -> str:
    """`access_token=set refresh_token=missing ...`: names and presence only, never values."""
    parts = [f"{kind}={'set' if getattr(tokens, kind) else 'missing'}" for kind in _NAMED_KINDS]
    parts += [f"{kind}=set" for kind in sorted(tokens.extra)]
    return " ".join(parts)
