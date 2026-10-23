"""`clipper resolve` (CLP-22): handles -> stable IDs, written back into creators.yaml."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipper import cli
from clipper.cli import app
from clipper.config import load_config
from clipper.resolve import Resolved, resolve_twitch, resolve_youtube, write_ids
from clipper.sources import twitch
from clipper.sources.youtube import HandleNotFound, QuotaMeter, SourceError

runner = CliRunner()
SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "creators.yaml"
IDS = {
    "@MrBeast": "UCX6OQ3DkcsbYNE6H8uQQuVA",
    "@IShowSpeed": "UCWsDFcIhY2DBi3GB5uykGXA",
    "@KaiCenat": "UCvbUo8pClsgSoiV4XS4fu1g",
    "@RaffyTulfoInAction": "UCxhygwqQ1ZMoBGQM2yEcNug",
    "@IvanaAlawi": "UCdl9Z9H4FHWp1YJKcrsxxtQ",
    "@CongTV": "UCvP1t1zMbQ4AOEe3zj1GNjA",
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


@pytest.fixture
def shipped(tmp_path: Path) -> Path:
    path = tmp_path / "creators.yaml"
    path.write_text(SHIPPED_CONFIG.read_text())
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
      channel_id: UCvbUo8pClsgSoiV4XS4fu1g
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
    # Only the inserted lines differ from the hand-written file.
    added = set(after_first.decode().splitlines()) - set(SHIPPED_CONFIG.read_text().splitlines())
    assert added == {f"      channel_id: {cid}" for cid in IDS.values()}


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
    del provider.ids["@CongTV"]

    result = runner.invoke(app, ["--config", str(shipped), "resolve"])

    assert result.exit_code == 1
    assert "Cong TV: youtube @CongTV: error: no YouTube channel has the handle @CongTV" in (
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
