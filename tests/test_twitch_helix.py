"""Twitch Helix client for commentary discovery (CLP-144), against httpx MockTransport."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from clipper.sources import twitch
from clipper.sources.twitch import Helix, TwitchAuthError, TwitchError, TwitchPermanentError

SECRET = "SENTINEL-client-secret-7f3a"
TOKEN = "SENTINEL-access-token-91bc"
NOW = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)

Handler = Callable[[httpx.Request], httpx.Response]


def clip_json(clip_id: str, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "broadcaster_id": "100",
        "broadcaster_name": "KaiCenat",
        "game_id": "1867279146",
        "language": "en",
        "title": f"clip {clip_id}",
        "view_count": 12000,
        "created_at": "2026-11-26T09:00:00Z",
        "duration": 30.5,
    }
    row.update(extra)
    return row


class Recorder:
    """A MockTransport handler: token requests answer with TOKEN, Helix requests with
    `helix(request)`; every request is kept."""

    def __init__(self, helix: Handler, tokens: list[str] | None = None) -> None:
        self.helix = helix
        self.tokens = tokens or [TOKEN]
        self.requests: list[httpx.Request] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url == twitch.TOKEN_URL:
            token = self.tokens[min(self.token_requests, len(self.tokens) - 1)]
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        return self.helix(request)

    def helix_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url != twitch.TOKEN_URL]


def client(handler: Callable[[httpx.Request], httpx.Response]) -> Helix:
    return Helix(httpx.Client(transport=httpx.MockTransport(handler)), "cid", SECRET)


# --- Get Clips ----------------------------------------------------------------------------


def test_clips_by_game_follow_the_cursor_up_to_five_pages() -> None:
    def helix(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("after", "0"))
        return httpx.Response(
            200,
            json={"data": [clip_json(f"c{page}")], "pagination": {"cursor": str(page + 1)}},
        )

    rec = Recorder(helix)
    clips = client(rec).clips(game_id="1867279146", started_at=SINCE, ended_at=NOW)

    assert [c.id for c in clips] == ["c0", "c1", "c2", "c3", "c4"]
    calls = rec.helix_requests()
    assert len(calls) == 5
    first = calls[0]
    assert first.url.path == "/helix/clips"
    assert first.url.params["game_id"] == "1867279146"
    assert first.url.params["first"] == "100"
    assert first.url.params["started_at"] == "2026-11-25T11:00:00Z"
    assert first.url.params["ended_at"] == "2026-11-26T11:00:00Z"
    assert "after" not in first.url.params
    assert [r.url.params.get("after") for r in calls[1:]] == ["1", "2", "3", "4"]
    assert first.headers["Client-Id"] == "cid"
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("pagination", [{}, {"cursor": ""}, None])
def test_clips_stop_when_there_is_no_cursor(pagination: object) -> None:
    body: dict[str, object] = {"data": [clip_json("only")]}
    if pagination is not None:
        body["pagination"] = pagination
    rec = Recorder(lambda request: httpx.Response(200, json=body))

    clips = client(rec).clips(game_id="1", started_at=SINCE, ended_at=NOW)

    assert [c.id for c in clips] == ["only"]
    assert len(rec.helix_requests()) == 1


def test_clips_stop_on_an_empty_page_even_with_a_cursor() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"data": [], "pagination": {"cursor": "x"}}))

    assert client(rec).clips(game_id="1", started_at=SINCE, ended_at=NOW) == []
    assert len(rec.helix_requests()) == 1


def test_pinned_streamer_clips_ask_by_broadcaster_id_for_twenty() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"data": [clip_json("p1")]}))

    clips = client(rec).clips(
        broadcaster_id="641972806", started_at=SINCE, ended_at=NOW, first=20, max_pages=1
    )

    (request,) = rec.helix_requests()
    assert request.url.params["broadcaster_id"] == "641972806"
    assert request.url.params["first"] == "20"
    assert "game_id" not in request.url.params
    assert [c.id for c in clips] == ["p1"]


def test_clip_fields_are_parsed() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"data": [clip_json("abc")]}))

    (clip,) = client(rec).clips(game_id="1", started_at=SINCE, ended_at=NOW)

    assert clip == twitch.Clip(
        id="abc",
        url="https://clips.twitch.tv/abc",
        broadcaster_id="100",
        broadcaster_name="KaiCenat",
        game_id="1867279146",
        language="en",
        title="clip abc",
        view_count=12000,
        created_at=datetime(2026, 11, 26, 9, 0, tzinfo=UTC),
        duration_s=30.5,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        {"id": "x"},
        clip_json("x", view_count="12"),
        clip_json("x", view_count=True),
        clip_json("x", view_count=-1),
        clip_json("x", duration=0),
        clip_json("x", duration="30"),
        clip_json("x", created_at="yesterday"),
        clip_json("x", created_at="2026-11-26T09:00:00"),
        clip_json("x", broadcaster_id="12a"),
        clip_json("x", url=None),
    ],
)
def test_malformed_clip_rows_are_left_out(bad: object) -> None:
    body = {"data": [bad, clip_json("good")]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))

    clips = client(rec).clips(game_id="1", started_at=SINCE, ended_at=NOW)

    assert [c.id for c in clips] == ["good"]


# --- Get Top Games, Get Channel Information, Get Games ------------------------------------


def test_top_games_returns_the_ids_in_order() -> None:
    body = {"data": [{"id": "509658", "name": "Just Chatting"}, {"id": "1867279146"}, {"x": 1}]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))

    assert client(rec).top_games() == ["509658", "1867279146"]
    (request,) = rec.helix_requests()
    assert request.url.path == "/helix/games/top"
    assert request.url.params["first"] == "20"


def test_channels_return_login_game_and_classification_labels_in_batches_of_100() -> None:
    def helix(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get_list("broadcaster_id")
        data = [
            {
                "broadcaster_id": i,
                "broadcaster_login": f"user{i}",
                "broadcaster_name": f"User{i}",
                "game_id": "1867279146",
                "content_classification_labels": ["Gambling"] if i == "7" else [],
            }
            for i in ids
        ]
        return httpx.Response(200, json={"data": [*data, "junk", {"broadcaster_id": "x"}]})

    rec = Recorder(helix)
    ids = [str(n) for n in range(1, 151)]

    channels = client(rec).channels(ids)

    calls = rec.helix_requests()
    assert [len(r.url.params.get_list("broadcaster_id")) for r in calls] == [100, 50]
    assert calls[0].url.path == "/helix/channels"
    assert len(channels) == 150
    assert channels["7"] == twitch.ChannelInfo(
        broadcaster_id="7",
        login="user7",
        name="User7",
        game_id="1867279146",
        labels=frozenset({"Gambling"}),
    )
    assert channels["8"].labels == frozenset()


def test_channels_with_no_ids_make_no_request() -> None:
    rec = Recorder(lambda r: httpx.Response(500))

    assert client(rec).channels([]) == {}
    assert rec.requests == []


def test_game_ids_look_games_up_by_exact_name() -> None:
    body = {"data": [{"id": "1867279146", "name": "Grand Theft Auto VI"}]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))

    found = client(rec).game_ids(["Grand Theft Auto VI", "Nope"])

    assert found == {"Grand Theft Auto VI": "1867279146"}
    (request,) = rec.helix_requests()
    assert request.url.path == "/helix/games"
    assert request.url.params.get_list("name") == ["Grand Theft Auto VI", "Nope"]


def test_user_ids_go_through_the_same_token() -> None:
    body = {"data": [{"id": "641972806", "login": "kaicenat"}]}
    rec = Recorder(lambda r: httpx.Response(200, json=body))

    assert client(rec).user_ids(["kaicenat"]) == {"kaicenat": "641972806"}
    assert rec.token_requests == 1


# --- Tokens and errors --------------------------------------------------------------------


def test_one_app_token_serves_every_request() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"data": []}))
    helix = client(rec)

    helix.top_games()
    helix.clips(game_id="1", started_at=SINCE, ended_at=NOW)

    assert rec.token_requests == 1


def test_an_expired_token_is_refreshed_once_and_the_call_retried() -> None:
    def helix(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"message": "Invalid OAuth token"})
        return httpx.Response(200, json={"data": [{"id": "1"}]})

    rec = Recorder(helix, tokens=["old", "new"])

    assert client(rec).top_games() == ["1"]
    assert rec.token_requests == 2
    assert [r.headers["Authorization"] for r in rec.helix_requests()] == [
        "Bearer old",
        "Bearer new",
    ]


def test_a_second_401_after_the_refresh_is_an_auth_error() -> None:
    rec = Recorder(lambda r: httpx.Response(401, json={}))

    with pytest.raises(TwitchAuthError, match="top games request failed: HTTP 401"):
        client(rec).top_games()
    assert rec.token_requests == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_client_credentials_are_an_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "invalid client secret"})

    with pytest.raises(TwitchAuthError, match=f"token request failed: HTTP {status}"):
        client(handler).top_games()


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx.Response(500), "clips request failed: HTTP 500"),
        (httpx.Response(503), "clips request failed: HTTP 503"),
        (httpx.Response(429), "clips request failed: HTTP 429"),
        (httpx.Response(200, text="<html>"), "clips request failed: unexpected response"),
        (httpx.ConnectError("refused"), "clips request failed (ConnectError)"),
        (httpx.ReadTimeout("slow"), "clips request failed (ReadTimeout)"),
    ],
)
def test_helix_failures_are_twitch_errors_but_not_auth_errors(
    answer: httpx.Response | Exception, message: str
) -> None:
    def helix(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(TwitchError, match=re.escape(message)) as caught:
        client(Recorder(helix)).clips(game_id="1", started_at=SINCE, ended_at=NOW)
    assert not isinstance(caught.value, TwitchAuthError)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_token_endpoint_outage_is_not_an_auth_error(status: int) -> None:
    with pytest.raises(TwitchError) as caught:
        client(lambda r: httpx.Response(status)).top_games()
    assert not isinstance(caught.value, TwitchPermanentError)


@pytest.mark.parametrize("status", [400, 403, 404, 410, 422])
def test_a_helix_4xx_is_permanent(status: int) -> None:
    rec = Recorder(lambda r: httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(TwitchPermanentError, match=f"clips request failed: HTTP {status}"):
        client(rec).clips(game_id="1", started_at=SINCE, ended_at=NOW)
    assert len(rec.helix_requests()) == 1  # not retried


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_a_helix_timeout_rate_limit_or_5xx_is_transient(status: int) -> None:
    with pytest.raises(TwitchError) as caught:
        client(Recorder(lambda r: httpx.Response(status))).top_games()
    assert not isinstance(caught.value, TwitchPermanentError)


def test_an_auth_error_is_a_permanent_error() -> None:
    assert issubclass(TwitchAuthError, TwitchPermanentError)


def test_the_secret_and_token_never_appear_in_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    messages: list[str] = []

    def echo_secret(request: httpx.Request) -> httpx.Response:
        # A hostile or buggy server echoing what it was sent.
        return httpx.Response(400, json={"message": request.content.decode()})

    for handler in (
        echo_secret,
        Recorder(lambda r: httpx.Response(401, text=r.headers["Authorization"])),
        Recorder(lambda r: httpx.Response(500, text=r.headers["Authorization"])),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"boom {SECRET}")),
        Recorder(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout(r.headers["Authorization"]))),
    ):
        with pytest.raises(TwitchError) as caught:
            client(handler).clips(game_id="1", started_at=SINCE, ended_at=NOW)
        messages.append(str(caught.value))
        messages.append(repr(caught.value))
        error = caught.value
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__

    text = "\n".join(messages) + caplog.text
    assert SECRET not in text
    assert TOKEN not in text
