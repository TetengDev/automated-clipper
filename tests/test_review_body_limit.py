"""Review app: request bodies are capped before any form parsing (CLP-104), forms are
capped at 200 fields, and HTMX requests get a readable 413 (CLP-110)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.formparsers import FormParser, MultiPartParser
from starlette.requests import Request
from starlette.types import Message

from clipper.metadata import PLATFORM_LIMITS
from clipper.review.app import (
    MAX_FORM_FIELDS,
    MAX_REQUEST_BODY_BYTES,
    SECURITY_HEADERS,
    _BodyTooLarge,
    csrf_protect,
)
from review_support import Env, login, make_env

ONE_MB = 1024 * 1024


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    env = make_env(tmp_path)
    yield env
    env.conn.close()


@pytest.fixture
def client(env: Env) -> TestClient:
    return env.client()


@pytest.fixture
def form_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts every `Request.form()` call, to prove a rejected body is never parsed."""
    calls: list[int] = []
    original = Request.form

    def spy(self: Request, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Request, "form", spy)
    return calls


def test_limit_is_about_256_kb() -> None:
    assert MAX_REQUEST_BODY_BYTES == 256 * 1024


def test_one_mb_form_post_is_413_before_it_is_parsed(
    client: TestClient, env: Env, form_calls: list[int]
) -> None:
    csrf = login(client)
    clip_id = env.clip()
    form_calls.clear()

    response = client.post(
        f"/clip/{clip_id}/variants",
        data={"csrf_token": csrf, "version": "1", "tiktok_caption": "x" * ONE_MB},
    )

    assert_plain_413(response)
    assert form_calls == []
    assert env.reviews(clip_id) == []


def test_one_mb_post_to_login_is_413_without_a_session(
    client: TestClient, form_calls: list[int]
) -> None:
    response = client.post("/login", data={"password": "p" * ONE_MB})

    assert response.status_code == 413
    assert form_calls == []


def test_one_mb_multipart_post_is_413(client: TestClient, form_calls: list[int]) -> None:
    csrf = login(client)
    form_calls.clear()

    response = client.post(
        "/logout",
        data={"csrf_token": csrf},
        files={"blob": ("blob.bin", b"\0" * ONE_MB, "application/octet-stream")},
    )

    assert response.status_code == 413
    assert form_calls == []


def test_413_response_carries_the_security_headers(client: TestClient) -> None:
    response = client.post("/login", data={"password": "p" * ONE_MB})

    assert response.status_code == 413
    for name in SECURITY_HEADERS:
        assert name in response.headers
    assert response.headers["Cache-Control"] == "no-store"


def test_streamed_body_without_content_length_is_413(client: TestClient, env: Env) -> None:
    csrf = login(client)
    clip_id = env.clip()

    def chunks() -> Iterator[bytes]:
        yield f"csrf_token={csrf}&version=1&tiktok_caption=".encode()
        for _ in range(64):
            yield b"x" * 16 * 1024  # 1 MB in total, never announced up front

    response = client.post(
        f"/clip/{clip_id}/variants",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert_plain_413(response)
    assert env.reviews(clip_id) == []


def test_streamed_body_to_a_route_without_form_params_is_413(client: TestClient) -> None:
    """`/logout` takes no form fields, so only `csrf_protect` reads the body."""
    login(client)

    def chunks() -> Iterator[bytes]:
        yield b"csrf_token="
        for _ in range(64):
            yield b"x" * 16 * 1024

    response = client.post(
        "/logout",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert_plain_413(response)


def _padded_logout_form(csrf: str, size: int) -> bytes:
    prefix = f"csrf_token={csrf}&pad=".encode()
    return prefix + b"p" * (size - len(prefix))


def test_body_at_the_limit_reaches_the_route(client: TestClient) -> None:
    body = _padded_logout_form(login(client), MAX_REQUEST_BODY_BYTES)

    response = client.post(
        "/logout", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code == 303  # parsed, CSRF checked, logged out


def test_body_one_byte_over_the_limit_is_413(client: TestClient) -> None:
    body = _padded_logout_form(login(client), MAX_REQUEST_BODY_BYTES + 1)

    response = client.post(
        "/logout", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code == 413


def _stream(body: bytes, chunk: int = 16 * 1024) -> Iterator[bytes]:
    for i in range(0, len(body), chunk):
        yield body[i : i + chunk]


def test_streamed_body_at_the_limit_reaches_the_route(client: TestClient) -> None:
    body = _padded_logout_form(login(client), MAX_REQUEST_BODY_BYTES)

    response = client.post(
        "/logout",
        content=_stream(body),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 303


def test_streamed_body_one_byte_over_the_limit_is_413(client: TestClient) -> None:
    body = _padded_logout_form(login(client), MAX_REQUEST_BODY_BYTES + 1)

    response = client.post(
        "/logout",
        content=_stream(body),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert_plain_413(response)


def assert_plain_413(response, text: str = "Request body too large") -> None:  # type: ignore[no-untyped-def]
    """Both 413 paths (declared length, streamed overflow) give the same closing response."""
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == text
    assert response.headers["connection"] == "close"
    for name in SECURITY_HEADERS:
        assert name in response.headers


def test_huge_content_length_is_413_not_a_crash() -> None:
    sent = asyncio.run(_call([b"ok"], [(b"content-length", b"9" * 5000)]))

    assert sent[0]["status"] == 413


def test_largest_legitimate_variants_form_fits_under_the_limit(
    client: TestClient, env: Env
) -> None:
    """Every title and caption at its platform maximum, in 4-byte characters that
    percent-encode to 12 bytes each, is still well under the cap."""
    csrf = login(client)
    clip_id = env.clip()
    data = {"csrf_token": csrf, "version": "1", "fingerprint": "0" * 64}
    for platform, limits in PLATFORM_LIMITS.items():
        data[f"{platform}_title"] = "\U0001f600" * limits.title_max
        data[f"{platform}_caption"] = "\U0001f600" * limits.caption_max

    response = client.post(f"/clip/{clip_id}/variants", data=data)

    assert response.status_code != 413
    assert response.status_code == 409  # reached the route: the fingerprint is stale


def test_normal_review_actions_are_unaffected(client: TestClient, env: Env) -> None:
    csrf = login(client)
    clip_id = env.clip()

    page = client.get(f"/clip/{clip_id}")
    logout = client.post("/logout", data={"csrf_token": csrf})

    assert page.status_code == 200
    assert logout.status_code == 303


# --- the middleware on its own: Content-Length cannot be used to sneak past -------------


async def _call(
    body_chunks: list[bytes], headers: list[tuple[bytes, bytes]], seen: list[int] | None = None
) -> list[Message]:
    from clipper.review.app import _BodySizeLimit

    seen = [] if seen is None else seen

    async def inner(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        while True:
            message = await receive()
            seen.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def messages() -> AsyncIterator[Message]:
        for i, chunk in enumerate(body_chunks):
            yield {"type": "http.request", "body": chunk, "more_body": i < len(body_chunks) - 1}

    stream = messages()

    async def receive() -> Message:
        return await anext(stream)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": headers}
    await _BodySizeLimit(inner, max_bytes=10)(scope, receive, send)
    return sent


def test_understated_content_length_is_still_counted() -> None:
    sent = asyncio.run(_call([b"12345", b"678901"], [(b"content-length", b"3")]))

    assert sent[0]["status"] == 413


def test_overstated_content_length_is_413_without_reading() -> None:
    seen: list[int] = []
    sent = asyncio.run(_call([b"ok"], [(b"content-length", b"11")], seen))

    assert sent[0]["status"] == 413
    assert seen == []  # the app never ran


@pytest.mark.parametrize("value", [b"abc", b"-1", b"1e3", b"", b"\xd9\xa1"])
def test_malformed_content_length_is_400(value: bytes) -> None:
    sent = asyncio.run(_call([b"ok"], [(b"content-length", value)]))

    assert sent[0]["status"] == 400


def test_small_body_passes_through() -> None:
    sent = asyncio.run(_call([b"12345", b"67890"], [(b"content-length", b"10")]))

    assert sent[0]["status"] == 200


def test_non_http_scopes_pass_through() -> None:
    from clipper.review.app import _BodySizeLimit

    seen: list[str] = []

    async def inner(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        seen.append(scope["type"])

    async def noop(*_args: object) -> None:
        return None

    limiter = _BodySizeLimit(inner, max_bytes=1)
    asyncio.run(limiter({"type": "lifespan"}, noop, noop))  # type: ignore[arg-type]

    assert seen == ["lifespan"]


def test_overflow_after_the_response_started_is_reraised() -> None:
    """Too late to answer 413: the error propagates so the server drops the connection."""
    from clipper.review.app import _BodySizeLimit, _BodyTooLarge

    async def inner(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11, "more_body": False}

    async def send(_message: Message) -> None:
        return None

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    with pytest.raises(_BodyTooLarge):
        asyncio.run(_BodySizeLimit(inner, max_bytes=10)(scope, receive, send))


# --- CLP-110: form field cap, and a readable 413 for HTMX ----------------------------------

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HTMX = {"HX-Request": "true", "HX-Target": "action-error"}


@pytest.fixture
def fields_parsed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts every field the form parsers finish, to prove a refused form is not parsed."""
    parsed: list[int] = []
    for parser in (FormParser, MultiPartParser):
        original = parser.on_part_end if parser is MultiPartParser else parser.on_field_end

        def spy(self, _original=original) -> None:  # type: ignore[no-untyped-def]
            parsed.append(1)
            _original(self)

        name = "on_part_end" if parser is MultiPartParser else "on_field_end"
        monkeypatch.setattr(parser, name, spy)
    return parsed


def _urlencoded(csrf: str, fields: int) -> bytes:
    """A form of exactly `fields` fields, the CSRF token among them."""
    return "&".join([f"csrf_token={csrf}", *(f"f{i}=x" for i in range(fields - 1))]).encode()


BOUNDARY = "clp110boundary"


def _multipart(csrf: str, parts: int) -> bytes:
    """A multipart form of exactly `parts` parts, the CSRF token among them."""
    names = ["csrf_token", *(f"f{i}" for i in range(parts - 1))]
    out = b""
    for name in names:
        value = csrf if name == "csrf_token" else "x"
        out += (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    return out + f"--{BOUNDARY}--\r\n".encode()


MULTIPART = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


def test_form_field_limit_is_200() -> None:
    assert MAX_FORM_FIELDS == 200


def test_thousands_of_fields_are_413_before_they_are_parsed(
    client: TestClient, env: Env, fields_parsed: list[int]
) -> None:
    csrf = login(client)
    clip_id = env.clip()
    body = b"version=1&fingerprint=x&" + _urlencoded(csrf, 5000)
    fields_parsed.clear()

    response = client.post(f"/clip/{clip_id}/variants", content=body, headers=FORM)

    assert_plain_413(response, "Too many form fields")
    assert fields_parsed == []
    assert env.reviews(clip_id) == []


def test_streamed_thousands_of_fields_are_413(client: TestClient, env: Env) -> None:
    csrf = login(client)
    clip_id = env.clip()
    body = b"version=1&fingerprint=x&" + _urlencoded(csrf, 5000)

    response = client.post(f"/clip/{clip_id}/variants", content=_stream(body, 7), headers=FORM)

    assert_plain_413(response, "Too many form fields")


def test_bare_separators_count_as_fields(client: TestClient) -> None:
    """`a&a&a...`, the shape the issue describes: about 128K fields under the byte cap."""
    csrf = login(client)
    body = f"csrf_token={csrf}&".encode() + b"&".join([b"a"] * 100_000)

    response = client.post("/logout", content=body, headers=FORM)

    assert_plain_413(response, "Too many form fields")


@pytest.mark.parametrize("chunk", [7, 1_000_000])
def test_form_at_the_field_limit_reaches_the_route(client: TestClient, chunk: int) -> None:
    body = _urlencoded(login(client), MAX_FORM_FIELDS)

    response = client.post("/logout", content=_stream(body, chunk), headers=FORM)

    assert response.status_code == 303  # parsed, CSRF checked, logged out


@pytest.mark.parametrize("chunk", [7, 1_000_000])
def test_form_one_field_over_the_limit_is_413(client: TestClient, chunk: int) -> None:
    body = _urlencoded(login(client), MAX_FORM_FIELDS + 1)

    response = client.post("/logout", content=_stream(body, chunk), headers=FORM)

    assert_plain_413(response, "Too many form fields")


@pytest.mark.parametrize("chunk", [7, 1_000_000])  # 7 splits the boundary across chunks
def test_multipart_at_the_field_limit_reaches_the_route(client: TestClient, chunk: int) -> None:
    body = _multipart(login(client), MAX_FORM_FIELDS)

    response = client.post("/logout", content=_stream(body, chunk), headers=MULTIPART)

    assert response.status_code == 303


@pytest.mark.parametrize("chunk", [7, 1_000_000])
def test_multipart_one_part_over_the_limit_is_413(
    client: TestClient, chunk: int, fields_parsed: list[int]
) -> None:
    body = _multipart(login(client), MAX_FORM_FIELDS + 1)
    fields_parsed.clear()

    response = client.post("/logout", content=_stream(body, chunk), headers=MULTIPART)

    assert_plain_413(response, "Too many form fields")
    if chunk > len(body):
        assert fields_parsed == []


def test_csrf_check_parses_no_more_than_the_field_limit() -> None:
    """Behind the middleware this cannot trigger; on its own, the dependency still stops."""
    body = _urlencoded("t", MAX_FORM_FIELDS + 1)

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/logout",
        "headers": [(b"content-type", FORM["Content-Type"].encode())],
        "session": {"csrf": "t"},
        "app": object(),
    }
    with pytest.raises(_BodyTooLarge) as raised:
        asyncio.run(csrf_protect(Request(scope, receive)))

    assert raised.value.status_code == 413
    assert raised.value.detail == "Too many form fields"


def test_csrf_check_passes_other_form_errors_through() -> None:
    """A malformed multipart body stays Starlette's 400; only the field cap becomes 413."""

    async def receive() -> Message:
        return {"type": "http.request", "body": b"garbage", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/logout",
        "headers": [(b"content-type", b"multipart/form-data")],
        "session": {"csrf": "t"},
        "app": object(),
    }
    with pytest.raises(HTTPException) as raised:
        asyncio.run(csrf_protect(Request(scope, receive)))

    assert raised.value.status_code == 400


def _oversized_variants_edit(csrf: str) -> dict[str, str]:
    """The issue's case: four 20k-character emoji captions, which the route would accept
    field by field, but which percent-encode to almost 1 MB."""
    data = {"version": "1", "fingerprint": "0" * 64}
    for platform in PLATFORM_LIMITS:
        data[f"{platform}_caption"] = "\U0001f600" * 20_000
    return data


def assert_htmx_413(response, message: str) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/html")
    assert re.fullmatch(
        rf'<p class="error" role="alert">{re.escape(message)}</p>\s*', response.text
    )
    assert response.headers["HX-Retarget"] == "#action-error"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert response.headers["connection"] == "close"
    for name in SECURITY_HEADERS:
        assert name in response.headers


TOO_LONG = "Not saved: the form is over 256 KB. Shorten the text and try again."
TOO_MANY = "Not saved: the form has too many fields."


def test_oversized_htmx_variants_edit_shows_an_error(client: TestClient, env: Env) -> None:
    csrf = login(client)
    clip_id = env.clip()

    response = client.post(
        f"/clip/{clip_id}/variants",
        data=_oversized_variants_edit(csrf),
        headers={**HTMX, "X-CSRF-Token": csrf},
    )

    assert_htmx_413(response, TOO_LONG)
    assert env.reviews(clip_id) == []


def test_streamed_oversized_htmx_edit_shows_an_error(client: TestClient, env: Env) -> None:
    """No Content-Length: the overflow is caught mid-parse, by the app's exception handler."""
    csrf = login(client)
    clip_id = env.clip()

    def chunks() -> Iterator[bytes]:
        yield b"version=1&tiktok_caption="
        for _ in range(64):
            yield b"x" * 16 * 1024

    response = client.post(
        f"/clip/{clip_id}/variants",
        content=chunks(),
        headers={**FORM, **HTMX, "X-CSRF-Token": csrf},
    )

    assert_htmx_413(response, TOO_LONG)


def test_htmx_form_with_too_many_fields_shows_an_error(client: TestClient, env: Env) -> None:
    csrf = login(client)
    clip_id = env.clip()

    response = client.post(
        f"/clip/{clip_id}/variants",
        content=_urlencoded(csrf, 5000),
        headers={**FORM, **HTMX, "X-CSRF-Token": csrf},
    )

    assert_htmx_413(response, TOO_MANY)


def test_htmx_413_goes_to_the_requests_own_error_slot(client: TestClient) -> None:
    """Posts and campaigns pages have their own slots (`post-error-N`)."""
    csrf = login(client)

    response = client.post(
        "/posts/7/cancel",
        content=_urlencoded(csrf, 5000),
        headers={**FORM, "HX-Request": "true", "HX-Target": "post-error-7"},
    )

    assert response.status_code == 413
    assert response.headers["HX-Retarget"] == "#post-error-7"


@pytest.mark.parametrize("target", [None, "", "a, body", "#x", "1abc", "x y"])
def test_htmx_413_without_a_plain_target_id_is_not_retargeted(
    client: TestClient, target: str | None
) -> None:
    """Only a plain element id is echoed back as a selector; otherwise htmx keeps its own."""
    headers = {**FORM, "HX-Request": "true"}
    if target is not None:
        headers["HX-Target"] = target

    response = client.post("/login", content=b"&" * 1000, headers=headers)

    assert response.status_code == 413
    assert "HX-Retarget" not in response.headers
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert 'class="error"' in response.text


def test_htmx_config_swaps_a_413_into_the_target(client: TestClient, env: Env) -> None:
    """htmx does not swap 4xx unless a responseHandling rule says so; the first rule whose
    pattern matches the status wins, as in htmx's `resolveResponseHandling`."""
    login(client)
    page = client.get(f"/clip/{env.clip()}")
    meta = re.search(r'<meta name="htmx-config"\s+content=\'([^\']*)\'', page.text)
    assert meta
    rules = json.loads(meta.group(1))["responseHandling"]

    rule = next(r for r in rules if re.search(r["code"], "413"))

    assert rule["swap"] is True
    assert rule.get("error") is False
