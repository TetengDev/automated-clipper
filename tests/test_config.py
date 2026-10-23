import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clipper.config import (
    ConfigError,
    CreatorOverrides,
    MissingSecretError,
    Secrets,
    Settings,
    load_config,
)

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "creators.yaml"


def test_shipped_config_is_valid() -> None:
    cfg = load_config(SHIPPED_CONFIG)

    assert len(cfg.creators) == 6
    assert cfg.settings.first_run_mode == "baseline"
    assert cfg.creator("Kai Cenat").twitch is not None


def test_load_valid_config(config_file: Path) -> None:
    cfg = load_config(config_file)

    assert [c.name for c in cfg.creators] == ["MrBeast", "Kai Cenat", "Raffy Tulfo in Action"]
    assert cfg.settings.min_duration_seconds == 300
    kai = cfg.creator("Kai Cenat")
    assert kai.twitch is not None and kai.twitch.login == "kaicenat"
    assert kai.youtube is not None and kai.youtube.channel_id is None


def test_per_creator_override_wins(config_file: Path) -> None:
    cfg = load_config(config_file)

    assert cfg.effective_settings("Raffy Tulfo in Action").min_duration_seconds == 600
    assert cfg.effective_settings("MrBeast").min_duration_seconds == 300
    # untouched fields fall through to globals
    assert cfg.effective_settings("Raffy Tulfo in Action").max_clips_per_source == 3


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("creators: []\nsettings: {min_duration_seconds: -5}\n", "settings.min_duration_seconds"),
        ("creators:\n  - name: X\n    region: global\n", "at least one platform"),
        ("creators:\n  - {name: X, region: mars, youtube: {handle: '@xxx'}}\n", "region"),
        ("creators:\n  - {name: X, region: global, youtube: {handle: 'nohandle'}}\n", "handle"),
        ("creators: []\nsettings: {bogus_key: 1}\n", "bogus_key"),
        (
            "creators:\n  - {name: A, region: global, youtube: {handle: '@aaa'}}\n"
            "  - {name: a, region: global, youtube: {handle: '@bbb'}}\n",
            "duplicate creator name",
        ),
        ("creators: [\n", "invalid YAML"),
    ],
)
def test_invalid_config_reports_field(tmp_path: Path, yaml_text: str, expected: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml_text)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert expected in str(excinfo.value)


def test_unknown_creator_lookup(config_file: Path) -> None:
    cfg = load_config(config_file)
    with pytest.raises(KeyError):
        cfg.creator("Nobody")


def test_secrets_never_leak_in_repr_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123-supersecret")
    secrets = Secrets()

    caplog.set_level(logging.INFO)
    logging.getLogger("t").info("secrets=%s %r", secrets, secrets)

    assert "sk-test-123-supersecret" not in repr(secrets)
    assert "sk-test-123-supersecret" not in str(secrets)
    assert "sk-test-123-supersecret" not in caplog.text
    assert secrets.require("ANTHROPIC_API_KEY") == "sk-test-123-supersecret"


def test_missing_secret_only_fails_when_required() -> None:
    secrets = Secrets()  # nothing set; constructing must not fail

    with pytest.raises(MissingSecretError, match="YOUTUBE_API_KEY"):
        secrets.require("YOUTUBE_API_KEY")


def test_empty_secret_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")
    with pytest.raises(MissingSecretError):
        Secrets().require("GROQ_API_KEY")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return path


def test_invalid_override_caught_at_load_with_creator_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "creators:\n  - name: Slow\n    region: global\n    youtube: {handle: '@slow'}\n"
        "    overrides: {clip_min_seconds: 90}\n",
    )
    with pytest.raises(ConfigError, match="Slow"):
        load_config(path)


def test_creator_lookup_is_case_insensitive(config_file: Path) -> None:
    assert load_config(config_file).creator("mrbeast").name == "MrBeast"


@pytest.mark.parametrize(
    "creators",
    [
        "  - {name: A, region: global, youtube: {handle: '@Same'}}\n"
        "  - {name: B, region: global, youtube: {handle: '@same'}}\n",
        "  - {name: A, region: global, twitch: {login: same_login}}\n"
        "  - {name: B, region: global, twitch: {login: same_login}}\n",
    ],
)
def test_duplicate_platform_identity_rejected(tmp_path: Path, creators: str) -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(_write(tmp_path, "creators:\n" + creators))


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "creators: []\nsettings: {}\nsettings: {lookback_days: 1}\n")
    with pytest.raises(ConfigError, match="duplicate key"):
        load_config(path)


def test_non_utf8_file_is_config_error(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_bytes(b"creators: [\xff\xfe]\n")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("name", ["../etc", "a/b", "-rf", "x\ny"])
def test_unsafe_creator_names_rejected(tmp_path: Path, name: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump(
            {"creators": [{"name": name, "region": "global", "youtube": {"handle": "@abc"}}]}
        )
    )
    with pytest.raises(ConfigError, match="name"):
        load_config(path)


def test_paths_not_configurable_from_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="data_dir"):
        load_config(_write(tmp_path, "creators: []\nsettings: {data_dir: /etc}\n"))


def test_short_session_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(MissingSecretError, match="at least 32"):
        Secrets().require("SESSION_SECRET")


@pytest.mark.parametrize("remote", ["-x", "--config=/etc/passwd", "no colon", "a b:c"])
def test_rclone_remote_validated(monkeypatch: pytest.MonkeyPatch, remote: str) -> None:
    monkeypatch.setenv("RCLONE_REMOTE", remote)
    with pytest.raises(ValidationError):
        Secrets()


def test_rclone_remote_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCLONE_REMOTE", "gdrive:clips/ready")
    assert Secrets().rclone_remote == "gdrive:clips/ready"


def test_reframe_setting_defaults_to_auto_with_creator_opt_out() -> None:
    from clipper.config import AppConfig, Creator, CreatorOverrides, Settings, YouTubeChannel

    creator = Creator(
        name="Static",
        region="global",
        youtube=YouTubeChannel(handle="@static"),
        overrides=CreatorOverrides(reframe="blurred_fill"),
    )
    cfg = AppConfig(creators=(creator,))
    assert cfg.settings.reframe == "auto"
    assert cfg.effective_settings("Static").reframe == "blurred_fill"
    with pytest.raises(ValidationError):
        Settings(reframe="split_screen")  # type: ignore[arg-type]


def test_jump_cuts_defaults_to_silences_with_creator_override() -> None:
    from clipper.config import AppConfig, Creator, CreatorOverrides, Settings, YouTubeChannel

    creator = Creator(
        name="Raw",
        region="ph",
        youtube=YouTubeChannel(handle="@raw"),
        overrides=CreatorOverrides(jump_cuts="full"),
    )
    cfg = AppConfig(creators=(creator,))
    assert cfg.settings.jump_cuts == "silences"
    assert cfg.effective_settings("Raw").jump_cuts == "full"
    with pytest.raises(ValidationError):
        Settings(jump_cuts="everything")  # type: ignore[arg-type]


def test_jump_cuts_off_survives_yaml(tmp_path: Path) -> None:
    """YAML 1.1 reads a bare `off` as false; it still means "off"."""
    from clipper.config import Settings

    path = tmp_path / "creators.yaml"
    path.write_text(
        "settings:\n  jump_cuts: off\ncreators:\n"
        "  - name: Raw\n    region: ph\n    youtube: {handle: '@raw'}\n"
        "    overrides: {jump_cuts: off}\n"
    )
    cfg = load_config(path)
    assert cfg.settings.jump_cuts == "off"
    assert cfg.effective_settings("Raw").jump_cuts == "off"
    with pytest.raises(ValidationError):
        Settings(jump_cuts=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- transcriber (CLP-35)


def test_transcriber_defaults_to_local_whisper() -> None:
    from clipper.config import Settings

    s = Settings()
    assert (s.transcriber, s.whisper_model, s.whisper_compute_type) == (
        "local",
        "large-v3-turbo",
        "int8",
    )


@pytest.mark.parametrize(
    "model",
    [
        "../models/x",
        "/etc/passwd",
        "Systran/faster-whisper-tiny",
        "https://evil.example/m",
        "a b",
        ".",
        "",
        "small.en",  # a real faster-whisper name, but not pinned
        "large",  # alias: only pinned names
    ],
)
def test_whisper_model_must_be_a_pinned_name(model: str) -> None:
    from clipper.config import CreatorOverrides, Settings

    with pytest.raises(ValidationError, match="whisper_model must be one of"):
        Settings(whisper_model=model)
    with pytest.raises(ValidationError, match="whisper_model must be one of"):
        CreatorOverrides(whisper_model=model)


@pytest.mark.parametrize("threads", [0, 65, -1])
def test_whisper_cpu_threads_bounded(threads: int) -> None:
    from clipper.config import Settings

    assert Settings().whisper_cpu_threads == 4
    with pytest.raises(ValidationError):
        Settings(whisper_cpu_threads=threads)


@pytest.mark.parametrize("field", ["transcriber", "whisper_compute_type"])
def test_transcriber_choices_are_closed(field: str) -> None:
    from clipper.config import Settings

    with pytest.raises(ValidationError):
        Settings(**{field: "bogus"})


def test_transcriber_per_creator_override(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "settings: {whisper_model: large-v3}\n"
        "creators:\n"
        "  - {name: A, region: global, youtube: {handle: '@aaa'},"
        " overrides: {transcriber: groq, whisper_model: tiny, whisper_compute_type: float32}}\n"
        "  - {name: B, region: global, youtube: {handle: '@bbb'}}\n"
    )
    cfg = load_config(path)
    a, b = cfg.effective_settings("A"), cfg.effective_settings("B")
    assert (a.transcriber, a.whisper_model, a.whisper_compute_type) == ("groq", "tiny", "float32")
    assert (b.transcriber, b.whisper_model, b.whisper_compute_type) == ("local", "large-v3", "int8")


def test_max_llm_windows_must_be_positive() -> None:
    assert Settings().max_llm_windows == 8
    with pytest.raises(ValidationError):
        Settings(max_llm_windows=0)
    with pytest.raises(ValidationError):
        CreatorOverrides(max_llm_windows=0)


# --- CLP-51: content_type per creator ------------------------------------------------------------


def test_shipped_creators_have_their_content_type() -> None:
    cfg = load_config(SHIPPED_CONFIG)
    assert {c.name: c.content_type for c in cfg.creators} == {
        "MrBeast": "challenge_stunt",
        "IShowSpeed": "streamer_reaction",
        "Kai Cenat": "streamer_reaction",
        "Raffy Tulfo in Action": "public_service",
        "Ivana Alawi": "vlog_lifestyle",
        "Cong TV": "comedy",
    }


@pytest.mark.parametrize(
    "content_type",
    ["challenge_stunt", "streamer_reaction", "public_service", "vlog_lifestyle", "comedy"],
)
def test_valid_content_types_accepted(tmp_path: Path, content_type: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "creators:\n  - {name: X, region: global, youtube: {handle: '@xxx'},"
        f" content_type: {content_type}}}\n"
    )
    assert load_config(path).creator("X").content_type == content_type


def test_content_type_is_optional(config_file: Path) -> None:
    assert load_config(config_file).creator("MrBeast").content_type is None


@pytest.mark.parametrize("bad", ["horror", "Comedy", "''", "[comedy]"])
def test_invalid_content_type_rejected(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "creators:\n  - {name: X, region: global, youtube: {handle: '@xxx'},"
        f" content_type: {bad}}}\n"
    )
    with pytest.raises(ConfigError, match="content_type"):
        load_config(path)
