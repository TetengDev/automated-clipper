"""Source URL parsing: a strict allowlist that maps user input to (platform, item_id).

Anything that leaves this process (yt-dlp args, HTTP requests) must be built from
`canonical_url(ref)`, never from the raw input string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

Platform = Literal["youtube", "twitch"]

MAX_URL_LENGTH = 2048
_ITEM_ID = {
    "youtube": re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{10}"),
    "twitch": re.compile(r"[1-9][0-9]{0,19}"),
}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}


class InvalidSourceURL(ValueError):
    """URL is not a supported YouTube video or Twitch VOD. Message is safe to show."""


@dataclass(frozen=True)
class SourceRef:
    platform: Platform
    item_id: str

    def __post_init__(self) -> None:
        pattern = _ITEM_ID.get(self.platform)
        if pattern is None or not pattern.fullmatch(self.item_id):
            raise InvalidSourceURL(f"invalid {self.platform} id: {self.item_id!r}")

    @property
    def key(self) -> str:
        """Filesystem-safe identity, e.g. `youtube_dQw4w9WgXcQ` (media dir name)."""
        return f"{self.platform}_{self.item_id}"


def canonical_url(ref: SourceRef) -> str:
    if ref.platform == "youtube":
        return f"https://www.youtube.com/watch?v={ref.item_id}"
    return f"https://www.twitch.tv/videos/{ref.item_id}"


def parse_source_url(url: str) -> SourceRef:
    """Parse a YouTube watch/live/youtu.be URL or a Twitch VOD URL (https only).

    Strict: ASCII only, no backslashes, ids are taken raw (percent-encoding is rejected).
    """
    if len(url) > MAX_URL_LENGTH or not url.isprintable() or any(c.isspace() for c in url):
        raise InvalidSourceURL("URL is too long or contains whitespace/control characters")
    if not url.isascii() or "\\" in url:
        raise InvalidSourceURL("URL must be plain ASCII without backslashes")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidSourceURL("malformed URL") from None
    if parts.scheme != "https":
        raise InvalidSourceURL("URL must start with https://")
    # Hostname only: reject userinfo ("user@host") and explicit ports.
    host = parts.netloc.lower()
    if "@" in host or ":" in host:
        raise InvalidSourceURL("URL must not contain credentials or a port")
    segments = [s for s in parts.path.split("/") if s]

    if host in _YOUTUBE_HOSTS:
        if segments == ["watch"]:
            ids = [
                p.partition("=")[2] for p in parts.query.split("&") if p.partition("=")[0] == "v"
            ]
            if len(ids) == 1:
                return _ref("youtube", ids[0])
        elif len(segments) == 2 and segments[0] == "live":
            return _ref("youtube", segments[1])
        raise InvalidSourceURL(
            "unsupported YouTube URL; use youtube.com/watch?v=ID, youtube.com/live/ID"
            " or youtu.be/ID"
        )
    if host == "youtu.be":
        if len(segments) == 1:
            return _ref("youtube", segments[0])
        raise InvalidSourceURL("unsupported youtu.be URL; expected youtu.be/ID")
    if host in _TWITCH_HOSTS:
        if len(segments) == 2 and segments[0] == "videos":
            return _ref("twitch", segments[1])
        raise InvalidSourceURL("unsupported Twitch URL; expected twitch.tv/videos/<number>")
    raise InvalidSourceURL("unsupported host; expected a YouTube or Twitch video URL")


def _ref(platform: Platform, item_id: str) -> SourceRef:
    try:
        return SourceRef(platform, item_id)
    except InvalidSourceURL:
        raise InvalidSourceURL(f"not a valid {platform} video id: {item_id!r}") from None
