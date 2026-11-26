"""`clipper resolve` (CLP-22): handles -> stable IDs, written back into creators.yaml."""

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipper import cli
from clipper.accounts import load_accounts
from clipper.cli import app
from clipper.commentary.config import CommentaryConfig, load_commentary
from clipper.config import load_config
from clipper.resolve import (
    CommentaryResolved,
    Resolved,
    resolve_commentary,
    resolve_twitch,
    resolve_youtube,
    write_commentary_ids,
    write_ids,
)
from clipper.sources import twitch
from clipper.sources.youtube import HandleNotFound, QuotaMeter, SourceError

runner = CliRunner()
SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "creators.yaml"
IDS = {
    "@MrBeast": "UCX6OQ3DkcsbYNE6H8uQQuVA",
    "@IShowSpeed": "UCWsDFcIhY2DBi3GB5uykGXA",
    "@KaiCenat": "UCoEmptob-eEGKk18c2VplJg",
    "@RaffyTulfoInAction": "UCxhygwqQ1ZMoBGQM2yEcNug",
    "@IvanaAlawi": "UCypeLkg3Ne5-LIyiB41b1GQ",
    "@CongTheVlogger": "UC73zqrs0Th_a9dFUivEmv2A",
}
OTHER = "UC0000000000000000000000"

SMALL_YAML = """\
# Header comment.
settings:
  first_run_mode: baseline        # keep me
creators:
  - name: MrBeast
    region: global
    tags: [challenge, entertainment]
    youtube:
      handle: "@MrBeast"

  # --- Section comment ---
  - name: Kai Cenat
    region: global
    twitch:
      login: kaicenat
    youtube:
      handle: "@KaiCenat"   # trailing comment
"""


class FakeProvider:
    def __init__(self, ids: dict[str, str | Exception]) -> None:
        self.ids = ids
        self.quota = QuotaMeter()
        self.asked: list[str] = []

    def channel_id(self, handle: str) -> str:
        self.asked.append(handle)
        answer = self.ids.get(handle, HandleNotFound(f"no YouTube channel has the handle {handle}"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def videos(self, ids, should_stop=None):  # pragma: no cover - not used here
        raise AssertionError

    def uploads(self, channel_id):  # pragma: no cover - not used here
        raise AssertionError


def unresolved_shipped() -> str:
    """The shipped config as written by hand: handles only, no resolved IDs."""
    return re.sub(r"(?m)^ +(channel_id|user_id): .*\n", "", SHIPPED_CONFIG.read_text())


@pytest.fixture
def shipped(tmp_path: Path) -> Path:
    path = tmp_path / "creators.yaml"
    path.write_text(unresolved_shipped())
    return path


@pytest.fixture
def small(tmp_path: Path) -> Path:
    path = tmp_path / "creators.yaml"
    path.write_text(SMALL_YAML)
    return path


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider(dict(IDS))
    monkeypatch.setattr(cli, "_youtube_provider", lambda secrets, http: fake)
    return fake


# --- resolve_youtube / write_ids ----------------------------------------------------------


def test_all_six_starting_creators_resolve(shipped: Path) -> None:
    cfg = load_config(shipped)

    results = resolve_youtube(cfg.creators, FakeProvider(dict(IDS)))

    assert [(r.creator, r.new, r.error) for r in results] == [
        (c.name, IDS[c.youtube.handle], None) for c in cfg.creators if c.youtube
    ]
    assert len(results) == 6
    assert all(r.changed for r in results)


def test_unknown_handle_is_an_error_and_others_still_resolve(small: Path) -> None:
    cfg = load_config(small)
    provider = FakeProvider({"@KaiCenat": IDS["@KaiCenat"]})

    results = resolve_youtube(cfg.creators, provider)

    assert results[0].error == "no YouTube channel has the handle @MrBeast"
    assert results[0].new is None and not results[0].changed
    assert results[1].new == IDS["@KaiCenat"] and results[1].error is None


def test_lookup_failure_is_reported_not_raised(small: Path) -> None:
    cfg = load_config(small)
    provider = FakeProvider({"@MrBeast": SourceError("timed out"), "@KaiCenat": OTHER})

    results = resolve_youtube(cfg.creators, provider)

    assert results[0].error == "timed out"
    assert results[1].new == OTHER


def test_write_inserts_ids_after_handle_and_keeps_comments(small: Path) -> None:
    results = [
        Resolved("MrBeast", "youtube", "@MrBeast", None, IDS["@MrBeast"]),
        Resolved("Kai Cenat", "youtube", "@KaiCenat", None, IDS["@KaiCenat"]),
        Resolved("Kai Cenat", "twitch", "kaicenat", None, "641972806"),
    ]

    assert write_ids(small, results) == 3

    assert (
        small.read_text()
        == """\
# Header comment.
settings:
  first_run_mode: baseline        # keep me
creators:
  - name: MrBeast
    region: global
    tags: [challenge, entertainment]
    youtube:
      handle: "@MrBeast"
      channel_id: UCX6OQ3DkcsbYNE6H8uQQuVA

  # --- Section comment ---
  - name: Kai Cenat
    region: global
    twitch:
      login: kaicenat
      user_id: "641972806"
    youtube:
      handle: "@KaiCenat"   # trailing comment
      channel_id: UCoEmptob-eEGKk18c2VplJg
"""
    )
    cfg = load_config(small)
    assert cfg.creator("Kai Cenat").twitch.user_id == "641972806"


def test_write_with_nothing_changed_leaves_file_byte_identical(small: Path) -> None:
    before = small.read_bytes()
    stat = small.stat()

    written = write_ids(
        small, [Resolved("MrBeast", "youtube", "@MrBeast", None, None, error="boom")]
    )

    assert written == 0
    assert small.read_bytes() == before
    assert small.stat().st_mtime_ns == stat.st_mtime_ns


def test_write_replaces_a_changed_id_in_place(small: Path) -> None:
    write_ids(small, [Resolved("MrBeast", "youtube", "@MrBeast", None, IDS["@MrBeast"])])

    write_ids(small, [Resolved("MrBeast", "youtube", "@MrBeast", IDS["@MrBeast"], OTHER)])

    text = small.read_text()
    assert f"channel_id: {OTHER}\n\n  # --- Section comment ---" in text
    assert IDS["@MrBeast"] not in text


def test_write_refuses_a_result_that_breaks_the_config(small: Path) -> None:
    before = small.read_bytes()
    # Two creators may not share a channel id; the file must stay as it was.
    results = [
        Resolved("MrBeast", "youtube", "@MrBeast", None, OTHER),
        Resolved("Kai Cenat", "youtube", "@KaiCenat", None, OTHER),
    ]

    with pytest.raises(ValueError, match="duplicate youtube channel_id"):
        write_ids(small, results)
    assert small.read_bytes() == before
    assert list(small.parent.iterdir()) == [small]


def test_write_never_follows_a_planted_temp_symlink(small: Path, tmp_path: Path) -> None:
    victim = tmp_path / "elsewhere" / "victim.txt"
    victim.parent.mkdir()
    victim.write_text("untouched")
    for name in (f".{small.name}.resolve.tmp", f".{small.name}.tmp"):
        (small.parent / name).symlink_to(victim)

    write_ids(small, [Resolved("MrBeast", "youtube", "@MrBeast", None, IDS["@MrBeast"])])

    assert victim.read_text() == "untouched"
    assert not small.is_symlink()
    assert load_config(small).creator("MrBeast").youtube.channel_id == IDS["@MrBeast"]
    leftovers = {p.name for p in small.parent.iterdir()} - {small.name, "elsewhere"}
    assert leftovers == {f".{small.name}.resolve.tmp", f".{small.name}.tmp"}  # ours are gone


def test_temp_file_is_written_through_its_exclusive_handle(
    small: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = Path.open
    real_write_text = Path.write_text

    def no_reopen_for_writing(self: Path, mode: str = "r", *args, **kwargs):
        if self.name.endswith(".tmp") and any(c in mode for c in "wax+"):
            raise AssertionError(f"temp file reopened by path for writing: {self.name}")
        return real_open(self, mode, *args, **kwargs)

    def no_write_text(self: Path, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise AssertionError(f"temp file rewritten by path: {self.name}")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_reopen_for_writing)
    monkeypatch.setattr(Path, "write_text", no_write_text)

    write_ids(small, [Resolved("MrBeast", "youtube", "@MrBeast", None, IDS["@MrBeast"])])

    assert load_config(small).creator("MrBeast").youtube.channel_id == IDS["@MrBeast"]
    assert list(small.parent.iterdir()) == [small]


# --- Twitch -------------------------------------------------------------------------------


@pytest.fixture
def helix() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(twitch.TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok123", "expires_in": 9})
        )
        yield mock


def test_twitch_logins_resolve_with_an_app_token(small: Path, helix: respx.MockRouter) -> None:
    users = helix.get(twitch.USERS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "641972806", "login": "kaicenat"}]})
    )
    cfg = load_config(small)

    with httpx.Client() as http:
        results = resolve_twitch(cfg.creators, http, "cid", "csecret")

    assert results == [Resolved("Kai Cenat", "twitch", "kaicenat", None, "641972806")]
    request = users.calls[0].request
    assert request.headers["Client-Id"] == "cid"
    assert request.headers["Authorization"] == "Bearer tok123"
    assert request.url.params.get_list("login") == ["kaicenat"]
    token_body = helix.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in token_body


def test_unknown_twitch_login_is_an_error(small: Path, helix: respx.MockRouter) -> None:
    helix.get(twitch.USERS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    cfg = load_config(small)

    with httpx.Client() as http:
        results = resolve_twitch(cfg.creators, http, "cid", "csecret")

    assert results[0].error == "no Twitch user has the login kaicenat"


def test_twitch_token_failure_never_shows_the_secret(small: Path) -> None:
    cfg = load_config(small)
    with respx.mock:
        respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(403, json={"message": "no"}))
        with httpx.Client() as http:
            results = resolve_twitch(cfg.creators, http, "cid", "csecret")

    assert "HTTP 403" in results[0].error
    assert "csecret" not in results[0].error


# --- CLI ----------------------------------------------------------------------------------


def test_cli_dry_run_reports_and_writes_nothing(shipped: Path, provider: FakeProvider) -> None:
    before = shipped.read_bytes()

    result = runner.invoke(app, ["--config", str(shipped), "resolve", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "MrBeast: youtube @MrBeast -> UCX6OQ3DkcsbYNE6H8uQQuVA (new)" in result.output
    assert "dry run: 6 id(s) would change" in result.output
    assert "twitch: skipped 1 creator(s)" in result.output
    assert shipped.read_bytes() == before


def test_cli_resolve_writes_then_rerun_is_a_byte_identical_no_op(
    shipped: Path, provider: FakeProvider
) -> None:
    first = runner.invoke(app, ["--config", str(shipped), "resolve"])
    after_first = shipped.read_bytes()
    second = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert first.exit_code == 0, first.output
    assert "wrote 6 id(s)" in first.output
    cfg = load_config(shipped)
    assert {c.youtube.handle: c.youtube.channel_id for c in cfg.creators} == IDS
    assert second.exit_code == 0, second.output
    assert "(unchanged)" in second.output and "nothing to write" in second.output
    assert shipped.read_bytes() == after_first
    # Only the inserted lines differ from the hand-written file ...
    added = set(after_first.decode().splitlines()) - set(unresolved_shipped().splitlines())
    assert added == {f"      channel_id: {cid}" for cid in IDS.values()}
    # ... and the result is exactly the committed config (resolved for real).
    assert after_first == SHIPPED_CONFIG.read_bytes()


def test_cli_rerun_after_handle_change_updates_the_id(
    shipped: Path, provider: FakeProvider
) -> None:
    runner.invoke(app, ["--config", str(shipped), "resolve"])
    provider.ids["@MrBeast"] = OTHER

    result = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert result.exit_code == 0, result.output
    assert f"-> {OTHER} (was UCX6OQ3DkcsbYNE6H8uQQuVA)" in result.output
    assert load_config(shipped).creator("MrBeast").youtube.channel_id == OTHER


def test_cli_unknown_handle_fails_at_the_end_but_others_are_written(
    shipped: Path, provider: FakeProvider
) -> None:
    del provider.ids["@CongTheVlogger"]

    result = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert result.exit_code == 1
    handle = "@CongTheVlogger"
    assert f"Cong TV: youtube {handle}: error: no YouTube channel has the handle {handle}" in (
        result.output
    )
    assert "wrote 5 id(s)" in result.output
    cfg = load_config(shipped)
    assert cfg.creator("Cong TV").youtube.channel_id is None
    assert cfg.creator("MrBeast").youtube.channel_id == IDS["@MrBeast"]


def test_cli_resolves_twitch_when_credentials_are_set(
    shipped: Path, provider: FakeProvider, helix: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    helix.get(twitch.USERS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "641972806", "login": "kaicenat"}]})
    )

    result = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert result.exit_code == 0, result.output
    assert "Kai Cenat: twitch kaicenat -> 641972806 (new)" in result.output
    assert load_config(shipped).creator("Kai Cenat").twitch.user_id == "641972806"


def test_cli_invalid_config_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "missing.yaml"), "resolve"])

    assert result.exit_code == 1
    assert "config file not found" in result.stderr


def test_cli_missing_cookies_file_fails_cleanly(
    shipped: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPPER_YTDLP_COOKIES", str(tmp_path / "missing.txt"))

    result = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert result.exit_code == 1
    assert "CLIPPER_YTDLP_COOKIES points to a missing file" in result.stderr


@pytest.mark.parametrize(
    ("token", "users", "message"),
    [
        (httpx.ConnectError("down"), None, "token request failed (ConnectError)"),
        (httpx.Response(200, text="not json"), None, "unexpected response"),
        (httpx.Response(200, json={"token_type": "bearer"}), None, "no access_token"),
        (None, httpx.ReadTimeout("slow"), "users request failed (ReadTimeout)"),
        (None, httpx.Response(401, json={}), "users request failed: HTTP 401"),
    ],
)
def test_twitch_failures_are_reported_per_creator(
    small: Path, token: object, users: object, message: str
) -> None:
    cfg = load_config(small)
    with respx.mock:
        token_route = respx.post(twitch.TOKEN_URL)
        if isinstance(token, Exception):
            token_route.mock(side_effect=token)
        else:
            token_route.mock(return_value=token or httpx.Response(200, json={"access_token": "t"}))
        users_route = respx.get(twitch.USERS_URL)
        if isinstance(users, Exception):
            users_route.mock(side_effect=users)
        else:
            users_route.mock(return_value=users or httpx.Response(200, json={"data": []}))
        with httpx.Client() as http:
            results = resolve_twitch(cfg.creators, http, "cid", "csecret")

    assert message in results[0].error


def test_twitch_ignores_malformed_user_rows(small: Path, helix: respx.MockRouter) -> None:
    helix.get(twitch.USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": ["x", {"login": "kaicenat", "id": "１２"}, {"login": "kaicenat"}]},
        )
    )
    cfg = load_config(small)

    with httpx.Client() as http:
        results = resolve_twitch(cfg.creators, http, "cid", "csecret")

    assert results[0].error == "no Twitch user has the login kaicenat"


# --- Commentary channels: game ids and pinned Twitch user ids (CLP-144) -------------------

COMMENTARY_CREATORS = """\
creators:
  - name: MrBeast
    region: global
    youtube:
      handle: "@MrBeast"
"""

COMMENTARY_ACCOUNTS = """\
accounts:
  - id: random_gaming_yt
    platform: youtube
    content: commentary
    channel_id: UCcMYw3Ae2crqXE2yzFBSUig
    handle: "@RandomGaming-f3w"
    market: US
    timezone: America/New_York
"""

COMMENTARY = """\
# Header comment.
defaults:
  run_at: "06:00"
channels:
  - account: random_gaming_yt   # Random Gaming
    source: twitch_clips
    prompt: commentary_gaming_v1
    games:
      - name: Grand Theft Auto VI
        aliases: [GTA 6, GTA VI]   # keep me
      - name: Fortnite
    # Pinned streamers.
    streamers:
      - name: Kai Cenat
        twitch: kaicenat
      - name: IShowSpeed
        youtube:
          handle: "@IShowSpeed"
    exclude_streamers: []
"""

GTA_ID, FORTNITE_ID, KAI_ID = "1867279146", "33214", "641972806"
COMMENTARY_SECRET = "SENTINEL-commentary-secret"


@pytest.fixture
def commentary_dir(tmp_path: Path) -> Path:
    """creators.yaml, accounts.yaml and commentary.yaml; returns creators.yaml."""
    (tmp_path / "accounts.yaml").write_text(COMMENTARY_ACCOUNTS)
    (tmp_path / "commentary.yaml").write_text(COMMENTARY)
    creators = tmp_path / "creators.yaml"
    creators.write_text(COMMENTARY_CREATORS)
    return creators


def _commentary(creators: Path) -> CommentaryConfig:
    folder = creators.parent
    accounts = load_accounts(folder / "accounts.yaml", load_config(creators))
    return load_commentary(folder / "commentary.yaml", accounts)


def _validate_commentary(tmp: Path) -> object:
    creators = tmp.parent / "creators.yaml"
    accounts = load_accounts(tmp.parent / "accounts.yaml", load_config(creators))
    return load_commentary(tmp, accounts)


@pytest.fixture
def games_and_users(helix: respx.MockRouter) -> respx.MockRouter:
    helix.get(twitch.GAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": GTA_ID, "name": "Grand Theft Auto VI"},
                    {"id": FORTNITE_ID, "name": "Fortnite"},
                ]
            },
        )
    )
    helix.get(twitch.USERS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": KAI_ID, "login": "kaicenat"}]})
    )
    return helix


def test_commentary_games_and_twitch_pins_resolve(
    commentary_dir: Path, games_and_users: respx.MockRouter
) -> None:
    with httpx.Client() as http:
        results = resolve_commentary(_commentary(commentary_dir), http, "cid", "csecret")

    assert results == [
        CommentaryResolved("random_gaming_yt", "game", "Grand Theft Auto VI", None, GTA_ID),
        CommentaryResolved("random_gaming_yt", "game", "Fortnite", None, FORTNITE_ID),
        CommentaryResolved("random_gaming_yt", "twitch", "kaicenat", None, KAI_ID),
    ]
    games_call = games_and_users.routes[1].calls[0].request
    assert games_call.url.params.get_list("name") == ["Grand Theft Auto VI", "Fortnite"]
    assert games_call.headers["Authorization"] == "Bearer tok123"


def test_unknown_game_and_login_are_errors(commentary_dir: Path, helix: respx.MockRouter) -> None:
    helix.get(twitch.GAMES_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    helix.get(twitch.USERS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    with httpx.Client() as http:
        results = resolve_commentary(_commentary(commentary_dir), http, "cid", "csecret")

    assert [r.error for r in results] == [
        "no Twitch game is named Grand Theft Auto VI",
        "no Twitch game is named Fortnite",
        "no Twitch user has the login kaicenat",
    ]
    assert not any(r.changed for r in results)


def test_commentary_twitch_failure_is_reported_per_entry_without_the_secret(
    commentary_dir: Path,
) -> None:
    with respx.mock:
        respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(403, text=COMMENTARY_SECRET))
        with httpx.Client() as http:
            results = resolve_commentary(
                _commentary(commentary_dir), http, "cid", COMMENTARY_SECRET
            )

    assert len(results) == 3
    for result in results:
        assert result.error is not None
        assert "HTTP 403" in result.error
        assert COMMENTARY_SECRET not in result.error


def test_commentary_with_nothing_on_twitch_makes_no_request() -> None:
    with respx.mock, httpx.Client() as http:  # any request would fail: no route
        assert resolve_commentary(CommentaryConfig(), http, "cid", "csecret") == []


def test_commentary_ids_are_written_after_their_key_keeping_comments(
    commentary_dir: Path, games_and_users: respx.MockRouter
) -> None:
    path = commentary_dir.parent / "commentary.yaml"
    with httpx.Client() as http:
        results = resolve_commentary(_commentary(commentary_dir), http, "cid", "csecret")

    written = write_commentary_ids(path, results, _validate_commentary)

    assert written == 3
    text = path.read_text()
    assert (
        "      - name: Grand Theft Auto VI\n"
        "        aliases: [GTA 6, GTA VI]   # keep me\n"
        f'        game_id: "{GTA_ID}"\n'
        "      - name: Fortnite\n"
        f'        game_id: "{FORTNITE_ID}"\n'
        "    # Pinned streamers.\n"
    ) in text
    assert f'        twitch: kaicenat\n        user_id: "{KAI_ID}"\n' in text
    assert text.startswith("# Header comment.\n")
    assert "  - account: random_gaming_yt   # Random Gaming\n" in text
    cfg = _commentary(commentary_dir)
    assert [g.game_id for g in cfg.channels[0].games] == [GTA_ID, FORTNITE_ID]
    assert cfg.channels[0].streamers[0].user_id == KAI_ID


def test_commentary_write_with_nothing_changed_leaves_the_file_alone(
    commentary_dir: Path,
) -> None:
    path = commentary_dir.parent / "commentary.yaml"
    before = path.read_bytes()
    unchanged = [CommentaryResolved("random_gaming_yt", "game", "Fortnite", "1", "1")]

    assert write_commentary_ids(path, unchanged, _validate_commentary) == 0
    assert path.read_bytes() == before


def test_commentary_write_replaces_a_changed_id_in_place(commentary_dir: Path) -> None:
    path = commentary_dir.parent / "commentary.yaml"
    path.write_text(
        COMMENTARY.replace(
            "      - name: Fortnite\n", '      - name: Fortnite\n        game_id: "1"\n'
        )
    )
    change = [CommentaryResolved("random_gaming_yt", "game", "Fortnite", "1", FORTNITE_ID)]

    assert write_commentary_ids(path, change, _validate_commentary) == 1
    assert f'        game_id: "{FORTNITE_ID}"\n' in path.read_text()
    assert 'game_id: "1"\n' not in path.read_text()


def test_commentary_write_refuses_an_invalid_result(commentary_dir: Path) -> None:
    path = commentary_dir.parent / "commentary.yaml"
    before = path.read_bytes()
    bad = [CommentaryResolved("random_gaming_yt", "game", "Fortnite", None, "not-an-id")]

    with pytest.raises(ValueError, match="would be invalid"):
        write_commentary_ids(path, bad, _validate_commentary)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "accounts.yaml",
        "commentary.yaml",
        "creators.yaml",
    ]


def test_cli_resolves_commentary_ids_when_credentials_are_set(
    commentary_dir: Path,
    provider: FakeProvider,
    games_and_users: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")

    first = runner.invoke(app, ["--config", str(commentary_dir), "resolve"])
    second = runner.invoke(app, ["--config", str(commentary_dir), "resolve"])

    assert first.exit_code == 0, first.output
    assert f"random_gaming_yt: game Grand Theft Auto VI -> {GTA_ID} (new)" in first.output
    assert f"random_gaming_yt: twitch kaicenat -> {KAI_ID} (new)" in first.output
    commentary = commentary_dir.parent / "commentary.yaml"
    assert f"wrote 3 id(s) to {commentary}" in first.output
    assert _commentary(commentary_dir).channels[0].streamers[0].user_id == KAI_ID
    assert second.exit_code == 0, second.output
    assert f"random_gaming_yt: twitch kaicenat -> {KAI_ID} (unchanged)" in second.output
    assert f"{commentary}: nothing to write" in second.output


def test_cli_commentary_dry_run_writes_nothing(
    commentary_dir: Path,
    provider: FakeProvider,
    games_and_users: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    before = (commentary_dir.parent / "commentary.yaml").read_bytes()

    result = runner.invoke(app, ["--config", str(commentary_dir), "resolve", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry run: 3 commentary id(s) would change" in result.output
    assert (commentary_dir.parent / "commentary.yaml").read_bytes() == before


def test_cli_commentary_without_credentials_is_skipped_with_a_note(
    commentary_dir: Path, provider: FakeProvider
) -> None:
    result = runner.invoke(app, ["--config", str(commentary_dir), "resolve"])

    assert result.exit_code == 0, result.output
    assert (
        "commentary: skipped 2 game(s) and 1 pinned streamer(s)"
        " (set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET to resolve them)"
    ) in result.output


def test_cli_commentary_unknown_game_fails_at_the_end(
    commentary_dir: Path,
    provider: FakeProvider,
    helix: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    helix.get(twitch.GAMES_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": GTA_ID, "name": "Grand Theft Auto VI"}]}
        )
    )
    helix.get(twitch.USERS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": KAI_ID, "login": "kaicenat"}]})
    )

    result = runner.invoke(app, ["--config", str(commentary_dir), "resolve"])

    assert result.exit_code == 1
    assert (
        "random_gaming_yt: game Fortnite: error: no Twitch game is named Fortnite" in result.stderr
    )
    assert "wrote 2 id(s)" in result.output


@pytest.mark.parametrize("broken", ["commentary.yaml", "accounts.yaml"])
def test_a_broken_commentary_or_accounts_file_still_resolves_creators(
    commentary_dir: Path, provider: FakeProvider, broken: str
) -> None:
    (commentary_dir.parent / broken).write_text("channels: [\n")

    result = runner.invoke(app, ["--config", str(commentary_dir), "resolve"])

    assert result.exit_code == 1
    assert "commentary: skipped:" in result.stderr
    assert "invalid YAML" in result.stderr
    assert f"wrote 1 id(s) to {commentary_dir}" in result.output
    assert load_config(commentary_dir).creator("MrBeast").youtube.channel_id == IDS["@MrBeast"]


def test_cli_resolve_on_the_shipped_config_only_adds_user_ids_to_commentary_yaml(
    tmp_path: Path,
    provider: FakeProvider,
    helix: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipped_dir = SHIPPED_CONFIG.parent
    for name in ("creators.yaml", "accounts.yaml", "commentary.yaml"):
        (tmp_path / name).write_text((shipped_dir / name).read_text())
    original = (tmp_path / "commentary.yaml").read_text()
    logins = re.findall(r"(?m)^ +twitch: (\w+)$", original)
    ids = {login: str(1000 + n) for n, login in enumerate(logins)}

    def users(request: httpx.Request) -> httpx.Response:
        asked = request.url.params.get_list("login")
        return httpx.Response(
            200, json={"data": [{"login": x, "id": ids.get(x, "641972806")} for x in asked]}
        )

    helix.get(twitch.USERS_URL).mock(side_effect=users)
    helix.get(twitch.GAMES_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": GTA_ID, "name": "Grand Theft Auto VI"}]}
        )
    )
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")

    result = runner.invoke(app, ["--config", str(tmp_path / "creators.yaml"), "resolve"])

    assert result.exit_code == 0, result.output
    assert f"random_gaming_yt: game Grand Theft Auto VI -> {GTA_ID} (unchanged)" in result.output
    after = (tmp_path / "commentary.yaml").read_text()
    added = [line for line in after.splitlines() if line not in original.splitlines()]
    assert added == [f'        user_id: "{ids[login]}"' for login in logins]
    assert [line for line in after.splitlines() if "user_id" not in line] == original.splitlines()
    assert len(logins) == 10


def test_game_names_match_whatever_case_twitch_returns(
    commentary_dir: Path, helix: respx.MockRouter
) -> None:
    helix.get(twitch.GAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": GTA_ID, "name": "Grand Theft Auto vi"},
                    {"id": FORTNITE_ID, "name": "FORTNITE"},
                ]
            },
        )
    )
    helix.get(twitch.USERS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    with httpx.Client() as http:
        results = resolve_commentary(_commentary(commentary_dir), http, "cid", "csecret")

    assert [(r.key, r.new) for r in results[:2]] == [
        ("Grand Theft Auto VI", GTA_ID),
        ("Fortnite", FORTNITE_ID),
    ]
