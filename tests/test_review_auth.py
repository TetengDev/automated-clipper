"""Review app shell: login, sessions, CSRF, throttling, headers (CLP-18)."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import bcrypt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipper.cli import app as cli_app
from clipper.db import connect, migrate
from clipper.review.app import (
    LOGIN_GLOBAL_MAX_ATTEMPTS,
    LOGIN_MAX_ATTEMPTS,
    SESSION_MAX_AGE,
    LoginThrottle,
    create_app,
    get_db,
    render,
    require_user,
    safe_next_path,
)

PASSWORD = "correct horse battery staple"
SECRET = "s" * 40
BASE_URL = "https://testserver"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _creator_config(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    """run-review loads creators.yaml for the faces check (CLP-87)."""
    monkeypatch.setenv("CLIPPER_CONFIG", str(config_file))


@pytest.fixture(scope="module")
def password_hash() -> str:
    return bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def wall() -> FakeClock:
    return FakeClock(start=1_800_000_000.0)


@pytest.fixture
def make_app(
    password_hash: str, tmp_path: Path, clock: FakeClock, wall: FakeClock
) -> Callable[..., FastAPI]:
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()

    def _make(**kwargs: object) -> FastAPI:
        params: dict[str, object] = {
            "password_hash": password_hash,
            "session_secret": SECRET,
            "db_path": db_path,
            "clock": clock,
            "wall_clock": wall,
        }
        params.update(kwargs)
        return create_app(**params)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> TestClient:
    return TestClient(make_app(), base_url=BASE_URL, follow_redirects=False)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token field missing"
    return match.group(1)


def login(client: TestClient, password: str = PASSWORD, next_: str | None = None):
    token = csrf_from(client.get("/login").text)
    data = {"password": password, "csrf_token": token}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data)


def logged_in_csrf(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return csrf_from(response.text)


# --- redirects when logged out ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/queue", "/does-not-exist"])
def test_pages_redirect_to_login_when_logged_out(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={path.replace('/', '%2F')}"


def test_redirect_preserves_query_in_next(client: TestClient) -> None:
    response = client.get("/queue?page=2")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fqueue%3Fpage%3D2"


def test_htmx_request_gets_hx_redirect_when_logged_out(client: TestClient) -> None:
    response = client.get("/queue", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert response.headers["HX-Redirect"].startswith("/login")


def test_healthz_and_static_need_no_session(client: TestClient) -> None:
    health = client.get("/healthz")
    css = client.get("/static/app.css")
    js = client.get("/static/htmx.min.js")

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert css.status_code == 200
    assert js.status_code == 200


def test_login_page_renders_with_viewport_and_csrf(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert 'name="viewport"' in response.text
    assert "width=device-width" in response.text
    assert csrf_from(response.text)
    assert "/static/htmx.min.js" in response.text
    assert "unpkg" not in response.text and "cdn" not in response.text


# --- login --------------------------------------------------------------------------


def test_wrong_password_rejected(client: TestClient) -> None:
    response = login(client, password="nope")

    assert response.status_code == 401
    assert "Incorrect password" in response.text
    assert client.get("/").status_code == 303


def test_correct_password_sets_secure_session_cookie(client: TestClient) -> None:
    response = login(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    page = client.get("/")
    assert page.status_code == 200
    assert "Review queue" in page.text


def test_insecure_cookies_option_drops_secure_flag(make_app: Callable[..., FastAPI]) -> None:
    client = TestClient(make_app(secure_cookies=False), follow_redirects=False)

    response = login(client)

    assert response.status_code == 303
    assert "secure" not in response.headers["set-cookie"].lower()


def test_login_redirects_to_safe_next(client: TestClient) -> None:
    response = login(client, next_="/queue?page=2")

    assert response.headers["location"] == "/queue?page=2"


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example",
        "https://evil.example/",
        "javascript:alert(1)",
        "/\\evil.example",
        "\\\\evil.example",
        "/\tevil",
        "evil",
        "",
    ],
)
def test_open_redirect_rejected(client: TestClient, target: str) -> None:
    response = login(client, next_=target)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/queue", "/queue"),
        ("/clips/3?x=1#y", "/clips/3?x=1#y"),
        ("//evil", "/"),
        ("/%2F%2Fevil", "/%2F%2Fevil"),
        (None, "/"),
        ("/login", "/"),
        ("http:/x", "/"),
    ],
)
def test_safe_next_path(value: str | None, expected: str) -> None:
    assert safe_next_path(value) == expected


def test_logged_in_user_visiting_login_is_sent_home(client: TestClient) -> None:
    login(client)

    response = client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_overlong_password_is_rejected_not_crashing(client: TestClient) -> None:
    response = login(client, password="x" * 200)

    assert response.status_code == 401


# --- logout -------------------------------------------------------------------------


def test_logout_clears_session(client: TestClient) -> None:
    login(client)
    token = logged_in_csrf(client)

    response = client.post("/logout", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_logout_via_get_not_allowed(client: TestClient) -> None:
    login(client)

    response = client.get("/logout")

    assert response.status_code == 405
    assert client.get("/").status_code == 200


# --- CSRF ---------------------------------------------------------------------------


def test_login_post_without_csrf_is_403(client: TestClient) -> None:
    client.get("/login")

    response = client.post("/login", data={"password": PASSWORD})

    assert response.status_code == 403


def test_login_post_with_invalid_csrf_is_403(client: TestClient) -> None:
    client.get("/login")

    response = client.post("/login", data={"password": PASSWORD, "csrf_token": "forged"})

    assert response.status_code == 403
    assert client.get("/").status_code == 303


def test_csrf_without_session_is_403(make_app: Callable[..., FastAPI]) -> None:
    fresh = TestClient(make_app(), base_url=BASE_URL, follow_redirects=False)

    response = fresh.post("/login", data={"password": PASSWORD, "csrf_token": "x"})

    assert response.status_code == 403


def test_logout_without_csrf_is_403(client: TestClient) -> None:
    login(client)

    response = client.post("/logout")

    assert response.status_code == 403
    assert client.get("/").status_code == 200


def test_csrf_header_accepted_for_htmx(client: TestClient) -> None:
    login(client)
    token = logged_in_csrf(client)

    response = client.post("/logout", headers={"X-CSRF-Token": token})

    assert response.status_code == 303


def test_csrf_token_rotates_on_login(client: TestClient) -> None:
    before = csrf_from(client.get("/login").text)
    client.post("/login", data={"password": PASSWORD, "csrf_token": before})

    after = logged_in_csrf(client)

    assert after != before


def test_csrf_enforced_on_routes_added_later(make_app: Callable[..., FastAPI]) -> None:
    """Routes added by later stories inherit CSRF + auth via app-level dependencies."""
    application = make_app()

    @application.post("/clips/1/approve")
    def approve(user: str = Depends(require_user)) -> dict[str, str]:
        return {"user": user}

    client = TestClient(application, base_url=BASE_URL, follow_redirects=False)
    assert client.post("/clips/1/approve").status_code == 303  # logged out -> login
    login(client)
    token = logged_in_csrf(client)

    assert client.post("/clips/1/approve").status_code == 403
    ok = client.post("/clips/1/approve", headers={"X-CSRF-Token": token})
    assert ok.status_code == 200
    assert ok.json() == {"user": "reviewer"}


# --- throttling ---------------------------------------------------------------------


def test_sixth_login_attempt_within_a_minute_is_throttled(
    client: TestClient, clock: FakeClock
) -> None:
    assert LOGIN_MAX_ATTEMPTS == 5
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
        clock.now += 5

    sixth = login(client)  # even the right password is refused while throttled

    assert sixth.status_code == 429
    assert int(sixth.headers["Retry-After"]) >= 1
    assert client.get("/").status_code == 303


def test_failed_and_throttled_logins_are_logged_without_password(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="clipper.review.app")
    for _ in range(6):
        login(client, password="hunter2-wrong")

    messages = [r.getMessage() for r in caplog.records]
    assert sum("login failed" in m for m in messages) == 5
    assert sum("login throttled" in m for m in messages) == 1
    assert all("testclient" in m for m in messages)  # TestClient's client host
    assert "hunter2-wrong" not in caplog.text


def test_throttle_window_slides(client: TestClient, clock: FakeClock) -> None:
    for _ in range(5):
        login(client, password="wrong")
    assert login(client, password="wrong").status_code == 429

    clock.now += 61

    assert login(client).status_code == 303


def test_attempts_spread_over_more_than_a_minute_are_not_throttled(
    client: TestClient, clock: FakeClock
) -> None:
    for _ in range(10):
        assert login(client, password="wrong").status_code == 401
        clock.now += 15


# --- headers ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/healthz", "/", "/static/app.css"])
def test_security_headers_present(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_db_path_exposed_on_app_state(make_app: Callable[..., FastAPI], tmp_path: Path) -> None:
    application = make_app()

    assert application.state.db_path == tmp_path / "state.db"


# --- configuration validation ---------------------------------------------------------


def test_create_app_rejects_non_bcrypt_hash(make_app: Callable[..., FastAPI]) -> None:
    with pytest.raises(ValueError, match="bcrypt"):
        make_app(password_hash="plaintext")


def test_create_app_rejects_short_session_secret(make_app: Callable[..., FastAPI]) -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        make_app(session_secret="short")


# --- CLI ----------------------------------------------------------------------------

runner = CliRunner()


def test_hash_password_cli_prints_verifiable_hash() -> None:
    result = runner.invoke(cli_app, ["hash-password"], input=f"{PASSWORD}\n{PASSWORD}\n")

    assert result.exit_code == 0, result.output
    hashed = re.search(r"(\$2b\$\S+)", result.stdout)
    assert hashed
    assert bcrypt.checkpw(PASSWORD.encode(), hashed.group(1).encode())
    assert PASSWORD not in result.stdout
    assert "single quote" in result.output.lower()


def test_hash_password_cli_rejects_mismatch() -> None:
    result = runner.invoke(cli_app, ["hash-password"], input="one\ntwo\none\none\n")

    # click re-prompts on mismatch; the second pair matches
    assert result.exit_code == 0, result.output
    assert "do not match" in result.output.lower()


def test_hash_password_cli_rejects_empty_and_overlong() -> None:
    empty = runner.invoke(cli_app, ["hash-password"], input="\n\n")
    long_pw = "x" * 73
    overlong = runner.invoke(cli_app, ["hash-password"], input=f"{long_pw}\n{long_pw}\n")

    assert empty.exit_code == 1
    assert overlong.exit_code == 1
    assert "72" in overlong.stderr


def test_run_review_help() -> None:
    result = runner.invoke(cli_app, ["run-review", "--help"])
    # Typer forces Rich colour under CI, which splits option names with escape codes.
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    assert result.exit_code == 0
    assert "--host" in output
    assert "--port" in output


def test_run_review_missing_secrets_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.delenv("REVIEW_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    started: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(a))

    result = runner.invoke(cli_app, ["run-review"])

    assert result.exit_code == 1
    assert "REVIEW_PASSWORD_HASH" in result.stderr
    assert started == []


def test_run_review_starts_uvicorn(
    monkeypatch: pytest.MonkeyPatch, password_hash: str, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_run(application: FastAPI, **kwargs: object) -> None:
        calls["app"] = application
        calls.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    db = tmp_path / "x.db"

    result = runner.invoke(cli_app, ["--db", str(db), "run-review", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert calls["host"] == "0.0.0.0"  # noqa: S104
    assert calls["port"] == 9123
    assert calls["proxy_headers"] is True
    assert calls["forwarded_allow_ips"] == "127.0.0.1"
    assert isinstance(calls["app"], FastAPI)
    assert calls["app"].state.db_path == db


@pytest.fixture
def review_env(monkeypatch: pytest.MonkeyPatch, password_hash: str) -> dict[str, object]:
    calls: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda application, **kw: calls.update(kw))
    monkeypatch.setenv("REVIEW_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    return calls


def test_run_review_forwarded_allow_ips_option_and_env(
    monkeypatch: pytest.MonkeyPatch, review_env: dict[str, object]
) -> None:
    by_flag = runner.invoke(cli_app, ["run-review", "--forwarded-allow-ips", "172.18.0.5"])
    assert by_flag.exit_code == 0, by_flag.output
    assert review_env["forwarded_allow_ips"] == "172.18.0.5"

    monkeypatch.setenv("CLIPPER_FORWARDED_ALLOW_IPS", "10.0.0.2,10.0.0.3")
    by_env = runner.invoke(cli_app, ["run-review"])
    assert by_env.exit_code == 0, by_env.output
    assert review_env["forwarded_allow_ips"] == "10.0.0.2,10.0.0.3"


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "127.0.0.1,*",
        " * ",
        "0.0.0.0/0",
        "10.0.0.2, 0.0.0.0/0",
        "::/0",
        "1.2.3.4/0",
        "0.0.0.0/0.0.0.0",
        "::ffff:0:0/0",
    ],
)
def test_run_review_refuses_wildcard_forwarded_ips(
    review_env: dict[str, object], value: str
) -> None:
    result = runner.invoke(cli_app, ["run-review", "--forwarded-allow-ips", value])

    assert result.exit_code == 1
    assert "forwarded-allow-ips" in result.stderr
    assert review_env == {}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])  # noqa: S104
def test_run_review_refuses_insecure_cookies_on_non_loopback_host(
    review_env: dict[str, object], host: str
) -> None:
    result = runner.invoke(cli_app, ["run-review", "--insecure-cookies", "--host", host])

    assert result.exit_code == 1
    assert "--insecure-cookies" in result.stderr
    assert "loopback" in result.stderr
    assert review_env == {}


def test_run_review_refuses_insecure_cookies_on_default_host(
    review_env: dict[str, object],
) -> None:
    result = runner.invoke(cli_app, ["run-review", "--insecure-cookies"])

    assert result.exit_code == 1
    assert review_env == {}


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "[::1]", "localhost"])
def test_run_review_allows_insecure_cookies_on_loopback(
    review_env: dict[str, object], host: str
) -> None:
    result = runner.invoke(cli_app, ["run-review", "--insecure-cookies", "--host", host])

    assert result.exit_code == 0, result.output
    assert review_env["host"] == host


def test_run_review_allows_insecure_cookies_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch, review_env: dict[str, object]
) -> None:
    monkeypatch.setenv("CLIPPER_DEV", "1")

    result = runner.invoke(cli_app, ["run-review", "--insecure-cookies", "--host", "0.0.0.0"])  # noqa: S104

    assert result.exit_code == 0, result.output


# --- M1: session lifetime + password fingerprint ----------------------------------------


def test_session_expires_after_absolute_lifetime(client: TestClient, wall: FakeClock) -> None:
    login(client)
    wall.now += SESSION_MAX_AGE - 60
    assert client.get("/").status_code == 200  # still valid; this re-signs the cookie

    wall.now += 120  # now past iat + 7 days despite the recent re-sign

    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_session_with_future_iat_rejected(client: TestClient, wall: FakeClock) -> None:
    login(client)
    wall.now -= 3600  # clock went backwards an hour: iat is in the future

    assert client.get("/").status_code == 303


def test_changing_password_hash_invalidates_sessions(
    make_app: Callable[..., FastAPI], tmp_path: Path
) -> None:
    first = TestClient(make_app(), base_url=BASE_URL, follow_redirects=False)
    login(first)
    assert first.get("/").status_code == 200

    other_hash = bcrypt.hashpw(b"new password", bcrypt.gensalt(rounds=4)).decode()
    second = TestClient(
        make_app(password_hash=other_hash),
        base_url=BASE_URL,
        follow_redirects=False,
        cookies=first.cookies,
    )

    assert second.get("/").status_code == 303


def test_same_hash_and_secret_keep_session_across_restart(
    make_app: Callable[..., FastAPI],
) -> None:
    first = TestClient(make_app(), base_url=BASE_URL, follow_redirects=False)
    login(first)

    restarted = TestClient(
        make_app(), base_url=BASE_URL, follow_redirects=False, cookies=first.cookies
    )

    assert restarted.get("/").status_code == 200


def test_require_user_checks_lifetime_and_fingerprint_itself(
    make_app: Callable[..., FastAPI], wall: FakeClock
) -> None:
    """require_user enforces the same policy as the gate, independent of it."""
    from clipper.review.app import LoginRequired

    application = make_app()
    policy = application.state.session_policy

    def request_with(session: dict[str, object]) -> Request:
        return Request({"type": "http", "session": session, "app": application})

    good = {"user": "reviewer", "iat": wall.now, "fp": policy.fingerprint}
    assert require_user(request_with(dict(good))) == "reviewer"
    for bad in (
        {**good, "iat": wall.now - SESSION_MAX_AGE - 1},
        {**good, "fp": "0" * 16},
        {**good, "iat": "yesterday"},
        {"user": "reviewer"},
    ):
        with pytest.raises(LoginRequired):
            require_user(request_with(dict(bad)))


# --- M2: per-IP throttle + global backstop ------------------------------------------------


def ip_client(application: FastAPI, ip: str) -> TestClient:
    return TestClient(application, base_url=BASE_URL, follow_redirects=False, client=(ip, 50000))


def test_throttle_is_per_client_ip(make_app: Callable[..., FastAPI]) -> None:
    application = make_app()
    attacker = ip_client(application, "203.0.113.9")
    reviewer = ip_client(application, "198.51.100.7")
    for _ in range(LOGIN_MAX_ATTEMPTS):
        login(attacker, password="wrong")

    assert login(attacker, password="wrong").status_code == 429
    assert login(reviewer).status_code == 303


def test_successful_login_resets_ip_bucket(make_app: Callable[..., FastAPI]) -> None:
    application = make_app()
    client = ip_client(application, "198.51.100.7")
    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        login(client, password="wrong")
    assert login(client).status_code == 303
    client.cookies.clear()

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert login(client, password="wrong").status_code == 401


def test_global_backstop_throttles_distributed_attempts(
    make_app: Callable[..., FastAPI],
) -> None:
    assert LOGIN_GLOBAL_MAX_ATTEMPTS == 30
    application = make_app()
    for i in range(LOGIN_GLOBAL_MAX_ATTEMPTS):
        assert login(ip_client(application, f"203.0.113.{i}"), password="x").status_code == 401

    response = login(ip_client(application, "198.51.100.200"))

    assert response.status_code == 429


def test_login_throttle_is_thread_safe() -> None:
    import threading

    throttle = LoginThrottle(clock=lambda: 0.0, max_attempts=5, global_max_attempts=1000)
    allowed: list[bool] = []
    barrier = threading.Barrier(50)

    def attempt() -> None:
        barrier.wait()
        allowed.append(throttle.acquire("10.0.0.1") is None)

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert allowed.count(True) == 5


def test_throttle_forgets_idle_ips() -> None:
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0], max_attempts=5, global_max_attempts=10**6)
    for i in range(2000):
        throttle.acquire(f"10.0.{i // 256}.{i % 256}")
    now[0] += 61
    throttle.acquire("10.9.9.9")

    assert throttle.tracked_keys() <= 1


# --- L1: next param for HTMX / non-GET ------------------------------------------------------


def test_htmx_next_uses_current_url_path(client: TestClient) -> None:
    response = client.get(
        "/fragments/queue",
        headers={"HX-Request": "true", "HX-Current-URL": "https://testserver/clips/4?tab=a"},
    )

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=%2Fclips%2F4%3Ftab%3Da"


def test_htmx_next_from_hostile_current_url_is_sanitized(client: TestClient) -> None:
    response = client.get(
        "/fragments/queue",
        headers={"HX-Request": "true", "HX-Current-URL": "https://evil.example//evil.example"},
    )

    assert response.headers["HX-Redirect"] == "/login?next=%2F"


def test_non_get_redirect_uses_root_next(client: TestClient) -> None:
    response = client.post("/clips/1/approve")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


# --- L2/L3: headers -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/healthz", "/"])
def test_dynamic_responses_not_cached(client: TestClient, path: str) -> None:
    assert client.get(path).headers["Cache-Control"] == "no-store"


def test_static_responses_cacheable(client: TestClient) -> None:
    assert "no-store" not in client.get("/static/app.css").headers.get("Cache-Control", "")


def test_hsts_and_permissions_policy_when_secure(client: TestClient) -> None:
    headers = client.get("/healthz").headers

    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "camera=()" in headers["Permissions-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"]


def test_no_hsts_when_insecure(make_app: Callable[..., FastAPI]) -> None:
    client = TestClient(make_app(secure_cookies=False))

    assert "Strict-Transport-Security" not in client.get("/healthz").headers


def test_security_headers_on_500(make_app: Callable[..., FastAPI]) -> None:
    application = make_app()

    @application.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    client = TestClient(application, base_url=BASE_URL, raise_server_exceptions=False)
    login(client)
    response = client.get("/boom")

    assert response.status_code == 500
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_htmx_config_disables_eval_and_script_tags(client: TestClient) -> None:
    html = client.get("/login").text

    assert '"allowEval": false' in html
    assert '"allowScriptTags": false' in html


# --- L7: cookie name ------------------------------------------------------------------------


def test_cookie_name_uses_host_prefix_when_secure(client: TestClient) -> None:
    response = client.get("/login")

    assert response.headers["set-cookie"].startswith("__Host-clipper_session=")
    assert "path=/" in response.headers["set-cookie"]
    assert "domain=" not in response.headers["set-cookie"].lower()


def test_cookie_name_plain_when_insecure(make_app: Callable[..., FastAPI]) -> None:
    client = TestClient(make_app(secure_cookies=False))

    assert client.get("/login").headers["set-cookie"].startswith("clipper_session=")


# --- L10: flashes ---------------------------------------------------------------------------


def test_logout_flash_shown_once_on_login_page(client: TestClient) -> None:
    login(client)
    client.post("/logout", data={"csrf_token": logged_in_csrf(client)})

    first = client.get("/login").text
    second = client.get("/login").text

    assert "Signed out" in first
    assert "Signed out" not in second


def test_partials_do_not_consume_flashes(make_app: Callable[..., FastAPI]) -> None:
    from clipper.review.app import flash

    application = make_app()

    @application.post("/note")
    def note(request: Request, _user: str = Depends(require_user)) -> dict[str, bool]:
        flash(request, "Saved")
        return {"ok": True}

    @application.get("/partial", response_class=HTMLResponse)
    def partial(request: Request, _user: str = Depends(require_user)) -> HTMLResponse:
        return render(request, "_queue_empty.html")

    client = TestClient(application, base_url=BASE_URL, follow_redirects=False)
    login(client)
    token = logged_in_csrf(client)
    client.post("/note", headers={"X-CSRF-Token": token})

    assert "Saved" not in client.get("/partial").text
    assert "Saved" in client.get("/").text


# --- L11: CSRF failure on HTMX --------------------------------------------------------------


def test_htmx_csrf_failure_requests_refresh(client: TestClient) -> None:
    login(client)

    response = client.post("/logout", headers={"HX-Request": "true", "X-CSRF-Token": "bad"})

    assert response.status_code == 403
    assert response.headers["HX-Refresh"] == "true"


def test_plain_csrf_failure_has_no_hx_refresh(client: TestClient) -> None:
    login(client)

    response = client.post("/logout", data={"csrf_token": "bad"})

    assert response.status_code == 403
    assert "HX-Refresh" not in response.headers


# --- shared helpers for wave-2 routes -----------------------------------------------------


def test_get_db_yields_connection_and_closes_it(
    make_app: Callable[..., FastAPI], tmp_path: Path
) -> None:
    application = make_app(db_path=tmp_path / "fresh.db")
    seen: list[sqlite3.Connection] = []

    @application.get("/dbcheck")
    def dbcheck(
        conn: Annotated[sqlite3.Connection, Depends(get_db)],
        _user: Annotated[str, Depends(require_user)],
    ) -> dict[str, object]:
        seen.append(conn)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {"tables": [t["name"] for t in tables]}

    client = TestClient(application, base_url=BASE_URL, follow_redirects=False)
    login(client)
    response = client.get("/dbcheck")

    assert response.status_code == 200
    assert response.json() == {"tables": []}  # connects only; never migrates
    assert (tmp_path / "fresh.db").exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")


def test_render_passes_status_and_headers(make_app: Callable[..., FastAPI]) -> None:
    application = make_app()

    @application.get("/teapot", response_class=HTMLResponse)
    def teapot(request: Request, _user: Annotated[str, Depends(require_user)]) -> HTMLResponse:
        return render(
            request, "_queue_empty.html", {"x": 1}, status_code=418, headers={"X-Test": "1"}
        )

    client = TestClient(application, base_url=BASE_URL, follow_redirects=False)
    login(client)
    response = client.get("/teapot")

    assert response.status_code == 418
    assert response.headers["X-Test"] == "1"
