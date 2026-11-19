"""The shared Google refresh-token exchange (CLP-93, extracted for CLP-154)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from clipper.posting.youtube import (
    ANALYTICS_SCOPE,
    SCOPES,
    TOKEN_URL,
    GrantRejected,
    TokenGrant,
    YouTubeError,
    mint_access_token,
)

REFRESH = "1//recorded-refresh-token-value"
SECRET = "GOCSPX-recorded-client-secret"
ACCESS = "ya29.recorded-access-token-value"


def _mint(handler: Callable[[httpx.Request], httpx.Response]) -> TokenGrant:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return mint_access_token(
        http, "123.apps.googleusercontent.com", SecretStr(SECRET), SecretStr(REFRESH)
    )


def test_mint_posts_the_refresh_grant_and_clamps_the_lifetime() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 10})

    grant = _mint(handler)

    assert grant.value.get_secret_value() == ACCESS
    assert grant.lifetime_s == 300
    assert str(seen[0].url) == TOKEN_URL
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == [REFRESH]
    assert ACCESS not in repr(grant)


@pytest.mark.parametrize(("status", "reason"), [(400, "invalid_grant"), (401, "invalid_client")])
def test_a_rejected_refresh_token_raises_grant_rejected_without_secrets(
    status: int, reason: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": reason, "error_description": "revoked"})

    with pytest.raises(GrantRejected) as info:
        _mint(handler)

    assert info.value.reason == reason
    assert info.value.status == status
    for text in (str(info.value), repr(info.value)):
        assert REFRESH not in text and SECRET not in text


def test_other_token_failures_are_youtube_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "backend"})

    with pytest.raises(YouTubeError) as info:
        _mint(handler)

    assert not isinstance(info.value, GrantRejected)
    assert info.value.status == 503


def test_a_body_without_a_token_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    with pytest.raises(YouTubeError, match="unexpected body"):
        _mint(handler)


def test_the_analytics_scope_is_one_of_the_account_scopes() -> None:
    assert ANALYTICS_SCOPE == "https://www.googleapis.com/auth/yt-analytics.readonly"
    assert ANALYTICS_SCOPE in SCOPES
