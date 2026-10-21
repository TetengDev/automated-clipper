"""Review app shell: single-user login, signed-cookie sessions, CSRF, login throttling.

Security model (single reviewer, behind Caddy/HTTPS in production):
- Sessions are Starlette signed cookies (itsdangerous): HttpOnly, SameSite=Lax, Secure,
  named `__Host-clipper_session` when Secure. A session records when it was issued (`iat`,
  wall clock) and a fingerprint of the password hash; it dies 7 days after login no matter
  how often it is re-signed, and changing REVIEW_PASSWORD_HASH logs every session out.
- Every request except /login, /healthz and /static/* needs a valid session (`_AuthGate`);
  routes should still declare `Depends(require_user)` so the requirement is explicit.
- Every unsafe request (POST/PUT/PATCH/DELETE) must carry the session's CSRF token, as a
  `csrf_token` form field or an `X-CSRF-Token` header (`csrf_protect`, applied app-wide).
- Login attempts are throttled per client IP (the 6th failure inside a sliding minute gets
  429) with a global backstop of 30 attempts a minute. The client IP is only trustworthy
  when uvicorn trusts the proxy's X-Forwarded-For (`run-review --forwarded-allow-ips`).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import bcrypt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clipper.config import MIN_SESSION_SECRET_LENGTH
from clipper.db import connect
from clipper.export import DEFAULT_OUTPUT_DIR, ExportLocks, Runner, run_subprocess
from clipper.jobs.worker import DEFAULT_MEDIA_ROOT

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

REVIEWER = "reviewer"  # single-user app: the only identity a session can hold
SESSION_COOKIE = "clipper_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # absolute lifetime from login, in seconds
SESSION_CLOCK_SKEW = 60  # tolerate an `iat` this many seconds in the future
LOGIN_MAX_ATTEMPTS = 5  # failures per client IP per window
LOGIN_GLOBAL_MAX_ATTEMPTS = 30  # attempts across all IPs per window
LOGIN_WINDOW_SECONDS = 60.0
BCRYPT_MAX_BYTES = 72
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_PATHS = frozenset({"/login", "/healthz"})
_BCRYPT_HASH = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")
_UNSAFE_NEXT_CHARS = re.compile(r"[\x00-\x20\x7f\\]")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
HSTS_VALUE = "max-age=31536000"

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=TEMPLATES_DIR)


class LoginRequired(Exception):
    """Raised by `require_user`; turned into a redirect to /login (or HX-Redirect)."""


def safe_next_path(value: str | None) -> str:
    """Return `value` if it is a same-site relative path, else "/". Prevents open redirects.

    Rejects scheme/host forms (`https://x`, `//x`), backslashes (browsers treat `/\\x` as
    `//x`), whitespace/control characters (stripped by browsers), and the login page itself.
    """
    if not value or len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        return "/"
    if _UNSAFE_NEXT_CHARS.search(value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path == "/login":
        return "/"
    return value


def _path_and_query(path: str, query: str) -> str:
    return f"{path}?{query}" if query else path


def _next_target(scope: Scope) -> str:
    """Where to send the user after login, for a request that needs a session.

    HTMX requests target fragments, so use the page they came from (`HX-Current-URL`);
    other non-GET requests cannot be replayed by a redirect, so go home.
    """
    headers = Headers(scope=scope)
    if headers.get("hx-request") == "true":
        current = urlsplit(headers.get("hx-current-url", ""))
        return safe_next_path(_path_and_query(current.path, current.query))
    if scope.get("method", "GET") not in ("GET", "HEAD"):
        return "/"
    query = scope.get("query_string", b"").decode("latin-1")
    return safe_next_path(_path_and_query(scope.get("path", "/"), query))


def _login_redirect(scope: Scope) -> Response:
    location = f"/login?next={quote(_next_target(scope), safe='')}"
    if Headers(scope=scope).get("hx-request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


@dataclass(frozen=True)
class SessionPolicy:
    """Decides whether a session is logged in; also starts sessions at login."""

    fingerprint: str
    wall_clock: Callable[[], float]
    max_age: int = SESSION_MAX_AGE

    @classmethod
    def build(
        cls, *, session_secret: str, password_hash: str, wall_clock: Callable[[], float]
    ) -> SessionPolicy:
        digest = hmac.new(session_secret.encode(), password_hash.encode(), hashlib.sha256)
        return cls(fingerprint=digest.hexdigest()[:16], wall_clock=wall_clock)

    def is_valid(self, session: MutableMapping[str, Any]) -> bool:
        if session.get("user") != REVIEWER:
            return False
        fp, iat = session.get("fp"), session.get("iat")
        if not isinstance(fp, str) or not hmac.compare_digest(fp, self.fingerprint):
            return False
        if isinstance(iat, bool) or not isinstance(iat, int | float):
            return False
        age = self.wall_clock() - iat
        return -SESSION_CLOCK_SKEW <= age < self.max_age

    def start(self, session: MutableMapping[str, Any]) -> None:
        """Fresh session: drops pre-login state (fixation) and rotates the CSRF token."""
        session.clear()
        session.update(user=REVIEWER, iat=int(self.wall_clock()), fp=self.fingerprint)
        session["csrf"] = secrets.token_urlsafe(32)


class _AuthGate:
    """ASGI middleware (inside SessionMiddleware): no valid session -> redirect to /login.

    Covers every path, including unknown ones, so a route that forgets `require_user`
    still cannot leak content to anonymous visitors. Invalid sessions are cleared.
    """

    def __init__(self, app: ASGIApp, policy: SessionPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public(scope["path"]):
            await self.app(scope, receive, send)
            return
        session = scope["session"]
        if not self.policy.is_valid(session):
            if "user" in session:
                session.clear()
            await _login_redirect(scope)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _SecurityHeaders:
    """Outermost ASGI wrapper: security headers on every response, 500s included."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope.get("path", "").startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
                if not is_static:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _ReviewApp(FastAPI):
    """FastAPI whose middleware stack is wrapped in `_SecurityHeaders`, outside Starlette's
    ServerErrorMiddleware, so even unhandled-exception 500 pages get the headers."""

    hsts: bool = True

    def build_middleware_stack(self) -> ASGIApp:
        return _SecurityHeaders(super().build_middleware_stack(), hsts=self.hsts)


class LoginThrottle:
    """Thread-safe sliding-window login limiter.

    `acquire(key)` atomically checks and records an attempt: at most `max_attempts` per key
    and `global_max_attempts` overall inside `window` seconds. `reset(key)` forgets a key's
    attempts (called after a successful login, so only failures accumulate per IP).
    """

    def __init__(
        self,
        clock: Callable[[], float],
        max_attempts: int = LOGIN_MAX_ATTEMPTS,
        global_max_attempts: int = LOGIN_GLOBAL_MAX_ATTEMPTS,
        window: float = LOGIN_WINDOW_SECONDS,
    ) -> None:
        self._clock = clock
        self._max = max_attempts
        self._global_max = global_max_attempts
        self._window = window
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._last_sweep = clock()

    def _prune(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()

    def _wait(self, bucket: deque[float], now: float) -> int:
        return max(1, int(self._window - (now - bucket[0]) + 0.999))

    def acquire(self, key: str) -> int | None:
        """Record an attempt and return None, or return seconds to wait (not recorded)."""
        with self._lock:
            now = self._clock()
            if now - self._last_sweep >= self._window:
                for stale in [k for k, b in self._buckets.items() if now - b[-1] >= self._window]:
                    del self._buckets[stale]
                self._last_sweep = now
            bucket = self._buckets.setdefault(key, deque())
            self._prune(bucket, now)
            self._prune(self._global, now)
            waits = []
            if len(bucket) >= self._max:
                waits.append(self._wait(bucket, now))
            if len(self._global) >= self._global_max:
                waits.append(self._wait(self._global, now))
            if waits:
                if not bucket:
                    del self._buckets[key]
                return max(waits)
            bucket.append(now)
            self._global.append(now)
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)


def get_csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use. Templates call `csrf_token()`."""
    token = request.session.get("csrf")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def require_user(request: Request) -> str:
    """FastAPI dependency: the logged-in reviewer's name, else redirect to /login.

    Usage in later routes (CLP-19..21)::

        @app.get("/clips/{clip_id}")
        def clip_detail(clip_id: int, user: Annotated[str, Depends(require_user)]): ...

    Applies the full session policy (absolute 7-day lifetime, password fingerprint).
    Anonymous/stale requests get 303 -> /login?next=<path> (401 + `HX-Redirect` for HTMX).
    The app-wide auth gate already blocks them; declaring this dependency keeps the
    requirement explicit and survives the gate being reconfigured.
    """
    policy: SessionPolicy = request.app.state.session_policy
    if not policy.is_valid(request.session):
        raise LoginRequired
    return REVIEWER


async def csrf_protect(request: Request) -> None:
    """FastAPI dependency, installed app-wide: unsafe methods need the session CSRF token.

    Routes need nothing extra; any POST/PUT/PATCH/DELETE without a matching token is 403
    (with `HX-Refresh: true` for HTMX, so the page reloads and picks up a fresh token).
    Send the token either as a form field (`<input type="hidden" name="csrf_token"
    value="{{ csrf_token() }}">`) or as the `X-CSRF-Token` header — base.html sets
    `hx-headers` on <body>, so every HTMX request carries it automatically.
    """
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            supplied = value if isinstance(value, str) else None
    if (
        not isinstance(expected, str)
        or not expected
        or not supplied
        or not hmac.compare_digest(expected.encode(), supplied.encode())
    ):
        htmx = request.headers.get("HX-Request") == "true"
        raise HTTPException(
            status_code=403,
            detail="CSRF token missing or invalid",
            headers={"HX-Refresh": "true"} if htmx else None,
        )


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a connection to the app's DB, closed after the request.

    Never migrates (run `clipper migrate` first). Usage in later routes (CLP-19..21)::

        def clips(conn: Annotated[sqlite3.Connection, Depends(get_db)], ...): ...
    """
    conn = connect(request.app.state.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def render(
    request: Request,
    name: str,
    context: Mapping[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """Render a template with `csrf_token()`, `user` and `get_flashed_messages()` available."""
    policy: SessionPolicy = request.app.state.session_policy
    ctx = {
        "csrf_token": lambda: get_csrf_token(request),
        "user": REVIEWER if policy.is_valid(request.session) else None,
        "get_flashed_messages": lambda: _pop_flashes(request),
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


def flash(request: Request, message: str) -> None:
    """Queue a one-time message; the next full-page render (base.html) shows and clears it.
    Partials don't call `get_flashed_messages()`, so HTMX swaps never swallow flashes."""
    # Reassign (not .append): the session only re-saves on top-level writes.
    request.session["flash"] = [*request.session.get("flash", []), message]


def _pop_flashes(request: Request) -> list[str]:
    return [str(m) for m in request.session.pop("flash", None) or []]


def _password_ok(password: str, password_hash: bytes) -> bool:
    candidate = password.encode("utf-8")
    if not candidate or len(candidate) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(candidate, password_hash)
    except ValueError:
        return False


def create_app(
    *,
    password_hash: str,
    session_secret: str,
    db_path: Path,
    secure_cookies: bool = True,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    media_root: Path = DEFAULT_MEDIA_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    rclone_remote: str | None = None,
    export_runner: Runner = run_subprocess,
) -> FastAPI:
    """Build the review app. All config is passed in so tests need no environment.

    `db_path` is stored on `app.state.db_path`; routes get a connection via `get_db`.
    `media_root` confines which rendered files /media may serve; approved clips are
    exported under `output_dir`/ready (and copied to `rclone_remote` via `export_runner`).
    `clock` (monotonic seconds) drives the login throttle; `wall_clock` (epoch seconds)
    stamps and ages sessions. Routes render templates with the module-level `render`.
    """
    if not _BCRYPT_HASH.match(password_hash):
        raise ValueError("REVIEW_PASSWORD_HASH must be a bcrypt hash ($2b$...)")
    if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
        raise ValueError(f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters")
    hash_bytes = password_hash.encode("ascii")
    throttle = LoginThrottle(clock)
    policy = SessionPolicy.build(
        session_secret=session_secret, password_hash=password_hash, wall_clock=wall_clock
    )

    app = _ReviewApp(
        title="clipper review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    app.hsts = secure_cookies
    app.state.db_path = db_path
    app.state.media_root = media_root
    app.state.output_dir = output_dir
    app.state.rclone_remote = rclone_remote
    app.state.export_runner = export_runner
    app.state.export_locks = ExportLocks()
    app.state.wall_clock = wall_clock
    app.state.login_throttle = throttle
    app.state.session_policy = policy

    # Middleware: last added is outermost -> [security headers](session(gate(app))).
    app.add_middleware(_AuthGate, policy=policy)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=f"__Host-{SESSION_COOKIE}" if secure_cookies else SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=secure_cookies,
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        return _login_redirect(request.scope)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str | None = None) -> Response:
        if policy.is_valid(request.session):
            return RedirectResponse(safe_next_path(next), status_code=303)
        return render(request, "login.html", {"next": safe_next_path(next), "error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        password: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "/",
    ) -> Response:
        target = safe_next_path(next)
        client_ip = request.client.host if request.client else "unknown"
        retry_after = throttle.acquire(client_ip)
        if retry_after is not None:
            logger.warning("login throttled for %s (retry after %ss)", client_ip, retry_after)
            return render(
                request,
                "login.html",
                {"next": target, "error": "Too many attempts. Wait a minute and try again."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if not _password_ok(password, hash_bytes):
            logger.warning("login failed from %s", client_ip)
            return render(
                request,
                "login.html",
                {"next": target, "error": "Incorrect password."},
                status_code=401,
            )
        throttle.reset(client_ip)
        policy.start(request.session)
        return RedirectResponse(target, status_code=303)

    @app.post("/logout")
    def logout(request: Request, _user: Annotated[str, Depends(require_user)]) -> Response:
        request.session.clear()
        flash(request, "Signed out.")
        return RedirectResponse("/login", status_code=303)

    from clipper.review.routes import router  # imports this module's helpers

    app.include_router(router)
    return app
