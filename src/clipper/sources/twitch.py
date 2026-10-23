"""Twitch Helix, only what `clipper resolve` needs (login -> user_id). The Twitch source
itself comes with S3 (CLP-25).

Uses an app access token (client-credentials grant). The client secret is sent in the
POST body only and never appears in messages.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - an endpoint, not a secret
USERS_URL = "https://api.twitch.tv/helix/users"
USERS_BATCH = 100


class TwitchError(RuntimeError):
    """A Helix or token request failed. Message is safe to show."""


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
        data = _json(response, "users request").get("data")
        for user in data if isinstance(data, list) else []:
            if not isinstance(user, dict):
                continue
            login, user_id = user.get("login"), user.get("id")
            if (
                isinstance(login, str)
                and isinstance(user_id, str)
                and user_id.isascii()
                and user_id.isdigit()
            ):
                found[login.lower()] = user_id
    return found
