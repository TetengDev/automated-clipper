"""Review app: request bodies are capped before any form parsing (CLP-104)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.types import Message

from clipper.metadata import PLATFORM_LIMITS
from clipper.review.app import MAX_REQUEST_BODY_BYTES, SECURITY_HEADERS
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

    assert response.status_code == 413
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

    assert response.status_code == 413
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

    assert response.status_code == 413


def test_body_at_the_limit_is_accepted(client: TestClient) -> None:
    prefix = b"password="
    body = prefix + b"p" * (MAX_REQUEST_BODY_BYTES - len(prefix))
    assert len(body) == MAX_REQUEST_BODY_BYTES

    response = client.post(
        "/login", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code != 413


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


async def _call(body_chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> list[Message]:
    from clipper.review.app import _BodySizeLimit

    seen: list[int] = []

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
    sent = asyncio.run(_call([b"ok"], [(b"content-length", b"11")]))

    assert sent[0]["status"] == 413


@pytest.mark.parametrize("value", [b"abc", b"-1", b"1e3", b"", b"\xd9\xa1"])
def test_malformed_content_length_is_400(value: bytes) -> None:
    sent = asyncio.run(_call([b"ok"], [(b"content-length", value)]))

    assert sent[0]["status"] == 400


def test_small_body_passes_through() -> None:
    sent = asyncio.run(_call([b"12345", b"67890"], [(b"content-length", b"10")]))

    assert sent[0]["status"] == 200
