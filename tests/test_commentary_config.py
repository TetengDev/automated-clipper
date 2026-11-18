"""Commentary channels config: config/commentary.yaml per channel (CLP-143)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from clipper.accounts import AccountsConfig, load_accounts
from clipper.cli import app
from clipper.commentary.config import (
    COMMENTARY_ENV,
    commentary_path,
    load_commentary,
)
from clipper.config import ConfigError, load_config

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]

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
  - id: random_gaming_yt
    platform: youtube
    content: commentary
    channel_id: UCcMYw3Ae2crqXE2yzFBSUig
    handle: "@randomgaming"
    market: US
    timezone: America/New_York
  - id: mrbeast_clips_yt
    platform: youtube
    handle: "@MrBeastClipsDaily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
"""

COMMENTARY_YAML = """\
defaults:
  run_at: "06:00"
  candidates_per_day: 12
  stories_per_day: 3
  trending_boost: 0.5
  pinned_boost: 0.5
  min_clip_views: 1000
  max_streamers_scanned: 50
  excluded_game_ids: []
  voice: am_michael
  music_dir: null
channels:
  - account: random_gaming_yt
    source: twitch_clips
    prompt: commentary_gaming_v1
    games:
      - name: Grand Theft Auto VI
        aliases: [GTA 6, GTA VI]
    streamers:
      - name: Kai Cenat
        twitch: kaicenat
      - name: Jynxzi
        twitch: jynxzi
      - name: IShowSpeed
        youtube:
          handle: "@IShowSpeed"
          channel_id: UCWsDFcIhY2DBi3GB5uykGXA
    exclude_streamers: [ninja, "@SomeYouTuber"]
"""

SPEED_ID = "          channel_id: UCWsDFcIhY2DBi3GB5uykGXA\n"
GTA_ALIASES = "        aliases: [GTA 6, GTA VI]\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "commentary.yaml"
    path.write_text(text)
    return path


def _config_dir(tmp_path: Path, commentary: str | None = COMMENTARY_YAML) -> Path:
    """creators.yaml, accounts.yaml and (unless None) commentary.yaml in tmp_path."""
    creators = tmp_path / "creators.yaml"
    creators.write_text(CREATORS_YAML)
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS_YAML)
    if commentary is not None:
        _write(tmp_path, commentary)
    return creators


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsConfig:
    creators = _config_dir(tmp_path, commentary=None)
    return load_accounts(tmp_path / "accounts.yaml", load_config(creators))


def test_commentary_yaml_loads(tmp_path: Path, accounts: AccountsConfig) -> None:
    cfg = load_commentary(_write(tmp_path, COMMENTARY_YAML), accounts)

    (channel,) = cfg.channels
    assert channel.account == "random_gaming_yt"
    assert channel.source == "twitch_clips"
    assert channel.prompt == "commentary_gaming_v1"
    assert [(g.name, g.aliases, g.game_id) for g in channel.games] == [
        ("Grand Theft Auto VI", ["GTA 6", "GTA VI"], None)
    ]
    assert [(s.name, s.twitch, s.user_id) for s in channel.streamers] == [
        ("Kai Cenat", "kaicenat", None),
        ("Jynxzi", "jynxzi", None),
        ("IShowSpeed", None, None),
    ]
    speed = channel.streamers[2].youtube
    assert speed is not None
    assert (speed.handle, speed.channel_id) == ("@IShowSpeed", "UCWsDFcIhY2DBi3GB5uykGXA")
    assert channel.exclude_streamers == ["ninja", "@SomeYouTuber"]
    settings = cfg.settings_for(channel)
    assert settings.run_at == "06:00"
    assert settings.run_at_time == time(6, 0)
    assert (settings.candidates_per_day, settings.stories_per_day) == (12, 3)
    assert (settings.trending_boost, settings.pinned_boost) == (0.5, 0.5)
    assert (settings.min_clip_views, settings.max_streamers_scanned) == (1000, 50)
    assert settings.excluded_game_ids == ()
    assert settings.voice == "am_michael"
    assert settings.music_dir is None


def test_missing_commentary_file_is_empty_config(tmp_path: Path, accounts: AccountsConfig) -> None:
    assert load_commentary(tmp_path / "nope.yaml", accounts).channels == []


def test_defaults_apply_when_left_out(tmp_path: Path, accounts: AccountsConfig) -> None:
    text = COMMENTARY_YAML.split("channels:\n", 1)[1]
    cfg = load_commentary(_write(tmp_path, "channels:\n" + text), accounts)

    settings = cfg.settings_for(cfg.channels[0])
    assert (
        settings.run_at,
        settings.candidates_per_day,
        settings.pinned_boost,
        settings.min_clip_views,
        settings.max_streamers_scanned,
        settings.voice,
    ) == ("06:00", 12, 0.5, 1000, 50, "am_michael")


def test_aliases_pins_and_excludes_are_optional(tmp_path: Path, accounts: AccountsConfig) -> None:
    head = COMMENTARY_YAML.split("    streamers:\n", 1)[0].replace(GTA_ALIASES, "")

    cfg = load_commentary(_write(tmp_path, head), accounts)

    (channel,) = cfg.channels
    assert channel.streamers == [] and channel.exclude_streamers == []
    assert [(g.name, g.aliases) for g in channel.games] == [("Grand Theft Auto VI", [])]


def test_resolved_twitch_ids_are_kept_as_text(tmp_path: Path, accounts: AccountsConfig) -> None:
    text = COMMENTARY_YAML.replace(GTA_ALIASES, GTA_ALIASES + "        game_id: 1867279146\n")
    text = text.replace(
        "        twitch: kaicenat\n", '        twitch: kaicenat\n        user_id: "641972806"\n'
    )

    (channel,) = load_commentary(_write(tmp_path, text), accounts).channels

    assert channel.games[0].game_id == "1867279146"
    assert channel.streamers[0].user_id == "641972806"


def test_channel_overrides_change_only_what_they_set(
    tmp_path: Path, accounts: AccountsConfig
) -> None:
    text = (
        COMMENTARY_YAML
        + '    overrides:\n      run_at: "07:30"\n      voice: af_heart\n'
        + "      excluded_game_ids: [498566]\n      min_clip_views: 5000\n"
        + "      pinned_boost: 1.0\n      max_streamers_scanned: 20\n"
    )

    cfg = load_commentary(_write(tmp_path, text), accounts)

    settings = cfg.settings_for(cfg.channels[0])
    assert settings.run_at_time == time(7, 30)
    assert settings.voice == "af_heart"
    assert settings.excluded_game_ids == ("498566",)
    assert (settings.min_clip_views, settings.pinned_boost) == (5000, 1.0)
    assert settings.max_streamers_scanned == 20
    assert settings.candidates_per_day == 12


SECOND_YOUTUBE_PIN = '      - name: Speed Two\n        youtube:\n          handle: "@speedtwo"\n'


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "  voice: am_michael\n",
            "  voice: am_michael\n  surprise: 1\n",
            r"defaults\.surprise: Extra inputs are not permitted",
        ),
        (
            "    prompt: commentary_gaming_v1\n",
            "    prompt: commentary_gaming_v1\n    music: loud\n",
            r"channels\.0\.music: Extra inputs are not permitted",
        ),
        ('run_at: "06:00"', "run_at: 16:30", "quote the time"),
        ('run_at: "06:00"', 'run_at: "6am"', r"defaults\.run_at"),
        ("source: twitch_clips", "source: kick_clips", r"channels\.0\.source"),
        # games
        (
            "    games:\n      - name: Grand Theft Auto VI\n" + GTA_ALIASES,
            "    games: []\n",
            "a twitch_clips channel needs at least one game",
        ),
        (
            GTA_ALIASES,
            GTA_ALIASES + "      - name: grand theft auto vi\n",
            "duplicate game name or alias: grand theft auto vi",
        ),
        ("aliases: [GTA 6, GTA VI]", "aliases: [GTA 6, gta 6]", "duplicate game name or alias"),
        (GTA_ALIASES, GTA_ALIASES + '        game_id: "GTA"\n', r"games\.0\.game_id"),
        ("      - name: Grand Theft Auto VI\n", '      - name: ""\n', r"games\.0\.name"),
        ("aliases: [GTA 6, GTA VI]", 'aliases: [GTA 6, " GTA VI"]', r"games\.0\.aliases\.1"),
        (
            "      - name: Grand Theft Auto VI\n" + GTA_ALIASES,
            "      - Grand Theft Auto VI\n",
            r"games\.0",
        ),
        # pinned streamers
        ("twitch: jynxzi", "twitch: kaicenat", "duplicate pinned twitch login: kaicenat"),
        ("twitch: jynxzi", "twitch: Jynxzi", r"channels\.0\.streamers\.1\.twitch"),
        (
            "      - name: Jynxzi\n        twitch: jynxzi\n",
            "      - name: Jynxzi\n",
            "set exactly one of `twitch` or `youtube`",
        ),
        (SPEED_ID, SPEED_ID + "        twitch: ishowspeed\n", "set exactly one of `twitch`"),
        (SPEED_ID, SPEED_ID + '        user_id: "123"\n', "user_id is a Twitch id"),
        ("channel_id: UCWsDFcIhY2DBi3GB5uykGXA", "channel_id: UCshort", r"youtube\.channel_id"),
        ('handle: "@IShowSpeed"', "handle: IShowSpeed", r"streamers\.2\.youtube\.handle"),
        (
            SPEED_ID,
            SPEED_ID + SECOND_YOUTUBE_PIN + SPEED_ID,
            "duplicate pinned youtube channel_id: UCWsDFcIhY2DBi3GB5uykGXA",
        ),
        (
            SPEED_ID,
            SPEED_ID + SECOND_YOUTUBE_PIN.replace("@speedtwo", "@ishowspeed"),
            "duplicate pinned youtube handle: @ishowspeed",
        ),
        # excluded streamers
        ("exclude_streamers: [ninja,", "exclude_streamers: [NINJA,", r"exclude_streamers\.0"),
        (
            '"@SomeYouTuber"]',
            '"@SomeYouTuber", "@someyoutuber"]',
            "duplicate excluded streamer: @someyoutuber",
        ),
        (
            "exclude_streamers: [ninja,",
            "exclude_streamers: [jynxzi,",
            "jynxzi is both pinned in `streamers` and in `exclude_streamers`",
        ),
        ('"@SomeYouTuber"]', '"@ishowspeed"]', "@ishowspeed is both pinned"),
        # settings
        (
            "candidates_per_day: 12",
            "candidates_per_day: 2",
            "stories_per_day 3 is above candidates_per_day 2",
        ),
        ("min_clip_views: 1000", "min_clip_views: -1", r"defaults\.min_clip_views"),
        ("max_streamers_scanned: 50", "max_streamers_scanned: 0", r"max_streamers_scanned"),
        ("pinned_boost: 0.5", "pinned_boost: -1", r"defaults\.pinned_boost"),
        ("excluded_game_ids: []", 'excluded_game_ids: ["slots"]', "'slots' is not a Twitch game"),
        ("music_dir: null", "music_dir: music", "music_dir must be an absolute path"),
        ("voice: am_michael", "voice: Michael", r"defaults\.voice"),
        ("prompt: commentary_gaming_v1", "prompt: ../secrets", r"channels\.0\.prompt"),
    ],
)
def test_invalid_commentary_yaml_is_refused(
    tmp_path: Path, accounts: AccountsConfig, old: str, new: str, message: str
) -> None:
    assert old in COMMENTARY_YAML

    with pytest.raises(ConfigError, match=message):
        load_commentary(_write(tmp_path, COMMENTARY_YAML.replace(old, new, 1)), accounts)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ("      speed: 2\n", r"channels\.0\.overrides\.speed: Extra inputs are not permitted"),
        (
            "      candidates_per_day: 2\n",
            "channel 'random_gaming_yt' overrides are invalid: .*stories_per_day 3 is above",
        ),
        ("      run_at: 7:30\n", "quote the time"),
        ("      max_streamers_scanned: 0\n", r"overrides\.max_streamers_scanned"),
    ],
)
def test_invalid_overrides_are_refused(
    tmp_path: Path, accounts: AccountsConfig, overrides: str, message: str
) -> None:
    text = COMMENTARY_YAML + "    overrides:\n" + overrides

    with pytest.raises(ConfigError, match=message):
        load_commentary(_write(tmp_path, text), accounts)


def test_one_channel_per_account(tmp_path: Path, accounts: AccountsConfig) -> None:
    channel = COMMENTARY_YAML.split("channels:\n", 1)[1]

    with pytest.raises(ConfigError, match="account 'random_gaming_yt' is used by two channels"):
        load_commentary(_write(tmp_path, COMMENTARY_YAML + channel), accounts)


@pytest.mark.parametrize(
    ("account", "message"),
    [
        ("nobody_yt", "channel 'nobody_yt': no account 'nobody_yt' in accounts.yaml"),
        ("mrbeast_clips_yt", "account 'mrbeast_clips_yt' is not `content: commentary`"),
    ],
)
def test_channel_account_must_be_a_commentary_account(
    tmp_path: Path, accounts: AccountsConfig, account: str, message: str
) -> None:
    text = COMMENTARY_YAML.replace("account: random_gaming_yt", f"account: {account}")

    with pytest.raises(ConfigError, match=message):
        load_commentary(_write(tmp_path, text), accounts)


def test_yaml_problems_are_config_errors(tmp_path: Path, accounts: AccountsConfig) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_commentary(_write(tmp_path, "channels: [\n"), accounts)
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_commentary(_write(tmp_path, "- 1\n"), accounts)
    with pytest.raises(ConfigError, match="duplicate key"):
        load_commentary(_write(tmp_path, "channels: []\nchannels: []\n"), accounts)


def test_commentary_path_defaults_next_to_creators_config(tmp_path: Path) -> None:
    creators = tmp_path / "conf" / "creators.yaml"

    assert commentary_path(creators, env={}) == tmp_path / "conf" / "commentary.yaml"
    assert commentary_path(creators, env={COMMENTARY_ENV: "/x/c.yaml"}) == Path("/x/c.yaml")


def test_check_config_summarises_commentary_channels(tmp_path: Path) -> None:
    creators = _config_dir(tmp_path)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert "ok: 1 commentary channels" in result.output
    assert (
        "  - random_gaming_yt twitch_clips, prompt commentary_gaming_v1;"
        " games Grand Theft Auto VI; 3 pinned (2 twitch, 1 youtube), 2 excluded;"
        " daily at 06:00 America/New_York, 12 candidates, 3 stories, voice am_michael"
    ) in result.output
    assert (
        "note: channel random_gaming_yt: YouTube pinned streamers are not scanned until"
        " CLP-152 ships: IShowSpeed"
    ) in result.output


def test_check_config_has_no_note_without_youtube_pins(tmp_path: Path) -> None:
    head = COMMENTARY_YAML.split("      - name: IShowSpeed\n", 1)[0]
    creators = _config_dir(tmp_path, head)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert "CLP-152" not in result.output


def test_check_config_rejects_an_unknown_commentary_setting(tmp_path: Path) -> None:
    creators = _config_dir(tmp_path, COMMENTARY_YAML.replace("  voice:", "  speed: 2\n  voice:"))

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 1
    assert "invalid commentary config" in result.stderr
    assert "defaults.speed: Extra inputs are not permitted" in result.stderr


def test_check_config_rejects_a_streamer_both_pinned_and_excluded(tmp_path: Path) -> None:
    text = COMMENTARY_YAML.replace("exclude_streamers: [ninja,", "exclude_streamers: [kaicenat,")
    creators = _config_dir(tmp_path, text)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 1
    assert "kaicenat is both pinned in `streamers` and in `exclude_streamers`" in result.stderr


def test_check_config_warns_about_a_commentary_account_without_a_channel(
    tmp_path: Path,
) -> None:
    creators = _config_dir(tmp_path, commentary=None)

    result = runner.invoke(app, ["--config", str(creators), "check-config"])

    assert result.exit_code == 0, result.output
    assert "ok: 0 commentary channels" in result.output
    assert "commentary.yaml not found" in result.output
    assert (
        "warning: commentary account 'random_gaming_yt' has no channel in commentary.yaml"
        in result.stderr
    )
