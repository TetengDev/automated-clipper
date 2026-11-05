"""YouTube Shorts publisher (CLP-93), on the YouTube Data API v3 over plain httpx.

* `publish` uploads with the resumable protocol (`videos.insert`, `uploadType=resumable`)
  as `private` with `status.publishAt` = the post's `scheduled_at`, so YouTube itself
  makes it public at that time. A `publishAt` in the past publishes right away (videos
  resource, `status.publishAt`), so a late retry still goes out. Vertical videos up to
  3 minutes are classified as Shorts by YouTube; no flag is needed.
* Quota: one `videos.insert` costs 1 unit of the separate "Video Uploads" bucket
  (100 calls a day). `quotaExceeded` and `uploadLimitExceeded` raise `PublishDeferred`
  to just after the next quota reset (midnight Pacific time), so they never count as an
  attempt.
* OAuth: an access token is minted from the account's refresh token
  (`CLIPPER_REFRESH_TOKEN_<ACCOUNT_ID>`) and the OAuth client in `Secrets`, cached until
  shortly before it expires, and refreshed once on a 401. A refresh token Google rejects
  (`invalid_grant`, e.g. revoked, or expired because the consent screen is in Testing)
  pauses that account: its posts are deferred, one alert goes out, and the pause lifts
  once a different refresh token is configured (a changed OAuth client in `Secrets`
  takes a worker restart). A post for an account that is not an enabled YouTube account
  is deferred too, with one alert, instead of failing.
* `lookup(idempotency_key)` finds an earlier upload by the `clipper-<key>` tag every
  upload carries, among each account's latest uploads, so the posting stage never posts a
  video twice after a timeout or a crash.

Tokens are held as `SecretStr` or not at all (only a hash identifies the credentials),
travel only in the Authorization header or the token request body, go only to Google's
fixed hosts, and never appear in errors, logs or repr.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from clipper.accounts import account_tokens, token_env_name
from clipper.jobs.models import Clock, utcnow
from clipper.posting.models import Post, PostMetadata, PublishDeferred, PublishResult

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"
_UPLOAD_HOST = "www.googleapis.com"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # lookup: list the channel's uploads
)

CATEGORY_ENTERTAINMENT = "24"
TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
MAX_HASHTAGS = 5
TAG_PREFIX = "clipper-"

UPLOAD_UNIT = 256 * 1024
"""Every chunk but the last must be a multiple of 256 KiB (resumable upload guide)."""
CHUNK_SIZE = 32 * UPLOAD_UNIT  # 8 MiB
MAX_UPLOAD_RETRIES = 5
UPLOAD_DEADLINE = timedelta(minutes=20)
"""Well within the posting stage's 30-minute STALE_UPLOAD."""
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

TOKEN_EXPIRY_MARGIN = timedelta(minutes=2)
TOKEN_LIFETIME_BOUNDS = (300, 86400)
"""Seconds; Google's `expires_in` is clamped into this range."""
PAUSE_RECHECK = timedelta(hours=1)
"""How far a paused account's posts are pushed each time they come due."""
REJECTED_GRANT_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})

QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
QUOTA_RESET_SLACK = timedelta(minutes=5)
DEFER_REASONS = frozenset({"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"})

LOOKUP_DEPTH = 50
"""Latest uploads per account that `lookup` checks (one page of the uploads playlist)."""
LOOKUP_RETRIES = 3

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HASHTAG = re.compile(r"#[^\W_]\w{0,99}")
_RANGE = re.compile(r"bytes=0-(\d+)")

logger = logging.getLogger(__name__)

RefreshTokenSource = Callable[[str], SecretStr | None]


def _account_refresh_token(account_id: str) -> SecretStr | None:
    return account_tokens(account_id).refresh_token


@dataclass(frozen=True)
class OAuthClient:
    """The owner's Google Cloud OAuth client (Web application type, see
    docs/DEPLOYMENT.md, "YouTube publishing")."""

    client_id: str
    client_secret: SecretStr | None


class YouTubeError(RuntimeError):
    """A YouTube or Google OAuth request failed. The message carries the HTTP status and
    the API's error reasons, never a token."""

    def __init__(self, message: str, *, status: int | None = None, reasons: Iterable[str] = ()):
        super().__init__(message)
        self.status = status
        self.reasons = frozenset(reasons)


class _Paused(Exception):  # noqa: N818 - internal signal
    def __init__(self, account_id: str, why: str) -> None:
        super().__init__(why)
        self.account_id = account_id
        self.why = why


@dataclass
class _Pause:
    fingerprint: str
    why: str
    alerted: bool = False


@dataclass(frozen=True)
class _AccessToken:
    fingerprint: str
    value: SecretStr
    expires: datetime


# --- metadata -----------------------------------------------------------------------------


def idempotency_tag(idempotency_key: str) -> str:
    """The tag that marks an upload as this post's, for `lookup`."""
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ValueError("invalid idempotency key")
    return f"{TAG_PREFIX}{idempotency_key}"


def _visible(text: str, *, keep_newlines: bool) -> str:
    """Drop `<` and `>` (YouTube refuses them in titles and descriptions) and control or
    invisible format characters."""
    out = []
    for ch in text:
        if ch in "<>":
            continue
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif unicodedata.category(ch).startswith("C"):
            out.append(" " if ch in "\t\r\n" else "")
        else:
            out.append(ch)
    return "".join(out)


def _title(metadata: PostMetadata) -> str:
    candidates = [metadata.title, *metadata.caption.splitlines()]
    for candidate in candidates:
        title = " ".join(_visible(candidate, keep_newlines=False).split())
        if title:
            if len(title) > TITLE_MAX_CHARS:
                title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
            return title
    raise ValueError("the post has no title or caption to title the YouTube video with")


def _hashtags(hashtags: Sequence[str]) -> list[str]:
    tags: list[str] = []
    for raw in hashtags:
        tag = raw.strip()
        tag = tag if tag.startswith("#") else f"#{tag}"
        if _HASHTAG.fullmatch(tag) and tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
        if len(tags) == MAX_HASHTAGS:
            break
    return tags


def _fit_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()


def _description(caption: str, hashtags: Sequence[str]) -> str:
    """The caption, then a blank line and up to five hashtags, within 5000 bytes. The
    caption is shortened to fit; the hashtags are kept."""
    caption = _visible(caption, keep_newlines=True).strip()
    tags = " ".join(_hashtags(hashtags))
    if not tags:
        return _fit_bytes(caption, DESCRIPTION_MAX_BYTES)
    if not caption:
        return tags
    room = DESCRIPTION_MAX_BYTES - len(f"\n\n{tags}".encode())
    return f"{_fit_bytes(caption, max(room, 0))}\n\n{tags}"


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def video_resource(post: Post, metadata: PostMetadata) -> dict[str, Any]:
    """The `videos.insert` body: private, published by YouTube at `post.scheduled_at`."""
    return {
        "snippet": {
            "title": _title(metadata),
            "description": _description(metadata.caption, metadata.hashtags),
            "tags": [idempotency_tag(post.idempotency_key)],
            "categoryId": CATEGORY_ENTERTAINMENT,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": _rfc3339(post.scheduled_at),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(metadata.flags.get("ai_generated", False)),
        },
    }


def next_quota_reset(now: datetime) -> datetime:
    """Just after the next midnight Pacific time, when the YouTube API quota resets."""
    local = now.astimezone(QUOTA_TIMEZONE)
    midnight = datetime.combine(local.date() + timedelta(days=1), dtime(0), QUOTA_TIMEZONE)
    return midnight.astimezone(UTC) + QUOTA_RESET_SLACK


def shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def _video_id(body: Any) -> str:
    video_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        raise YouTubeError("YouTube returned an unexpected video id")
    return video_id


def _error(response: httpx.Response, what: str) -> YouTubeError:
    """A YouTubeError from an API error response: status, reasons and Google's message
    (which never echoes credentials)."""
    reasons: list[str] = []
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        for item in error.get("errors") or ():
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.append(item["reason"])
    elif isinstance(error, str):  # the OAuth token endpoint
        reasons.append(error)
    label = ",".join(reasons) or "-"
    detail = f": {message[:300]}" if message else ""
    return YouTubeError(
        f"YouTube {what} failed: HTTP {response.status_code} {label}{detail}",
        status=response.status_code,
        reasons=reasons,
    )


def _session_uri(response: httpx.Response) -> str:
    """The resumable session URI, only if it is a plain https URL on Google's upload host:
    the bearer token is sent to it."""
    location = response.headers.get("Location")
    if not location:
        raise YouTubeError("YouTube did not return an upload session URI")
    parts = urlsplit(location)
    if (
        parts.scheme != "https"
        or parts.hostname != _UPLOAD_HOST
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or not parts.path.startswith("/upload/")
    ):
        raise YouTubeError("YouTube returned an unexpected upload session URI")
    return location


def _received(response: httpx.Response, size: int) -> int:
    """Bytes the server has, from a 308's `Range: bytes=0-N` (none: nothing yet)."""
    header = response.headers.get("Range")
    if not header:
        return 0
    match = _RANGE.fullmatch(header.strip())
    if not match or int(match[1]) + 1 > size:
        raise YouTubeError("YouTube returned an unexpected upload Range")
    return int(match[1]) + 1


def _quota_deferral(exc: YouTubeError, now: datetime) -> PublishDeferred | None:
    limited = sorted(exc.reasons & DEFER_REASONS)
    if not limited:
        return None
    reason = f"YouTube {limited[0]}: deferred to the next quota day"
    return PublishDeferred(reason, next_quota_reset(now))


# --- the publisher --------------------------------------------------------------------------


class YouTubePublisher:
    """`Publisher` for the `youtube` platform, covering every configured YouTube account."""

    def __init__(
        self,
        *,
        account_ids: Sequence[str],
        client: OAuthClient,
        refresh_token: RefreshTokenSource = _account_refresh_token,
        http: httpx.Client | None = None,
        clock: Clock = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        if chunk_size <= 0 or chunk_size % UPLOAD_UNIT:
            raise ValueError("chunk_size must be a positive multiple of 256 KiB")
        self.account_ids = tuple(account_ids)
        self.client = client
        self.refresh_token = refresh_token
        self.http = http or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False)
        self.clock = clock
        self.sleep = sleep
        self.chunk_size = chunk_size
        self._tokens: dict[str, _AccessToken] = {}
        self._paused: dict[str, _Pause] = {}
        self._unknown_alerted: set[str] = set()
        # Refresh tokens read during one publish/lookup call, so `.env` is parsed once per
        # call rather than once per request; None outside a call.
        self._call_tokens: dict[str, SecretStr | None] | None = None

    def __repr__(self) -> str:
        return f"YouTubePublisher(account_ids={self.account_ids!r})"

    def close(self) -> None:
        self.http.close()

    # --- Publisher ---------------------------------------------------------------------

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        if post.account_id not in self.account_ids:
            raise self._unknown_account(post.account_id)
        resource = video_resource(post, metadata)
        size = video_path.stat().st_size
        if size == 0:
            raise ValueError("the rendered video is empty")
        deadline = self.clock() + UPLOAD_DEADLINE
        self._call_tokens = {}
        try:
            session = self._start_session(post.account_id, resource, size)
            video = self._upload(post.account_id, session, video_path, size, deadline)
        except _Paused as paused:
            raise self._paused_deferral(paused) from None
        except YouTubeError as exc:
            deferral = _quota_deferral(exc, self.clock())
            if deferral is not None:
                raise deferral from None
            raise
        finally:
            self._call_tokens = None
        video_id = _video_id(video)
        logger.info(
            "youtube %s: uploaded %s for %s", post.account_id, video_id, _rfc3339(post.scheduled_at)
        )
        return PublishResult(external_id=video_id, url=shorts_url(video_id))

    def lookup(self, idempotency_key: str) -> PublishResult | None:
        """The upload tagged with this post's key on any configured account, or None.

        A paused account cannot be checked: if nothing is found elsewhere, this raises
        `PublishDeferred` rather than claim the upload does not exist. Quota errors defer
        too; other errors propagate (the posting stage then hands the post to a human)."""
        tag = idempotency_tag(idempotency_key)
        skipped: list[str] = []
        self._call_tokens = {}
        try:
            for account_id in self.account_ids:
                try:
                    video_id = self._find_tagged(account_id, tag)
                except _Paused as paused:
                    logger.warning(
                        "youtube %s: lookup skipped, account paused: %s", account_id, paused.why
                    )
                    skipped.append(account_id)
                    continue
                except YouTubeError as exc:
                    deferral = _quota_deferral(exc, self.clock())
                    if deferral is not None:
                        raise deferral from None
                    raise
                if video_id is not None:
                    return PublishResult(external_id=video_id, url=shorts_url(video_id))
        finally:
            self._call_tokens = None
        if skipped:
            reason = f"YouTube lookup could not check paused account(s) {', '.join(skipped)}"
            raise PublishDeferred(reason, self.clock() + PAUSE_RECHECK)
        return None

    def _unknown_account(self, account_id: str) -> PublishDeferred:
        alert = None
        if account_id not in self._unknown_alerted:
            self._unknown_alerted.add(account_id)
            alert = (
                f"Posts for {account_id} are on YouTube but the account is not an enabled"
                " YouTube account in accounts.yaml; they are deferred, not failed. Enable"
                " the account or cancel its posts (`clipper posts cancel`)."
            )
        reason = f"{account_id} is not an enabled YouTube account"
        return PublishDeferred(reason, self.clock() + PAUSE_RECHECK, alert=alert)

    # --- OAuth -------------------------------------------------------------------------

    def _refresh_token_for(self, account_id: str) -> SecretStr | None:
        if self._call_tokens is None:
            return self.refresh_token(account_id)
        if account_id not in self._call_tokens:
            self._call_tokens[account_id] = self.refresh_token(account_id)
        return self._call_tokens[account_id]

    def _fingerprint(self, account_id: str) -> tuple[str, SecretStr | None]:
        refresh = self._refresh_token_for(account_id)
        digest = hashlib.sha256()
        for part in (
            self.client.client_id,
            self.client.client_secret.get_secret_value() if self.client.client_secret else "",
            refresh.get_secret_value() if refresh else "",
        ):
            digest.update(part.encode("utf-8") + b"\0")
        return digest.hexdigest(), refresh

    def _pause(self, account_id: str, fingerprint: str, why: str) -> _Paused:
        pause = self._paused.get(account_id)
        if pause is None or pause.fingerprint != fingerprint:
            self._paused[account_id] = _Pause(fingerprint, why)
            logger.error("youtube %s: account paused: %s", account_id, why)
        self._tokens.pop(account_id, None)
        return _Paused(account_id, self._paused[account_id].why)

    def _paused_deferral(self, paused: _Paused) -> PublishDeferred:
        pause = self._paused[paused.account_id]
        alert = None
        if not pause.alerted:
            pause.alerted = True
            alert = (
                f"YouTube account {paused.account_id} is paused: {pause.why}. Its posts are"
                " deferred, not failed. Fix it as described in docs/DEPLOYMENT.md"
                " (YouTube publishing); posting resumes once new credentials are in place."
            )
        reason = f"YouTube account {paused.account_id} paused: {pause.why}"
        return PublishDeferred(reason, self.clock() + PAUSE_RECHECK, alert=alert)

    def _access_token(self, account_id: str, *, force: bool = False) -> str:
        fingerprint, refresh = self._fingerprint(account_id)
        pause = self._paused.get(account_id)
        if pause is not None:
            if pause.fingerprint == fingerprint:
                raise _Paused(account_id, pause.why)
            del self._paused[account_id]
            logger.info("youtube %s: new credentials, pause lifted", account_id)
        secret = self.client.client_secret
        if not self.client.client_id.strip() or secret is None or not secret.get_secret_value():
            raise self._pause(
                account_id,
                fingerprint,
                "the OAuth client is not configured (set YOUTUBE_OAUTH_CLIENT_ID and"
                " YOUTUBE_OAUTH_CLIENT_SECRET)",
            )
        if refresh is None:
            name = token_env_name(account_id, "refresh_token")
            raise self._pause(account_id, fingerprint, f"no refresh token (set {name})")
        cached = self._tokens.get(account_id)
        now = self.clock()
        if (
            not force
            and cached is not None
            and cached.fingerprint == fingerprint
            and now < cached.expires
        ):
            return cached.value.get_secret_value()
        response = self.http.post(TOKEN_URL, data=_grant(self.client.client_id, secret, refresh))
        if response.status_code in (400, 401):
            error = _error(response, "token refresh")
            rejected = sorted(error.reasons & REJECTED_GRANT_ERRORS)
            if rejected:
                why = f"Google rejected its OAuth credentials ({rejected[0]})"
                raise self._pause(account_id, fingerprint, why)
            raise error
        if response.status_code != 200:
            raise _error(response, "token refresh")
        try:
            body = response.json()
            value = body["access_token"]
            lifetime = int(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError, OverflowError):
            raise YouTubeError("YouTube token refresh returned an unexpected body") from None
        if not isinstance(value, str) or not value:
            raise YouTubeError("YouTube token refresh returned no access token")
        low, high = TOKEN_LIFETIME_BOUNDS
        lifetime = min(max(lifetime, low), high)
        expires = now + timedelta(seconds=lifetime) - TOKEN_EXPIRY_MARGIN
        self._tokens[account_id] = _AccessToken(fingerprint, SecretStr(value), expires)
        return value

    def _authorized(self, account_id: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """A request with the account's bearer token; on a 401, one retry with a fresh
        token."""
        headers = dict(kwargs.pop("headers", None) or {})
        response = None
        for force in (False, True):
            token = self._access_token(account_id, force=force)
            headers["Authorization"] = f"Bearer {token}"
            response = self.http.request(method, url, headers=headers, **kwargs)
            if response.status_code != 401:
                return response
        assert response is not None
        return response

    # --- upload ------------------------------------------------------------------------

    def _start_session(self, account_id: str, resource: dict[str, Any], size: int) -> str:
        response = self._authorized(
            account_id,
            "POST",
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=resource,
            headers={
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
        )
        if response.status_code != 200:
            raise _error(response, "upload session")
        return _session_uri(response)

    def _upload(
        self, account_id: str, session: str, video_path: Path, size: int, deadline: datetime
    ) -> dict[str, Any]:
        """Send the file in chunks. After a 5xx, a dropped connection or a chunk the server
        did not take, ask it how much it has and resume from there; MAX_UPLOAD_RETRIES
        such failures in a row give up."""
        offset, failures, resync = 0, 0, False
        last_problem = ""
        with video_path.open("rb") as video:
            while True:
                if self.clock() > deadline:
                    raise TimeoutError(f"YouTube upload did not finish within {UPLOAD_DEADLINE}")
                querying = resync or offset >= size
                try:
                    if querying:
                        response = self._authorized(
                            account_id,
                            "PUT",
                            session,
                            headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
                        )
                    else:
                        video.seek(offset)
                        chunk = video.read(self.chunk_size)
                        end = offset + len(chunk) - 1
                        response = self._authorized(
                            account_id,
                            "PUT",
                            session,
                            content=chunk,
                            headers={
                                "Content-Range": f"bytes {offset}-{end}/{size}",
                                "Content-Type": "video/mp4",
                            },
                        )
                except httpx.TransportError as exc:
                    last_problem = type(exc).__name__
                else:
                    if response.status_code in (200, 201):
                        return _video_body(response)
                    if response.status_code == 308:
                        received = _received(response, size)
                        if querying:
                            offset, resync = received, False
                            continue
                        if received > offset:
                            offset, failures = received, 0
                            continue
                        last_problem = "no progress"
                    elif response.status_code == 404:
                        raise YouTubeError("YouTube upload session expired (HTTP 404)", status=404)
                    elif response.status_code not in RETRYABLE_STATUS:
                        raise _error(response, "upload")
                    else:
                        last_problem = f"HTTP {response.status_code}"
                failures += 1
                if failures > MAX_UPLOAD_RETRIES:
                    raise YouTubeError(
                        f"YouTube upload failed after {MAX_UPLOAD_RETRIES} retries: {last_problem}"
                    )
                self.sleep(min(2.0**failures, 60.0))
                resync = True

    # --- lookup ------------------------------------------------------------------------

    def _get(self, account_id: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        """A read-only list call, retried on 5xx and dropped connections."""
        url = f"{API_URL}/{path}"
        for attempt in range(1, LOOKUP_RETRIES + 1):
            try:
                response = self._authorized(account_id, "GET", url, params=params)
            except httpx.TransportError:
                if attempt == LOOKUP_RETRIES:
                    raise
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == LOOKUP_RETRIES:
                    break
            self.sleep(float(2**attempt))
        if response.status_code != 200:
            raise _error(response, f"{path}.list")
        try:
            body = response.json()
        except ValueError:
            raise YouTubeError(f"YouTube {path}.list returned an unexpected body") from None
        return body if isinstance(body, dict) else {}

    def _find_tagged(self, account_id: str, tag: str) -> str | None:
        channels = self._get(account_id, "channels", {"part": "contentDetails", "mine": "true"})
        uploads = [
            item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            for item in channels.get("items") or ()
        ]
        playlist = next((u for u in uploads if isinstance(u, str) and u), None)
        if playlist is None:
            return None
        items = self._get(
            account_id,
            "playlistItems",
            {"part": "contentDetails", "playlistId": playlist, "maxResults": str(LOOKUP_DEPTH)},
        )
        ids = [
            vid
            for item in items.get("items") or ()
            if isinstance(vid := item.get("contentDetails", {}).get("videoId"), str)
            and _VIDEO_ID.fullmatch(vid)
        ]
        if not ids:
            return None
        videos = self._get(
            account_id,
            "videos",
            {"part": "snippet", "id": ",".join(ids), "maxResults": str(LOOKUP_DEPTH)},
        )
        for item in videos.get("items") or ():
            tags = item.get("snippet", {}).get("tags") or ()
            if tag in tags:
                return _video_id(item)
        return None


def _grant(client_id: str, secret: SecretStr, refresh: SecretStr) -> dict[str, str]:
    """The refresh-token grant body. Built here so the plain values live only in this
    short call and the request, not in a long-lived frame."""
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh.get_secret_value(),
        "client_id": client_id,
        "client_secret": secret.get_secret_value(),
    }


def _video_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise YouTubeError("YouTube returned an unexpected upload response")
    return body
