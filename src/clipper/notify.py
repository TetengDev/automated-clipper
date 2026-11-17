"""Operator notifications through a Discord webhook (CLP-97's campaign submission pings).

A minimal sender: one POST per message, no retry here (callers retry on their own
schedule). Mentions are always disabled (`allowed_mentions.parse = []`), so text that
reaches a message can never ping @everyone or a role. The webhook URL is a secret: it
never appears in an exception, log line or repr. CLP-30 builds the clip-batch messages
and alert dedupe on top of the same webhooks.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

DISCORD_CONTENT_MAX = 2000
TIMEOUT_S = 5.0
_DISCORD_HOSTS = frozenset(
    {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
)

logger = logging.getLogger(__name__)

Notify = Callable[[str], None]


class NotifyError(Exception):
    """A message was not delivered. The text never includes the webhook URL.

    `status` is the HTTP status, None when the webhook was not reached. `retry_after` is
    the wait in seconds a 429 asked for, None when it gave no usable figure."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float | None:
    """The seconds a 429 asks us to wait: Discord's JSON `retry_after`, else the
    `Retry-After` header. None for anything missing, malformed, negative or infinite."""
    candidates: list[object] = []
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        candidates.append(body.get("retry_after"))
    candidates.append(response.headers.get("Retry-After"))
    for value in candidates:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            continue
        try:
            seconds = float(value)
        except ValueError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


class DiscordWebhook:
    """Send plain-text messages to one Discord webhook."""

    def __init__(self, url: str, http: httpx.Client) -> None:
        if any(c.isspace() or not c.isprintable() for c in url):
            raise ValueError("a Discord webhook URL must be a valid URL")
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("a Discord webhook URL must be a valid URL") from None
        if (
            port not in (None, 443)
            or parts.scheme != "https"
            or parts.hostname not in _DISCORD_HOSTS
            or not parts.path.startswith("/api/webhooks/")
        ):
            raise ValueError("a Discord webhook URL must be https://discord.com/api/webhooks/...")
        try:
            httpx.URL(url)
        except httpx.InvalidURL:
            raise ValueError("a Discord webhook URL must be a valid URL") from None
        self._url = url
        self._http = http

    def __repr__(self) -> str:
        return "DiscordWebhook(<redacted>)"

    def send(self, text: str) -> None:
        content = (
            text if len(text) <= DISCORD_CONTENT_MAX else text[: DISCORD_CONTENT_MAX - 1] + "…"
        )
        try:
            response = self._http.post(
                self._url,
                json={"content": content, "allowed_mentions": {"parse": []}},
                timeout=TIMEOUT_S,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx messages can carry the request URL, which holds the webhook token.
            raise NotifyError(f"discord webhook unreachable ({type(exc).__name__})") from None
        if response.status_code >= 300:
            status = response.status_code
            raise NotifyError(
                f"discord webhook returned {status}",
                status=status,
                retry_after=_retry_after(response) if status == 429 else None,
            )

    __call__ = send


def log_notify(text: str) -> None:
    """Fallback when no webhook is configured: the message goes to the log."""
    logger.warning("NOTIFY: %s", text)
