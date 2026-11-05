"""Meta Graph API plumbing for the Facebook and Instagram publishers (CLP-94).

* Credentials come from the CLP-91 account secrets: `CLIPPER_ACCESS_TOKEN_<ID>` (a Page
  access token) and `CLIPPER_META_ID_<ID>` (the numeric Page id, or the Instagram
  professional account's user id). They are held as `SecretStr` and never logged.
* Tokens travel only in the `Authorization: OAuth ...` header, never in a URL, so no
  request log can carry one. The one exception is `/debug_token`, whose `input_token`
  must be a query parameter; httpx's request logging is off in the CLI, and any error
  text is redacted.
* Video bytes go only to `https://rupload.facebook.com`. An upload URL from a response is
  checked against that host before the token is sent to it.
* `TokenGuard` checks each account's token with `/debug_token` (cached for
  `TOKEN_CHECK_EVERY`), alerts `TOKEN_WARN_BEFORE` before it expires, and pauses the
  account (`PublishDeferred`, one alert a day) while the token is invalid, expired or
  missing. Posts stay `scheduled` meanwhile and need no requeue once the token is fixed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from clipper.accounts import account_tokens, token_env_name
from clipper.jobs.models import Clock, utcnow
from clipper.posting.models import PublishDeferred, PublishRejected
from clipper.posting.stage import log_alert
from clipper.redact import redact

if TYPE_CHECKING:
    from clipper.accounts import Account
    from clipper.posting.models import Publisher

GRAPH_VERSION = "v26.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
UPLOAD_HOST = "rupload.facebook.com"
OBJECT_ID_KIND = "meta_id"
"""`CLIPPER_META_ID_<ACCOUNT_ID>`: the Page id (facebook) or IG user id (instagram)."""

TOKEN_CHECK_EVERY = timedelta(hours=6)
TOKEN_WARN_BEFORE = timedelta(days=7)
ALERT_EVERY = timedelta(hours=24)
DEFER_TOKEN = timedelta(hours=1)
DEFER_RATE = timedelta(hours=1)
TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=60.0, write=60.0)
"""Per request step (connect, each read or write); `PUBLISH_DEADLINE` bounds a publish."""
PUBLISH_DEADLINE = timedelta(minutes=15)
"""A publish gives up between steps after this, well within the stage's STALE_UPLOAD."""
MAX_UPLOAD_BYTES = 1024**3
"""1 GiB, the Reels file size limit."""
CHECK_RETRY = timedelta(minutes=5)
"""After a token check that failed for another reason (Meta down), wait this long."""

_TOKEN_CODES = frozenset({102, 190})
_ACCOUNT_CODES = frozenset({10, 368})
"""Permission denied (10, and 200-299) and policy blocks (368): the owner must act."""
_RATE_CODES = frozenset({4, 9, 17, 32, 613, 80001, 80002})
_TRANSIENT_CODES = frozenset({1, 2})
_ID = re.compile(r"[0-9]{1,30}")
_PATH = re.compile(r"[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")
_MESSAGE_MAX = 300

logger = logging.getLogger(__name__)

Alert = Callable[[str], None]


class MetaError(Exception):
    """A Graph API call failed. The message is Meta's (redacted, cut), never a token."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode
        self.transient = transient

    @property
    def token_problem(self) -> bool:
        """The token is invalid, expired or revoked (OAuthException 190 and friends)."""
        return self.code in _TOKEN_CODES

    @property
    def account_problem(self) -> bool:
        """A permission is missing or the account is blocked: retrying will not help."""
        code = self.code
        return code is not None and (code in _ACCOUNT_CODES or 200 <= code <= 299)

    @property
    def rate_limited(self) -> bool:
        """An app, Page or account rate limit, or Instagram's publishing limit."""
        return self.code in _RATE_CODES


class MissingCredentials(Exception):
    """An account's Meta secrets are missing or malformed; names variables, never values."""

    def __init__(self, message: str, missing: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.missing = list(missing)


@dataclass(frozen=True)
class MetaCredentials:
    account_id: str
    object_id: str
    token: SecretStr


def load_credentials(account_id: str, env: Mapping[str, str] | None = None) -> MetaCredentials:
    """The account's Page token and Page / IG user id from the environment (or `.env`)."""
    tokens = account_tokens(account_id, env)
    object_id = tokens.extra.get(OBJECT_ID_KIND)
    missing = [
        token_env_name(account_id, kind)
        for kind, value in (("access_token", tokens.access_token), (OBJECT_ID_KIND, object_id))
        if value is None
    ]
    if missing:
        raise MissingCredentials(f"account {account_id}: set {', '.join(missing)}", missing)
    assert tokens.access_token is not None and object_id is not None
    oid = object_id.get_secret_value().strip()
    if not _ID.fullmatch(oid):
        name = token_env_name(account_id, OBJECT_ID_KIND)
        raise MissingCredentials(f"account {account_id}: {name} must be the numeric id")
    return MetaCredentials(account_id=account_id, object_id=oid, token=tokens.access_token)


def graph_id(value: object, what: str) -> str:
    """An id from a Graph response, checked before it is put in a URL path."""
    text = str(value) if isinstance(value, str | int) and not isinstance(value, bool) else ""
    if not _ID.fullmatch(text):
        raise MetaError(f"unexpected {what} from Meta")
    return text


def safe_link(value: object, hosts: frozenset[str]) -> str | None:
    """A post link from a Graph response, rebuilt as `https://<host><path>?<query>` when
    its host is exactly one of `hosts`; None for anything else (another host, userinfo,
    a port, a backslash or control character, which browsers may read differently)."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        return None
    if "\\" in value or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        return None
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    if port is not None or parts.hostname not in hosts or parts.netloc != parts.hostname:
        return None
    return urlunsplit(("https", parts.hostname, parts.path or "/", parts.query, ""))


def form_bool(value: bool) -> str:
    """A Graph API form boolean."""
    return "true" if value else "false"


class GraphClient:
    """A thin Graph API client: JSON in, dict out, `MetaError` on any failure."""

    def __init__(self, http: httpx.Client, base_url: str = GRAPH_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.owned = False
        """Whether `close()` closes `http` (a client the publishers created themselves)."""
        # httpx logs request URLs at INFO, and /debug_token carries the token in its query.
        for name in ("httpx", "httpcore"):
            logger_ = logging.getLogger(name)
            if logger_.getEffectiveLevel() < logging.WARNING:
                logger_.setLevel(logging.WARNING)

    def get(
        self, path: str, token: SecretStr, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        return self._send("GET", self._url(path), token, params=dict(params or {}))

    def post(
        self, path: str, token: SecretStr, data: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        return self._send("POST", self._url(path), token, data=dict(data or {}))

    def upload(self, url: str, token: SecretStr, video: Path) -> dict[str, Any]:
        """Send the whole file to a rupload.facebook.com URL in one request (offset 0)."""
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != UPLOAD_HOST
            or parts.port is not None
            or parts.username is not None
        ):
            raise MetaError("unexpected upload URL from Meta (not https://rupload.facebook.com)")
        size = video.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise PublishRejected(f"the video is {size} bytes, over Meta's 1 GiB limit")
        content = video.read_bytes()
        headers = {"offset": "0", "file_size": str(len(content))}
        return self._send("POST", url, token, content=content, headers=headers)

    def close(self) -> None:
        if self.owned:
            self.http.close()

    def _url(self, path: str) -> str:
        if not _PATH.fullmatch(path):
            raise ValueError(f"not a Graph API path: {path!r}")
        return f"{self.base_url}/{path}"

    def _send(
        self,
        method: str,
        url: str,
        token: SecretStr,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        auth = {"Authorization": f"OAuth {token.get_secret_value()}", **(headers or {})}
        try:
            response = self.http.request(method, url, headers=auth, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise MetaError(f"{type(exc).__name__} talking to Meta", transient=True) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            raise _graph_error(response.status_code, body["error"], token)
        if response.status_code >= 400 or not isinstance(body, dict):
            status = response.status_code
            raise MetaError(
                f"HTTP {status} from Meta", status=status, transient=status >= 500 or status == 429
            )
        return body


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _graph_error(status: int, error: Mapping[str, Any], token: SecretStr) -> MetaError:
    code, subcode = _int(error.get("code")), _int(error.get("error_subcode"))
    text = str(error.get("message") or "Graph API error")
    secret = token.get_secret_value()
    if secret:
        text = text.replace(secret, "REDACTED")
    text = redact(text)[:_MESSAGE_MAX]
    detail = f"Meta error {code}" + (f"/{subcode}" if subcode is not None else "")
    transient = error.get("is_transient") is True or code in _TRANSIENT_CODES or status >= 500
    return MetaError(
        f"{detail}: {text}", status=status, code=code, subcode=subcode, transient=transient
    )


# --- token health ------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenHealth:
    valid: bool
    expires_at: datetime | None = None
    """None: the token never expires (a Page token from a long-lived user token)."""
    data_access_expires_at: datetime | None = None
    """When the app loses data access unless the owner logs in again (90 days)."""

    @property
    def expiry(self) -> datetime | None:
        """The earlier of the two expiries, or None when neither applies."""
        moments = [m for m in (self.expires_at, self.data_access_expires_at) if m is not None]
        return min(moments) if moments else None


def _moment(value: object) -> datetime | None:
    seconds = _int(value)
    return datetime.fromtimestamp(seconds, UTC) if seconds else None


def check_token(client: GraphClient, token: SecretStr) -> TokenHealth:
    """`/debug_token` for the token itself (a token may inspect itself)."""
    body = client.get("debug_token", token, params={"input_token": token.get_secret_value()})
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return TokenHealth(
        valid=data.get("is_valid") is True,
        expires_at=_moment(data.get("expires_at")),
        data_access_expires_at=_moment(data.get("data_access_expires_at")),
    )


def _fingerprint(token: SecretStr) -> str:
    """Tells tokens apart in memory, so a replaced token is checked at once. Never logged."""
    return hashlib.sha256(token.get_secret_value().encode()).hexdigest()[:16]


@dataclass
class TokenGuard:
    """Per-account token checks shared by the Meta publishers (one per worker)."""

    client: GraphClient
    clock: Clock = utcnow
    alert: Alert = log_alert
    _checked: dict[tuple[str, str], tuple[datetime, TokenHealth]] = field(
        default_factory=dict, init=False, repr=False
    )
    _alerted: dict[tuple[str, str], datetime] = field(default_factory=dict, init=False, repr=False)
    _check_failed: dict[tuple[str, str], datetime] = field(
        default_factory=dict, init=False, repr=False
    )
    _rate_paused: dict[str, PublishDeferred] = field(default_factory=dict, init=False, repr=False)

    def ensure(self, creds: MetaCredentials) -> None:
        """Return when the token may publish; raise `PublishDeferred` when it may not.
        A failed check (Meta down) does not block: the publish call itself will tell."""
        key = (creds.account_id, _fingerprint(creds.token))
        now = self.clock()
        cached = self._checked.get(key)
        paused = self._rate_paused.get(creds.account_id)
        if paused is not None:
            if now < paused.until:
                raise PublishDeferred(paused.reason, until=paused.until)
            del self._rate_paused[creds.account_id]
        if cached is None or now - cached[0] >= TOKEN_CHECK_EVERY:
            failed = self._check_failed.get(key)
            if failed is not None and now - failed < CHECK_RETRY:
                return
            try:
                health = check_token(self.client, creds.token)
            except MetaError as exc:
                if not exc.token_problem:
                    self._check_failed[key] = now
                    logger.warning(
                        "account %s: Meta token check failed (%s); publishing anyway",
                        creds.account_id,
                        exc,
                    )
                    return
                health = TokenHealth(valid=False)
            self._check_failed.pop(key, None)
            cached = (now, health)
            self._checked[key] = cached
        health = cached[1]
        expiry = health.expiry
        if not health.valid:
            raise self.unusable(creds.account_id, creds.token, "the token is invalid or revoked")
        if expiry is not None and expiry <= now:
            raise self.unusable(
                creds.account_id, creds.token, f"the token expired on {_when(expiry)}"
            )
        if expiry is not None and expiry - now <= TOKEN_WARN_BEFORE:
            self._notify(
                creds.account_id,
                "expiring",
                f"Meta token for account {creds.account_id} expires on {_when(expiry)};"
                " replace it before then (docs/DEPLOYMENT.md, Meta publishing).",
            )

    def unusable(self, account_id: str, token: SecretStr, reason: str) -> PublishDeferred:
        """Mark the token unusable, alert (once a day) and return the deferral to raise."""
        now = self.clock()
        self._checked[(account_id, _fingerprint(token))] = (now, TokenHealth(valid=False))
        return self._pause(account_id, f"Meta token cannot publish: {reason}", now)

    def rate_limited(self, account_id: str, reason: str, until: datetime) -> PublishDeferred:
        """Hold every post of the account until `until`, so the next due post does not hit
        the limit again in the same pass."""
        deferred = PublishDeferred(redact(reason), until=until)
        self._rate_paused[account_id] = deferred
        return deferred

    def missing(self, account_id: str, problem: str) -> PublishDeferred:
        return self._pause(account_id, problem, self.clock())

    def _pause(self, account_id: str, problem: str, now: datetime) -> PublishDeferred:
        problem = redact(problem).rstrip(".")
        self._notify(
            account_id,
            "paused",
            f"account {account_id}: {problem}. Publishing to this account is paused until"
            " the token is replaced (docs/DEPLOYMENT.md, Meta publishing).",
        )
        return PublishDeferred(f"account {account_id}: {problem}", until=now + DEFER_TOKEN)

    def _notify(self, account_id: str, kind: str, message: str) -> None:
        now = self.clock()
        last = self._alerted.get((account_id, kind))
        if last is not None and now - last < ALERT_EVERY:
            return
        self._alerted[(account_id, kind)] = now
        try:
            self.alert(message)
        except Exception:
            logger.exception("account %s: alert failed", account_id)


def _when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def token_report(
    client: GraphClient, account_id: str, now: datetime, env: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    """(usable and not expiring within TOKEN_WARN_BEFORE, one line for the operator).
    Names and dates only, never a token value."""
    try:
        creds = load_credentials(account_id, env)
    except MissingCredentials as exc:
        return False, f"missing {', '.join(exc.missing)}" if exc.missing else str(exc)
    try:
        health = check_token(client, creds.token)
    except MetaError as exc:
        if exc.token_problem:
            return False, f"invalid ({exc})"
        return False, f"check failed ({exc})"
    expiry = health.expiry
    if not health.valid:
        return False, "invalid or revoked; replace the token"
    if expiry is not None and expiry <= now:
        return False, f"expired on {_when(expiry)}; replace the token"
    parts = [
        "never expires" if health.expires_at is None else f"expires {_when(health.expires_at)}"
    ]
    if health.data_access_expires_at is not None:
        parts.append(f"data access until {_when(health.data_access_expires_at)}")
    line = "ok, " + "; ".join(parts)
    if expiry is not None and expiry - now <= TOKEN_WARN_BEFORE:
        return False, f"{line} -- expires soon, replace the token"
    return True, line


# --- publishers --------------------------------------------------------------------------

Credentials = Callable[[str], MetaCredentials]


class MetaPublisher:
    """What the Facebook and Instagram publishers share: credentials, the token guard,
    and turning token and rate-limit errors into deferrals."""

    platform: str = ""

    def __init__(
        self,
        client: GraphClient,
        guard: TokenGuard,
        *,
        credentials: Credentials = load_credentials,
        clock: Clock = utcnow,
        campaigns: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client
        self.guard = guard
        self._credentials = credentials
        self.clock = clock
        self.campaigns = dict(campaigns or {})
        """Account id -> campaign. Their posts are refused until CLP-97 wires the paid
        partnership disclosure into the post metadata (fail closed)."""

    def close(self) -> None:
        """Called by the worker on exit (CLP-93's registry contract)."""
        self.client.close()

    def _refuse_campaign(self, account_id: str) -> None:
        campaign = self.campaigns.get(account_id)
        if campaign is not None:
            raise PublishRejected(
                f"{self.platform}: account {account_id} posts for campaign {campaign}, and the"
                " paid partnership disclosure is not wired in yet (CLP-97); post it by hand"
            )

    def _deadline(self) -> datetime:
        return self.clock() + PUBLISH_DEADLINE

    def _in_time(self, deadline: datetime, step: str) -> None:
        if self.clock() >= deadline:
            raise MetaError(f"{self.platform}: gave up before {step} (publish deadline)")

    def _creds(self, account_id: str) -> MetaCredentials:
        """The account's credentials once the guard lets it publish."""
        try:
            creds = self._credentials(account_id)
        except MissingCredentials as exc:
            raise self.guard.missing(account_id, str(exc)) from None
        self.guard.ensure(creds)
        return creds

    def _translate(self, creds: MetaCredentials, exc: MetaError) -> Exception:
        if exc.token_problem or exc.account_problem:
            return self.guard.unusable(creds.account_id, creds.token, str(exc))
        if exc.rate_limited:
            return self.guard.rate_limited(
                creds.account_id,
                f"{self.platform} rate limit for account {creds.account_id}: {exc}",
                self.clock() + DEFER_RATE,
            )
        return exc

    def _raise(self, creds: MetaCredentials, exc: MetaError) -> None:
        translated = self._translate(creds, exc)
        if translated is exc:
            raise exc
        raise translated from exc


def meta_publishers(
    accounts: Iterable[Account],
    http: httpx.Client | None = None,
    *,
    clock: Clock = utcnow,
    alert: Alert = log_alert,
) -> dict[str, Publisher]:
    """The Facebook and Instagram publishers for the enabled accounts' platforms. Without
    `http` they share a client of their own, which their `close()` closes."""
    from clipper.publish.facebook import FacebookPublisher
    from clipper.publish.instagram import InstagramPublisher

    enabled = [a for a in accounts if a.enabled]
    platforms = {a.platform for a in enabled} & {"facebook", "instagram"}
    if not platforms:
        return {}
    campaigns = {a.id: a.campaign for a in enabled if a.campaign}
    owned = http is None
    client = GraphClient(httpx.Client(follow_redirects=False) if http is None else http)
    client.owned = owned
    guard = TokenGuard(client, clock=clock, alert=alert)
    publishers: dict[str, Publisher] = {}
    if "facebook" in platforms:
        publishers["facebook"] = FacebookPublisher(client, guard, clock=clock, campaigns=campaigns)
    if "instagram" in platforms:
        publishers["instagram"] = InstagramPublisher(
            client, guard, clock=clock, campaigns=campaigns
        )
    return publishers


__all__ = [
    "GRAPH_URL",
    "GraphClient",
    "MetaCredentials",
    "MetaError",
    "MetaPublisher",
    "MissingCredentials",
    "TokenGuard",
    "TokenHealth",
    "check_token",
    "form_bool",
    "graph_id",
    "safe_link",
    "load_credentials",
    "meta_publishers",
    "token_report",
]
