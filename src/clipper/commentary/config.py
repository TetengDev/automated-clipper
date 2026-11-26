"""Commentary channels (CLP-143): `config/commentary.yaml`, one entry per channel.

`defaults` apply to every channel unless the channel's `overrides` change them; an
override replaces the default value. Each channel names its accounts.yaml account (which
must be `content: commentary`, one channel per account), its discovery `source`, its
script `prompt` (prompts/<prompt>.md, CLP-146) and that source's inputs. For
`twitch_clips`:

- `games`: the Twitch categories the channel covers, at least one, each with optional
  `aliases` (how YouTube titles name it, CLP-152) and its Twitch `game_id`.
- `streamers`: optional pinned streamers, each on Twitch (`twitch: <login>`) or on YouTube
  (`youtube: {handle, channel_id}`, scanned from CLP-152). Pinned streamers are always
  scanned and boosted by `pinned_boost`; only their clips of the channel's games count.
- `exclude_streamers`: Twitch logins or YouTube handles never used, even when trending.

`clipper resolve` (CLP-144) fills in `game_id` and Twitch `user_id`. A missing file is an
empty config: no commentary jobs are made. `CLIPPER_COMMENTARY` reads the file from
somewhere else.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from clipper.accounts import ACCOUNT_ID_PATTERN, Account, AccountsConfig, _yaml_problem
from clipper.config import (
    CREATOR_NAME_PATTERN,
    ConfigError,
    YouTubeChannel,
    _first_duplicate,
    _format_errors,
    _UniqueKeyLoader,
)

COMMENTARY_ENV = "CLIPPER_COMMENTARY"
COMMENTARY_FILENAME = "commentary.yaml"
TWITCH_LOGIN_PATTERN = r"^[a-z0-9_]{3,25}$"
"""A Twitch login, lower case: the same rule as creators.yaml (`config.TwitchChannel`)."""
TWITCH_ID_PATTERN = r"^[1-9][0-9]{0,19}$"
"""A Twitch numeric id (game id, user id), kept as text. ASCII digits only."""
EXCLUDED_PATTERN = r"^(?:[a-z0-9_]{3,25}|@[A-Za-z0-9_.-]{3,100})$"
"""An excluded streamer: a Twitch login, or a YouTube handle (as in `config.YouTubeChannel`)."""
_TWITCH_ID = re.compile(TWITCH_ID_PATTERN.strip("^$"))
_RUN_AT_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
_VOICE_PATTERN = r"^[a-z]{2}_[a-z]+$"

Source = Literal["twitch_clips"]


def _readable(value: str) -> str:
    if not value.isprintable() or value != value.strip():
        raise ValueError("a game name must be printable, without surrounding spaces")
    return value


GameName = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_readable)]
Excluded = Annotated[str, Field(pattern=EXCLUDED_PATTERN)]


def _quoted_time(value: Any) -> Any:
    """YAML 1.1 reads an unquoted 16:30 as the base-60 integer 990."""
    if isinstance(value, int) and not isinstance(value, bool):
        raise ValueError('quote the time in YAML, e.g. run_at: "16:30"')
    return value


def _id_as_text(value: Any) -> Any:
    """Twitch ids are numbers kept as text; YAML reads a bare one as an integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


def _ids_as_text(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return [_id_as_text(v) for v in value]
    return value


class CommentarySettings(BaseModel):
    """A channel's settings: `defaults`, then the channel's `overrides`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_at: str = Field("06:00", pattern=_RUN_AT_PATTERN)
    """Local time, in the account's timezone, from which the monitor makes the day's job."""
    candidates_per_day: int = Field(12, ge=1, le=100)
    stories_per_day: int = Field(3, ge=1, le=10)
    trending_boost: float = Field(0.5, ge=0, le=10)
    """Score boost for a clip whose game is in Twitch's top 20 today."""
    pinned_boost: float = Field(0.5, ge=0, le=10)
    """Score boost for a clip from a pinned streamer."""
    min_clip_views: int = Field(10000, ge=0)
    """Virality bar (CLP-144): a clip qualifies with at least this many views ..."""
    min_views_per_hour: float = Field(400, ge=0)
    """... or at least this many views an hour, view_count / max(age_hours, 1). The default
    is 10,000 views over the 24 h scan window, rounded down."""
    min_score: float | None = Field(None, ge=0)
    """Score floor: a clip scoring lower is dropped. None: no floor. The default is decided
    from the first week's real scores (spec, open question 1)."""
    max_streamers_scanned: int = Field(50, ge=1, le=500)
    """Trending streamers kept from each day's game scan, besides the pinned ones."""
    excluded_game_ids: tuple[str, ...] = ()
    """Twitch game ids to skip, on top of the gambling categories the code always skips."""
    voice: str = Field("am_michael", pattern=_VOICE_PATTERN)
    music_dir: Path | None = None
    """Background music folder (absolute); None: no music."""

    _run_at_quoted = field_validator("run_at", mode="before")(_quoted_time)
    _game_ids_text = field_validator("excluded_game_ids", mode="before")(_ids_as_text)

    @field_validator("excluded_game_ids")
    @classmethod
    def _numeric_game_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for game_id in value:
            if not _TWITCH_ID.fullmatch(game_id):
                raise ValueError(f"excluded_game_ids: {game_id!r} is not a Twitch game id")
        if len(set(value)) != len(value):
            raise ValueError("excluded_game_ids: duplicate id")
        return value

    @field_validator("music_dir")
    @classmethod
    def _absolute_music_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("music_dir must be an absolute path, e.g. /data/music")
        return value

    @model_validator(mode="after")
    def _stories_fit(self) -> CommentarySettings:
        if self.stories_per_day > self.candidates_per_day:
            raise ValueError(
                f"stories_per_day {self.stories_per_day} is above candidates_per_day"
                f" {self.candidates_per_day}"
            )
        return self

    @property
    def run_at_time(self) -> time:
        hours, minutes = self.run_at.split(":")
        return time(int(hours), int(minutes))


class CommentaryOverrides(BaseModel):
    """A channel's changes to `defaults`; anything left out keeps the default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_at: str | None = Field(None, pattern=_RUN_AT_PATTERN)
    candidates_per_day: int | None = Field(None, ge=1, le=100)
    stories_per_day: int | None = Field(None, ge=1, le=10)
    trending_boost: float | None = Field(None, ge=0, le=10)
    pinned_boost: float | None = Field(None, ge=0, le=10)
    min_clip_views: int | None = Field(None, ge=0)
    min_views_per_hour: float | None = Field(None, ge=0)
    min_score: float | None = Field(None, ge=0)
    """An override can set a floor; with `exclude_none` it cannot clear one back to None."""
    max_streamers_scanned: int | None = Field(None, ge=1, le=500)
    excluded_game_ids: tuple[str, ...] | None = None
    voice: str | None = Field(None, pattern=_VOICE_PATTERN)
    music_dir: Path | None = None

    _run_at_quoted = field_validator("run_at", mode="before")(_quoted_time)
    _game_ids_text = field_validator("excluded_game_ids", mode="before")(_ids_as_text)


class Game(BaseModel):
    """A Twitch category the channel covers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: GameName
    """The category name as Twitch shows it, e.g. Grand Theft Auto VI."""
    aliases: list[GameName] = Field(default_factory=list)
    """Other names for it, e.g. GTA 6; YouTube discovery (CLP-152) matches titles on them."""
    game_id: str | None = Field(None, pattern=TWITCH_ID_PATTERN)
    """Twitch's id for the category; filled in or confirmed by `clipper resolve` (CLP-144)."""

    _game_id_text = field_validator("game_id", mode="before")(_id_as_text)


class Streamer(BaseModel):
    """A pinned streamer: always scanned and boosted by `pinned_boost`; only their clips of
    the channel's games count. On Twitch or on YouTube, never both."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=CREATOR_NAME_PATTERN)
    twitch: str | None = Field(None, pattern=TWITCH_LOGIN_PATTERN)
    """Twitch login, lower case."""
    user_id: str | None = Field(None, pattern=TWITCH_ID_PATTERN)
    """Twitch's id for the login; filled in by `clipper resolve` (CLP-144)."""
    youtube: YouTubeChannel | None = None
    """A YouTube streamer's channel (handle, channel_id); scanned from CLP-152."""

    _user_id_text = field_validator("user_id", mode="before")(_id_as_text)

    @model_validator(mode="after")
    def _one_platform(self) -> Streamer:
        if (self.twitch is None) == (self.youtube is None):
            raise ValueError(
                f"pinned streamer {self.name!r}: set exactly one of `twitch` or `youtube`"
            )
        if self.user_id is not None and self.twitch is None:
            raise ValueError(
                f"pinned streamer {self.name!r}: user_id is a Twitch id; set it only with `twitch`"
            )
        return self


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account: str = Field(pattern=ACCOUNT_ID_PATTERN, max_length=64)
    """An accounts.yaml id with `content: commentary`."""
    source: Source
    prompt: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    """Script prompt name: prompts/<prompt>.md (CLP-146)."""
    games: list[Game] = Field(default_factory=list)
    streamers: list[Streamer] = Field(default_factory=list)
    """Pinned streamers; may be empty."""
    exclude_streamers: list[Excluded] = Field(default_factory=list)
    """Twitch logins or YouTube handles never used, even when trending: always wins."""
    overrides: CommentaryOverrides = CommentaryOverrides()

    @model_validator(mode="after")
    def _check_inputs(self) -> Channel:
        if self.source == "twitch_clips" and not self.games:
            raise ValueError("a twitch_clips channel needs at least one game in `games`")
        twitch = [s.twitch for s in self.streamers if s.twitch is not None]
        youtube = [s.youtube for s in self.streamers if s.youtube is not None]
        for label, values in (
            ("game name or alias", [n for g in self.games for n in (g.name, *g.aliases)]),
            ("game_id", [g.game_id for g in self.games if g.game_id]),
            ("pinned streamer name", [s.name for s in self.streamers]),
            ("pinned twitch login", twitch),
            ("pinned youtube handle", [y.handle for y in youtube]),
            ("pinned youtube channel_id", [y.channel_id for y in youtube if y.channel_id]),
            ("excluded streamer", self.exclude_streamers),
        ):
            duplicate = _first_duplicate(values)
            if duplicate is not None:
                raise ValueError(f"duplicate {label}: {duplicate}")
        pinned = {p.casefold() for p in [*twitch, *(y.handle for y in youtube)]}
        both = sorted(pinned & {e.casefold() for e in self.exclude_streamers})
        if both:
            raise ValueError(
                f"{', '.join(both)} is both pinned in `streamers` and in `exclude_streamers`"
            )
        return self


class CommentaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    defaults: CommentarySettings = CommentarySettings()
    channels: list[Channel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_channels(self) -> CommentaryConfig:
        seen: set[str] = set()
        for channel in self.channels:
            if channel.account in seen:
                raise ValueError(f"account {channel.account!r} is used by two channels")
            seen.add(channel.account)
            try:
                self.settings_for(channel)
            except ValidationError as exc:
                problems = "; ".join(e["msg"] for e in exc.errors())
                msg = f"channel {channel.account!r} overrides are invalid: {problems}"
                raise ValueError(msg) from None
        return self

    def settings_for(self, channel: Channel) -> CommentarySettings:
        overrides = channel.overrides.model_dump(exclude_none=True)
        return CommentarySettings.model_validate({**self.defaults.model_dump(), **overrides})


def commentary_path(config_path: Path, env: Mapping[str, str] | None = None) -> Path:
    """`CLIPPER_COMMENTARY`, else commentary.yaml next to the creators config."""
    env = os.environ if env is None else env
    override = env.get(COMMENTARY_ENV)
    return Path(override) if override else config_path.parent / COMMENTARY_FILENAME


def _channel_problems(channel: Channel, account: Account | None) -> list[str]:
    where = f"channel {channel.account!r}"
    if account is None:
        return [f"{where}: no account {channel.account!r} in accounts.yaml"]
    if account.content != "commentary":
        return [f"{where}: account {account.id!r} is not `content: commentary`"]
    return []


def load_commentary(path: Path, accounts: AccountsConfig) -> CommentaryConfig:
    """Load and validate commentary.yaml against the accounts. A missing file is an empty
    config."""
    if not path.exists():
        return CommentaryConfig()
    cfg = _read(path)
    by_id = {a.id: a for a in accounts.accounts}
    problems = [p for c in cfg.channels for p in _channel_problems(c, by_id.get(c.account))]
    if problems:
        details = "\n".join(f"  {p}" for p in problems)
        raise ConfigError(f"invalid commentary config {path}:\n{details}")
    return cfg


def unchanneled_accounts(accounts: AccountsConfig, commentary: CommentaryConfig) -> list[str]:
    """Warnings for commentary accounts with no channel in commentary.yaml: they get no
    daily Short."""
    channels = {c.account for c in commentary.channels}
    return [
        f"commentary account {a.id!r} has no channel in commentary.yaml; it gets no daily Short"
        for a in accounts.accounts
        if a.content == "commentary" and a.id not in channels
    ]


def summary(channel: Channel, settings: CommentarySettings, account: Account) -> str:
    """One line for `clipper check-config`."""
    games = ", ".join(g.name for g in channel.games) or "none"
    twitch = sum(1 for s in channel.streamers if s.twitch is not None)
    youtube = len(channel.streamers) - twitch
    line = (
        f"{channel.account} {channel.source}, prompt {channel.prompt}; games {games}; "
        f"{len(channel.streamers)} pinned ({twitch} twitch, {youtube} youtube), "
        f"{len(channel.exclude_streamers)} excluded; "
        f"daily at {settings.run_at} {account.timezone}, "
        f"{settings.candidates_per_day} candidates, {settings.stories_per_day} stories, "
        f"voice {settings.voice}"
    )
    if settings.music_dir is not None:
        line += f", music from {settings.music_dir}"
    return line if account.enabled else f"{line} (account disabled: no daily job)"


def youtube_pins_note(channel: Channel) -> str | None:
    """A check-config note while YouTube discovery (CLP-152) is not built: the channel's
    YouTube pins are listed but not scanned. None when it has none."""
    names = [s.name for s in channel.streamers if s.youtube is not None]
    if not names:
        return None
    return (
        f"channel {channel.account}: YouTube pinned streamers are not scanned until CLP-152"
        f" ships: {', '.join(names)}"
    )


def _read(path: Path) -> CommentaryConfig:
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
        return CommentaryConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid commentary config {path}:\n{_format_errors(exc)}") from exc
