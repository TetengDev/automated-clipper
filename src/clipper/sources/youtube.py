"""YouTube source (CLP-23): RSS gate first, metadata only for video IDs not seen before.

The channel's RSS feed (latest ~15 uploads) costs no quota, so a poll with nothing new
makes no metadata calls at all. Metadata comes from a provider:
- `YtDlpProvider` (keyless, the default): one `yt-dlp -J --skip-download` per new video
  through the hardened runner in `clipper.pipeline.ytdlp`;
- `ApiProvider` (when YOUTUBE_API_KEY is set): Data API v3, 1 quota unit per call,
  `videos.list` batched 50 IDs per call. `search.list` (100 units) is never used.

When the feed fails (HTTP error, timeout, malformed XML) the provider lists the channel's
uploads instead: `playlistItems.list` on the long-form uploads playlist (UULF..., then
UU... on 404) with a key, or the channel's videos tab via yt-dlp without one.

Network XML is parsed with defusedxml only. Every video ID is validated against the
`SourceRef` pattern before it is used anywhere.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote
from xml.etree.ElementTree import ParseError  # noqa: S405 - exception type only

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from clipper.config import Secrets
from clipper.jobs.stages import PermanentError
from clipper.pipeline.ytdlp import Unavailable, YtDlp, YtDlpError
from clipper.redact import redact
from clipper.urls import InvalidSourceURL, SourceRef, canonical_url

logger = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
API_BASE = "https://www.googleapis.com/youtube/v3"
API_BATCH = 50
FALLBACK_ITEMS = 15
"""The feed carries the latest 15 uploads; the fallback listing asks for as many."""
FEED_MAX_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT = httpx.Timeout(20.0)
CHANNEL_ID = re.compile(r"UC[\w-]{22}")

LiveStatus = Literal["not_live", "is_live", "is_upcoming", "post_live", "was_live"]
LIVE_STATUSES: frozenset[str] = frozenset(
    {"not_live", "is_live", "is_upcoming", "post_live", "was_live"}
)
NOT_FINISHED: frozenset[str] = frozenset({"is_live", "is_upcoming", "post_live"})
"""Live now, scheduled, or a finished stream whose VOD is still processing."""

_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
# yt-dlp fails on a scheduled stream or premiere instead of reporting is_upcoming.
_UPCOMING = re.compile(r"live event will begin|premieres in", re.IGNORECASE)
_NOT_FOUND = re.compile(r"HTTP Error 404")
_ISO_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


class SourceError(RuntimeError):
    """Listing or metadata failed in a way that may work next poll. Message is redacted."""


class FeedError(SourceError):
    """The RSS feed failed (HTTP error, timeout, malformed XML); use the fallback listing."""


class ApiError(SourceError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class HandleNotFound(LookupError):
    """No channel has this handle. Message is safe to show."""


@dataclass(frozen=True)
class Upload:
    video_id: str
    published: datetime | None = None


@dataclass(frozen=True)
class VideoMeta:
    video_id: str
    title: str | None
    duration_s: int | None
    live_status: LiveStatus
    published: datetime | None


@dataclass
class Metadata:
    """Result of one metadata batch. An ID in none of the three was not looked at (the
    poll was interrupted); it stays unseen and is fetched next poll."""

    videos: dict[str, VideoMeta] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    """Private, removed or members-only: never processable."""
    errors: dict[str, str] = field(default_factory=dict)
    """Retryable per-video failures (redacted)."""


@dataclass
class QuotaMeter:
    """Data API units spent through one provider (the keyless provider never spends)."""

    units: int = 0


def _never() -> bool:
    return False


class MetadataProvider(Protocol):
    quota: QuotaMeter

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool] = _never) -> Metadata: ...

    def uploads(self, channel_id: str) -> list[Upload]: ...

    def channel_id(self, handle: str) -> str: ...


def _video_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return SourceRef("youtube", value).item_id
    except InvalidSourceURL:
        return None


def _parse_time(value: object) -> datetime | None:
    """An ISO-8601 stamp (feed, Data API) or a Unix timestamp (yt-dlp) -> aware UTC."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _uploads(pairs: Iterable[tuple[object, object]]) -> list[Upload]:
    """(raw id, raw published) -> Uploads, dropping invalid IDs and duplicates."""
    uploads: list[Upload] = []
    seen: set[str] = set()
    for raw_id, raw_published in pairs:
        video_id = _video_id(raw_id)
        if video_id is None:
            logger.warning("ignoring invalid YouTube video id %r", str(raw_id)[:40])
            continue
        if video_id not in seen:
            seen.add(video_id)
            uploads.append(Upload(video_id, _parse_time(raw_published)))
    return uploads


def parse_feed(data: bytes) -> list[Upload]:
    try:
        root = fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise FeedError(f"malformed feed XML ({type(exc).__name__})") from None
    if root.tag != f"{_ATOM}feed":
        raise FeedError("malformed feed XML (not an Atom feed)")
    return _uploads(
        (entry.findtext(f"{_YT}videoId"), entry.findtext(f"{_ATOM}published"))
        for entry in root.iter(f"{_ATOM}entry")
    )


def feed_uploads(http: httpx.Client, channel_id: str) -> list[Upload]:
    """The channel's latest uploads from its RSS feed (no quota)."""
    try:
        response = http.get(FEED_URL, params={"channel_id": channel_id})
    except httpx.HTTPError as exc:
        raise FeedError(f"feed request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise FeedError(f"feed returned HTTP {response.status_code}")
    if len(response.content) > FEED_MAX_BYTES:
        raise FeedError("feed too large")
    return parse_feed(response.content)


def iso_duration(text: str) -> int | None:
    """ISO-8601 duration as the Data API writes it (P1DT2H3M4S) -> seconds."""
    match = _ISO_DURATION.fullmatch(text)
    if not text or match is None:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value)


def _title(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass
class YtDlpProvider:
    """Keyless metadata: one yt-dlp probe per new video (~1-2 s each, no quota)."""

    ytdlp: YtDlp
    quota: QuotaMeter = field(default_factory=QuotaMeter)

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool] = _never) -> Metadata:
        result = Metadata()
        for video_id in ids:
            if should_stop():
                break
            try:
                info = self.ytdlp.probe(canonical_url(SourceRef("youtube", video_id)))
            except Unavailable:
                result.unavailable.add(video_id)
            except YtDlpError as exc:
                if _UPCOMING.search(str(exc)):
                    result.videos[video_id] = VideoMeta(video_id, None, None, "is_upcoming", None)
                else:
                    result.errors[video_id] = str(exc)
            else:
                result.videos[video_id] = self._meta(video_id, info)
        return result

    @staticmethod
    def _meta(video_id: str, info: dict[str, Any]) -> VideoMeta:
        status = info.get("live_status")
        if status not in LIVE_STATUSES:
            status = "is_live" if info.get("is_live") is True else "not_live"
        published = _parse_time(info.get("timestamp"))
        if published is None and isinstance(info.get("upload_date"), str):
            try:
                published = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                published = None
        return VideoMeta(
            video_id, _title(info.get("title")), _seconds(info.get("duration")), status, published
        )

    def uploads(self, channel_id: str) -> list[Upload]:
        """The channel's videos tab (long-form uploads, newest first). Dates come from the
        page's "N weeks ago" text (`approximate_date`): rounded down, so never earlier than
        the real upload, which keeps the first-run lookback filter from dropping recent
        uploads while it spares probes for old ones."""
        url = f"https://www.youtube.com/channel/{_channel(channel_id)}/videos"
        try:
            info = self.ytdlp.listing(
                url, f"1:{FALLBACK_ITEMS}", extractor_args="youtubetab:approximate_date"
            )
        except (YtDlpError, PermanentError) as exc:
            raise SourceError(f"yt-dlp listing failed: {exc}") from None
        entries = info.get("entries")
        rows = entries if isinstance(entries, list) else []
        return _uploads((e.get("id"), e.get("timestamp")) for e in rows if isinstance(e, dict))

    def channel_id(self, handle: str) -> str:
        url = f"https://www.youtube.com/{quote(handle, safe='@')}"
        try:
            info = self.ytdlp.listing(url, "0")
        except Unavailable:
            raise HandleNotFound(f"no YouTube channel has the handle {handle}") from None
        except YtDlpError as exc:
            if _NOT_FOUND.search(str(exc)):
                raise HandleNotFound(f"no YouTube channel has the handle {handle}") from None
            raise SourceError(f"yt-dlp could not look up {handle}: {exc}") from None
        channel_id = info.get("channel_id")
        if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
            raise HandleNotFound(f"no YouTube channel id found for the handle {handle}")
        return channel_id


def _channel(channel_id: str) -> str:
    if not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError(f"invalid channel id: {channel_id!r}")
    return channel_id


@dataclass
class ApiProvider:
    """YouTube Data API v3. Every request costs 1 quota unit (videos, playlistItems and
    channels.list all do); the key is a query parameter and never appears in messages."""

    http: httpx.Client
    api_key: str
    quota: QuotaMeter = field(default_factory=QuotaMeter)

    def _get(self, endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        self.quota.units += 1
        try:
            response = self.http.get(
                f"{API_BASE}/{endpoint}", params={**params, "key": self.api_key}
            )
        except httpx.HTTPError as exc:
            # `from None`: the httpx error's message and chain carry the URL with the key.
            raise SourceError(
                f"YouTube API {endpoint} request failed ({type(exc).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            reason = _api_reason(body)
            message = redact(f"YouTube API {endpoint}: HTTP {response.status_code} ({reason})")
            raise ApiError(message.replace(self.api_key, "REDACTED"), response.status_code)
        if not isinstance(body, dict):
            raise SourceError(f"YouTube API {endpoint}: unexpected response")
        return body

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool] = _never) -> Metadata:
        result = Metadata()
        for start in range(0, len(ids), API_BATCH):
            if should_stop():
                break
            batch = list(ids[start : start + API_BATCH])
            body = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails,liveStreamingDetails",
                    "id": ",".join(batch),
                    "maxResults": API_BATCH,
                },
            )
            for item in _items(body):
                video_id = _video_id(item.get("id"))
                if video_id in batch:
                    result.videos[video_id] = self._meta(video_id, item)
            # Private and removed videos are simply left out of the response.
            result.unavailable.update(v for v in batch if v not in result.videos)
        return result

    @staticmethod
    def _meta(video_id: str, item: dict[str, Any]) -> VideoMeta:
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        details = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
        raw_duration = details.get("duration")
        duration = iso_duration(raw_duration) if isinstance(raw_duration, str) else None
        broadcast = snippet.get("liveBroadcastContent")
        status: LiveStatus
        if broadcast == "live":
            status = "is_live"
        elif broadcast == "upcoming":
            status = "is_upcoming"
        elif isinstance(item.get("liveStreamingDetails"), dict):
            status = "was_live" if duration else "post_live"  # P0D: VOD still processing
        else:
            status = "not_live"
        return VideoMeta(
            video_id,
            _title(snippet.get("title")),
            duration or None,
            status,
            _parse_time(snippet.get("publishedAt")),
        )

    def uploads(self, channel_id: str) -> list[Upload]:
        """Latest long-form uploads (UULF...); all uploads (UU...) if that playlist 404s."""
        suffix = _channel(channel_id)[2:]
        try:
            body = self._playlist(f"UULF{suffix}")
        except ApiError as exc:
            if exc.status != 404:
                raise
            body = self._playlist(f"UU{suffix}")
        return _uploads(
            (d.get("videoId"), d.get("videoPublishedAt"))
            for d in (i.get("contentDetails") for i in _items(body))
            if isinstance(d, dict)
        )

    def _playlist(self, playlist_id: str) -> dict[str, Any]:
        return self._get(
            "playlistItems",
            {"part": "contentDetails", "playlistId": playlist_id, "maxResults": FALLBACK_ITEMS},
        )

    def channel_id(self, handle: str) -> str:
        body = self._get("channels", {"part": "id", "forHandle": handle})
        ids = [i.get("id") for i in _items(body)]
        if not ids or not isinstance(ids[0], str) or not CHANNEL_ID.fullmatch(ids[0]):
            raise HandleNotFound(f"no YouTube channel has the handle {handle}")
        return ids[0]


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _api_reason(body: object) -> str:
    try:
        reason = body["error"]["errors"][0]["reason"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return "no reason given"
    return str(reason)[:100]


@dataclass
class YouTubeSource:
    """RSS first; the provider's listing only when the feed fails."""

    http: httpx.Client
    provider: MetadataProvider

    def recent_uploads(self, channel_id: str) -> list[Upload]:
        try:
            return feed_uploads(self.http, channel_id)
        except FeedError as feed_error:
            logger.warning("%s: %s; listing uploads instead", channel_id, feed_error)
            try:
                return self.provider.uploads(channel_id)
            except SourceError as exc:
                raise SourceError(f"feed: {feed_error}; fallback: {exc}") from None

    def videos(self, ids: Sequence[str], should_stop: Callable[[], bool] = _never) -> Metadata:
        return self.provider.videos(ids, should_stop)

    @property
    def quota_units(self) -> int:
        return self.provider.quota.units


def metadata_provider(secrets: Secrets, http: httpx.Client, ytdlp: YtDlp) -> MetadataProvider:
    """The Data API when YOUTUBE_API_KEY is set, else keyless yt-dlp."""
    key = secrets.youtube_api_key.get_secret_value() if secrets.youtube_api_key else ""
    if key:
        return ApiProvider(http, key)
    return YtDlpProvider(ytdlp)


def build_source(secrets: Secrets, http: httpx.Client, ytdlp: YtDlp) -> YouTubeSource:
    return YouTubeSource(http, metadata_provider(secrets, http, ytdlp))
