"""Meta Graph API plumbing (CLP-94): credentials from the CLP-91 secrets, error mapping,
tokens kept out of URLs and logs, the upload host allowlist, and token expiry tracking."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from clipper.posting.models import PublishDeferred
from clipper.publish.meta import (
    TOKEN_CHECK_EVERY,
    GraphClient,
    MetaError,
    MissingCredentials,
    check_token,
    load_credentials,
)
from meta_support import (
    ENV,
    FB_TOKEN,
    PAGE_ID,
    T0,
    Alerts,
    Clock,
    graph,
    guard,
    recorded,
    reply,
    video,
)

TOKEN = SecretStr(FB_TOKEN)


# --- credentials -------------------------------------------------------------------------


def test_credentials_come_from_the_account_secrets() -> None:
    creds = load_credentials("fb_main", env=ENV)

    assert creds.account_id == "fb_main"
    assert creds.object_id == PAGE_ID
    assert creds.token.get_secret_value() == FB_TOKEN
    assert FB_TOKEN not in repr(creds) and FB_TOKEN not in str(creds)


def test_missing_credentials_name_the_variables_not_the_values() -> None:
    env = {"CLIPPER_ACCESS_TOKEN_FB_MAIN": FB_TOKEN}

    with pytest.raises(MissingCredentials) as caught:
        load_credentials("fb_main", env=env)

    assert "CLIPPER_META_ID_FB_MAIN" in str(caught.value)
    assert FB_TOKEN not in str(caught.value)


def test_missing_token_is_named() -> None:
    with pytest.raises(MissingCredentials, match="CLIPPER_ACCESS_TOKEN_FB_MAIN"):
        load_credentials("fb_main", env={"CLIPPER_META_ID_FB_MAIN": PAGE_ID})


@pytest.mark.parametrize("object_id", ["../me", "123/feed", "abc", "1" * 40])
def test_object_id_must_be_numeric(object_id: str) -> None:
    env = {**ENV, "CLIPPER_META_ID_FB_MAIN": object_id}

    with pytest.raises(MissingCredentials, match="numeric"):
        load_credentials("fb_main", env=env)


# --- client ------------------------------------------------------------------------------


@respx.mock
def test_token_goes_in_the_authorization_header_never_the_url() -> None:
    route = respx.get(graph(f"{PAGE_ID}")).mock(return_value=httpx.Response(200, json={"id": "1"}))

    with httpx.Client() as http:
        assert GraphClient(http).get(PAGE_ID, TOKEN, params={"fields": "id"}) == {"id": "1"}

    request = route.calls.last.request
    assert request.headers["authorization"] == f"OAuth {FB_TOKEN}"
    assert FB_TOKEN not in str(request.url)


@respx.mock
def test_graph_error_is_parsed_and_redacted() -> None:
    respx.post(graph(f"{PAGE_ID}/video_reels")).mock(return_value=reply("error_token_expired", 400))

    with httpx.Client() as http, pytest.raises(MetaError) as caught:
        GraphClient(http).post(f"{PAGE_ID}/video_reels", TOKEN, data={"upload_phase": "start"})

    error = caught.value
    assert error.status == 400 and error.code == 190 and error.subcode == 463
    assert error.token_problem and not error.rate_limited
    assert "has expired" in str(error)
    assert FB_TOKEN not in str(error)


@pytest.mark.parametrize(
    ("fixture", "rate", "transient"),
    [
        ("error_rate_limit", True, False),
        ("error_ig_publish_limit", True, False),
        ("error_transient", False, True),
        ("error_invalid_parameter", False, False),
    ],
)
@respx.mock
def test_error_classes(fixture: str, rate: bool, transient: bool) -> None:
    respx.get(graph("x1")).mock(return_value=reply(fixture, 400))

    with httpx.Client() as http, pytest.raises(MetaError) as caught:
        GraphClient(http).get("x1", TOKEN)

    assert caught.value.rate_limited is rate
    assert caught.value.transient is transient
    assert not caught.value.token_problem


@respx.mock
def test_non_json_error_and_server_error() -> None:
    respx.get(graph("x1")).mock(return_value=httpx.Response(502, text="<html>bad gateway"))

    with httpx.Client() as http, pytest.raises(MetaError) as caught:
        GraphClient(http).get("x1", TOKEN)

    assert caught.value.status == 502 and caught.value.transient


@respx.mock
def test_transport_error_is_a_transient_meta_error() -> None:
    respx.get(graph("x1")).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with httpx.Client() as http, pytest.raises(MetaError) as caught:
        GraphClient(http).get("x1", TOKEN)

    assert caught.value.transient and caught.value.status is None


@respx.mock
def test_upload_sends_the_file_to_rupload(tmp_path: Path) -> None:
    url = recorded("fb_reels_start")["upload_url"]
    route = respx.post(url).mock(return_value=reply("fb_upload_ok"))

    with httpx.Client() as http:
        body = GraphClient(http).upload(url, TOKEN, video(tmp_path, 4096))

    assert body == {"success": True}
    request = route.calls.last.request
    assert request.headers["offset"] == "0"
    assert request.headers["file_size"] == "4096"
    assert request.headers["authorization"] == f"OAuth {FB_TOKEN}"
    assert len(request.content) == 4096


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/upload",
        "http://rupload.facebook.com/video-upload/v26.0/1",
        "https://rupload.facebook.com.evil.example/x",
        "https://user@rupload.facebook.com:8443/x",
    ],
)
def test_upload_refuses_any_other_host(tmp_path: Path, url: str) -> None:
    with httpx.Client() as http, pytest.raises(MetaError, match="upload URL"):
        GraphClient(http).upload(url, TOKEN, video(tmp_path))


def test_path_must_be_a_graph_path() -> None:
    with httpx.Client() as http, pytest.raises(ValueError):
        GraphClient(http).get("https://evil.example/x", TOKEN)


# --- token health ------------------------------------------------------------------------


@respx.mock
def test_check_token_reads_expiry() -> None:
    respx.get(graph("debug_token")).mock(return_value=reply("debug_token_page"))

    with httpx.Client() as http:
        health = check_token(GraphClient(http), TOKEN)

    assert health.valid
    assert health.expires_at is None  # 0 means a Page token that never expires
    assert health.data_access_expires_at == datetime.fromtimestamp(1830000000, UTC)
    assert health.expiry == health.data_access_expires_at


@respx.mock
def test_guard_passes_a_healthy_token_and_caches_the_check() -> None:
    route = respx.get(graph("debug_token")).mock(return_value=reply("debug_token_page"))
    clock, alerts = Clock(), Alerts()
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http:
        token_guard = guard(http, clock, alerts)
        token_guard.ensure(creds)
        token_guard.ensure(creds)
        assert route.call_count == 1
        clock.now += TOKEN_CHECK_EVERY
        token_guard.ensure(creds)

    assert route.call_count == 2
    assert alerts.messages == []


@respx.mock
def test_guard_defers_and_alerts_once_on_an_invalid_token() -> None:
    respx.get(graph("debug_token")).mock(return_value=reply("debug_token_invalid"))
    clock, alerts = Clock(), Alerts()
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http:
        token_guard = guard(http, clock, alerts)
        with pytest.raises(PublishDeferred) as caught:
            token_guard.ensure(creds)
        with pytest.raises(PublishDeferred):
            token_guard.ensure(creds)

    assert caught.value.until > T0
    assert len(alerts.messages) == 1
    assert "fb_main" in alerts.messages[0] and "paused" in alerts.messages[0]
    assert FB_TOKEN not in alerts.messages[0]


@respx.mock
def test_guard_alerts_seven_days_before_expiry() -> None:
    body = recorded("debug_token_page")
    expires = T0 + timedelta(days=6)
    body["data"]["data_access_expires_at"] = int(expires.timestamp())
    respx.get(graph("debug_token")).mock(return_value=httpx.Response(200, json=body))
    clock, alerts = Clock(), Alerts()
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http:
        token_guard = guard(http, clock, alerts)
        token_guard.ensure(creds)  # still usable: no exception
        clock.now += TOKEN_CHECK_EVERY
        token_guard.ensure(creds)  # reminded once a day, not per check

    assert len(alerts.messages) == 1
    assert "fb_main" in alerts.messages[0] and "2027-01-21" in alerts.messages[0]


@respx.mock
def test_guard_defers_once_the_expiry_has_passed() -> None:
    body = recorded("debug_token_page")
    body["data"]["expires_at"] = int((T0 - timedelta(minutes=1)).timestamp())
    respx.get(graph("debug_token")).mock(return_value=httpx.Response(200, json=body))
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http, pytest.raises(PublishDeferred, match="expired"):
        guard(http, Clock(), Alerts()).ensure(creds)


@respx.mock
def test_a_replaced_token_is_checked_again_at_once() -> None:
    route = respx.get(graph("debug_token"))
    route.side_effect = [reply("debug_token_invalid"), reply("debug_token_page")]
    clock, alerts = Clock(), Alerts()

    with httpx.Client() as http:
        token_guard = guard(http, clock, alerts)
        with pytest.raises(PublishDeferred):
            token_guard.ensure(load_credentials("fb_main", env=ENV))
        fixed = {**ENV, "CLIPPER_ACCESS_TOKEN_FB_MAIN": "EAAB-new-token"}
        token_guard.ensure(load_credentials("fb_main", env=fixed))

    assert route.call_count == 2


@respx.mock
def test_guard_proceeds_when_the_check_itself_fails(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(graph("debug_token")).mock(return_value=reply("error_transient", 500))
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http, caplog.at_level(logging.WARNING):
        guard(http, Clock(), Alerts()).ensure(creds)

    assert "token check failed" in caplog.text
    assert FB_TOKEN not in caplog.text


def test_unusable_defers_with_an_alert() -> None:
    clock, alerts = Clock(), Alerts()
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http:
        token_guard = guard(http, clock, alerts)
        error = token_guard.unusable(creds.account_id, creds.token, "code 190")

    assert isinstance(error, PublishDeferred)
    assert error.until == T0 + timedelta(hours=1)
    assert len(alerts.messages) == 1


# --- review fixes ------------------------------------------------------------------------


@respx.mock
def test_a_token_echoed_in_an_error_is_scrubbed() -> None:
    error = {"error": {"message": f"Malformed access token {FB_TOKEN}", "code": 190}}
    respx.get(graph("x1")).mock(return_value=httpx.Response(400, json=error))

    with httpx.Client() as http, pytest.raises(MetaError) as caught:
        GraphClient(http).get("x1", TOKEN)

    assert FB_TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    ["Malformed access token EAABsbCS1iHgBAKZBZCqZAexample0123", "OAuth EAAB-anything.123"],
)
def test_redact_masks_meta_tokens(text: str) -> None:
    from clipper.redact import redact

    assert "EAAB" not in redact(text)


def test_graph_ids_are_ascii_digits_only() -> None:
    from clipper.publish.meta import graph_id

    with pytest.raises(MetaError):
        graph_id("١٢٣", "video id")
    env = {**ENV, "CLIPPER_META_ID_FB_MAIN": "١٢٣"}
    with pytest.raises(MissingCredentials):
        load_credentials("fb_main", env=env)


def test_upload_over_the_size_limit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipper.posting.models import PublishRejected
    from clipper.publish import meta

    monkeypatch.setattr(meta, "MAX_UPLOAD_BYTES", 10)
    url = recorded("fb_reels_start")["upload_url"]

    with httpx.Client() as http, pytest.raises(PublishRejected, match="1 GiB"):
        GraphClient(http).upload(url, TOKEN, video(tmp_path, 11))


@respx.mock
def test_a_failed_check_is_not_repeated_for_a_while() -> None:
    from clipper.publish.meta import CHECK_RETRY

    route = respx.get(graph("debug_token")).mock(return_value=reply("error_transient", 500))
    clock = Clock()
    creds = load_credentials("fb_main", env=ENV)

    with httpx.Client() as http:
        token_guard = guard(http, clock, Alerts())
        token_guard.ensure(creds)
        token_guard.ensure(creds)
        assert route.call_count == 1
        clock.now += CHECK_RETRY
        token_guard.ensure(creds)

    assert route.call_count == 2


def test_graph_client_keeps_httpx_request_logs_off() -> None:
    logging.getLogger("httpx").setLevel(logging.DEBUG)

    with httpx.Client() as http:
        GraphClient(http)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://www.instagram.com/reel/C1/", True),
        ("https://evil.example\\@www.instagram.com/x", False),
        ("https://user@www.instagram.com/x", False),
        ("https://www.instagram.com:8443/x", False),
        ("javascript:alert(1)", False),
    ],
)
def test_post_safe_url_refuses_lookalike_links(url: str, shown: bool) -> None:
    from dataclasses import replace

    from meta_support import make_post

    post = replace(make_post(), url=url)

    assert (post.safe_url == url) is shown
