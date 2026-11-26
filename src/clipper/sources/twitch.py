"""Twitch Helix: `clipper resolve` (login -> user_id, game name -> game_id) and commentary
discovery (CLP-144: Get Clips, Get Top Games, Get Channel Information).

Uses an app access token (client-credentials grant). The client secret is sent in the
POST body only, and neither the secret nor the token ever appears in an error message:
messages carry the request kind and the HTTP status or exception type, never a body, a
URL or a header.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - an endpoint, not a secret
HELIX_URL = "https://api.twitch.tv/helix"
USERS_URL = f"{HELIX_URL}/users"
CLIPS_URL = f"{HELIX_URL}/clips"
GAMES_URL = f"{HELIX_URL}/games"
TOP_GAMES_URL = f"{HELIX_URL}/games/top"
CHANNELS_URL = f"{HELIX_URL}/channels"
USERS_BATCH = 100
BATCH = 100
"""Most ids or names one Helix call takes (users, games, channels)."""
CLIPS_PAGE = 100
"""Get Clips' largest page."""
MAX_CLIP_PAGES = 5
TOP_GAMES = 20


class TwitchError(RuntimeError):
    """A Helix or token request failed. Message is safe to show."""


class TwitchPermanentError(TwitchError):
    """Twitch refused the request itself (a 4xx other than 401, 408 or 429): retrying the
    same request will not help."""


class TwitchAuthError(TwitchPermanentError):
    """Twitch rejected the app credentials (or a fresh token): retrying will not help."""


def _refused(status: int) -> bool:
    """A 4xx that is not a timeout (408) or a rate limit (429): the request is wrong."""
    return 400 <= status < 500 and status not in (408, 429)


@dataclass(frozen=True)
class Clip:
    """One Get Clips row. Twitch gives the streamer's id and display name, not the login."""

    id: str
    url: str
    broadcaster_id: str
    broadcaster_name: str
    game_id: str
    language: str
    title: str
    view_count: int
    created_at: datetime
    duration_s: float


@dataclass(frozen=True)
class ChannelInfo:
    """One Get Channel Information row: the login, the channel's current game and its
    content classification labels (e.g. Gambling, SexualThemes)."""

    broadcaster_id: str
    login: str
    name: str
    game_id: str
    labels: frozenset[str]


def _json(response: httpx.Response, what: str) -> dict[str, object]:
    if response.status_code != 200:
        raise TwitchError(f"Twitch {what} failed: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise TwitchError(f"Twitch {what} failed: unexpected response")
    return body


def _rows(body: Mapping[str, object]) -> list[dict[str, object]]:
    data = body.get("data")
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _numeric_id(value: object) -> str | None:
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def app_token(http: httpx.Client, client_id: str, client_secret: str) -> str:
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
        )
    except httpx.HTTPError as exc:
        raise TwitchError(f"Twitch token request failed ({type(exc).__name__})") from None
    if _refused(response.status_code):
        raise TwitchAuthError(f"Twitch token request failed: HTTP {response.status_code}")
    token = _json(response, "token request").get("access_token")
    if not isinstance(token, str) or not token:
        raise TwitchError("Twitch token request failed: no access_token")
    return token


def user_ids(
    http: httpx.Client, token: str, client_id: str, logins: Sequence[str]
) -> dict[str, str]:
    """login -> user_id for the logins Twitch knows; unknown logins are left out."""
    found: dict[str, str] = {}
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    for start in range(0, len(logins), USERS_BATCH):
        batch = list(logins[start : start + USERS_BATCH])
        try:
            response = http.get(USERS_URL, params=[("login", x) for x in batch], headers=headers)
        except httpx.HTTPError as exc:
            raise TwitchError(f"Twitch users request failed ({type(exc).__name__})") from None
        for user in _rows(_json(response, "users request")):
            login, user_id = user.get("login"), _numeric_id(user.get("id"))
            if isinstance(login, str) and user_id is not None:
                found[login.lower()] = user_id
    return found


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(row: Mapping[str, object]) -> Clip | None:
    """A Get Clips row, or None when it is malformed."""
    clip_id, url, title = row.get("id"), row.get("url"), row.get("title")
    name, game, language = row.get("broadcaster_name"), row.get("game_id"), row.get("language")
    views, duration, created = row.get("view_count"), row.get("duration"), row.get("created_at")
    broadcaster = _numeric_id(row.get("broadcaster_id"))
    if not (isinstance(clip_id, str) and clip_id and isinstance(url, str) and url):
        return None
    if broadcaster is None or not isinstance(views, int) or isinstance(views, bool) or views < 0:
        return None
    if not isinstance(duration, int | float) or isinstance(duration, bool) or duration <= 0:
        return None
    try:
        when = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        return None
    return Clip(
        id=clip_id,
        url=url,
        broadcaster_id=broadcaster,
        broadcaster_name=name if isinstance(name, str) else "",
        game_id=game if isinstance(game, str) else "",
        language=language if isinstance(language, str) else "",
        title=title if isinstance(title, str) else "",
        view_count=views,
        created_at=when.astimezone(UTC),
        duration_s=float(duration),
    )


def _channel(row: Mapping[str, object]) -> ChannelInfo | None:
    broadcaster, login = _numeric_id(row.get("broadcaster_id")), row.get("broadcaster_login")
    if broadcaster is None or not isinstance(login, str) or not login:
        return None
    name, game, labels = (
        row.get("broadcaster_name"),
        row.get("game_id"),
        row.get("content_classification_labels"),
    )
    return ChannelInfo(
        broadcaster_id=broadcaster,
        login=login.lower(),
        name=name if isinstance(name, str) else login,
        game_id=game if isinstance(game, str) else "",
        labels=frozenset(x for x in labels if isinstance(x, str))
        if isinstance(labels, list)
        else frozenset(),
    )


class Helix:
    """Helix calls with one app token. A 401 from Helix (an expired or revoked token)
    fetches a new token once and retries the call; a second 401 is a TwitchAuthError."""

    def __init__(self, http: httpx.Client, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None

    def _get(self, url: str, params: Sequence[tuple[str, str]], what: str) -> dict[str, object]:
        for fresh in (False, True):
            if self._token is None or fresh:
                self._token = app_token(self._http, self._client_id, self._client_secret)
            headers = {"Client-Id": self._client_id, "Authorization": f"Bearer {self._token}"}
            try:
                response = self._http.get(url, params=list(params), headers=headers)
            except httpx.HTTPError as exc:
                raise TwitchError(f"Twitch {what} failed ({type(exc).__name__})") from None
            if response.status_code == 401:
                continue  # expired or revoked token: fetch a new one once
            if _refused(response.status_code):
                raise TwitchPermanentError(f"Twitch {what} failed: HTTP {response.status_code}")
            return _json(response, what)
        raise TwitchAuthError(f"Twitch {what} failed: HTTP 401")

    def clips(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        game_id: str | None = None,
        broadcaster_id: str | None = None,
        first: int = CLIPS_PAGE,
        max_pages: int = MAX_CLIP_PAGES,
    ) -> list[Clip]:
        """Get Clips for one game or one broadcaster, made between the two times, following
        the cursor for at most `max_pages` pages. Malformed rows are left out."""
        key, value = (
            ("game_id", game_id) if game_id is not None else ("broadcaster_id", broadcaster_id)
        )
        if value is None:
            raise ValueError("clips needs a game_id or a broadcaster_id")
        base = [
            (key, value),
            ("first", str(first)),
            ("started_at", _rfc3339(started_at)),
            ("ended_at", _rfc3339(ended_at)),
        ]
        found: list[Clip] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params = base if cursor is None else [*base, ("after", cursor)]
            body = self._get(CLIPS_URL, params, "clips request")
            rows = _rows(body)
            found += [c for c in map(_clip, rows) if c is not None]
            pagination = body.get("pagination")
            cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
            if not rows or not isinstance(cursor, str) or not cursor:
                break
        return found

    def top_games(self, first: int = TOP_GAMES) -> list[str]:
        """Get Top Games: today's most watched game ids, most watched first."""
        body = self._get(TOP_GAMES_URL, [("first", str(first))], "top games request")
        return [g for g in (_numeric_id(r.get("id")) for r in _rows(body)) if g is not None]

    def channels(self, broadcaster_ids: Sequence[str]) -> dict[str, ChannelInfo]:
        """Get Channel Information, 100 broadcasters a call; unknown ids are left out."""
        found: dict[str, ChannelInfo] = {}
        for start in range(0, len(broadcaster_ids), BATCH):
            batch = broadcaster_ids[start : start + BATCH]
            params = [("broadcaster_id", b) for b in batch]
            for row in _rows(self._get(CHANNELS_URL, params, "channels request")):
                channel = _channel(row)
                if channel is not None:
                    found[channel.broadcaster_id] = channel
        return found

    def game_ids(self, names: Sequence[str]) -> dict[str, str]:
        """Get Games by name: asked name -> game_id for the names Twitch knows. The answer
        is matched back to the asked name ignoring case, in case Twitch's capitalisation
        differs from the config's."""
        asked = {n.casefold(): n for n in names}
        found: dict[str, str] = {}
        for start in range(0, len(names), BATCH):
            params = [("name", n) for n in names[start : start + BATCH]]
            for row in _rows(self._get(GAMES_URL, params, "games request")):
                name, game_id = row.get("name"), _numeric_id(row.get("id"))
                if isinstance(name, str) and game_id is not None:
                    found[asked.get(name.casefold(), name)] = game_id
        return found

    def user_ids(self, logins: Sequence[str]) -> dict[str, str]:
        """login -> user_id (Get Users), with this client's token."""
        found: dict[str, str] = {}
        for start in range(0, len(logins), USERS_BATCH):
            params = [("login", x) for x in logins[start : start + USERS_BATCH]]
            for user in _rows(self._get(USERS_URL, params, "users request")):
                login, user_id = user.get("login"), _numeric_id(user.get("id"))
                if isinstance(login, str) and user_id is not None:
                    found[login.lower()] = user_id
        return found
