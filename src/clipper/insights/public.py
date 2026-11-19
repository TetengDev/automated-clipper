"""Public numbers of a YouTube channel and every Short on it (CLP-154), from the YouTube
Data API v3 with the monitor's API key (`YOUTUBE_API_KEY`, one key and one 10,000-unit
daily quota): `channels.list`, `playlistItems.list`, `videos.list` and
`commentThreads.list`, 1 quota unit per call. `videos.list` also gives each video's
title, publish time and duration, from which the collector tells a Short (180 s or less)
from a long video.

The key is sent only as the `key` query parameter to www.googleapis.com. httpx error
messages can quote the request URL, and so the key, so every transport error becomes an
`InsightsError` that names only the call. API errors carry the HTTP status and Google's
reasons. With an API key YouTube returns public videos only: a Short that is still
private (uploaded with `publishAt`) is absent from the answers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from clipper.insights.repo import VIDEO_ID
from clipper.jobs.models import from_iso
from clipper.pipeline.common import strip_control
from clipper.posting.youtube import google_error
from clipper.redact import redact

API_URL = "https://www.googleapis.com/youtube/v3"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_IDS = 50
"""videos.list and playlistItems.list take at most 50 per call."""
MAX_UPLOAD_PAGES = 10
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{2,64}")
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
MAX_TITLE = 200


class InsightsError(RuntimeError):
    """An insights request failed. The message never carries a key or a token."""

    def __init__(
        self, message: str, *, status: int | None = None, reasons: Iterable[str] = ()
    ) -> None:
        super().__init__(redact(message))
        self.status = status
        self.reasons = frozenset(reasons)


def api_error(response: httpx.Response, what: str) -> InsightsError:
    """An InsightsError from a Google API error body: the status, Google's reasons (see
    `posting.youtube.google_error`) and the start of Google's message."""
    reasons, message = google_error(response)
    label = ",".join(reasons) or "-"
    detail = f": {message[:200]}" if message else ""
    return InsightsError(
        f"{what} failed: HTTP {response.status_code} {label}{detail}",
        status=response.status_code,
        reasons=reasons,
    )


def count(value: Any) -> int | None:
    """A non-negative count from the API (Data API statistics are strings), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def parse_duration(value: Any) -> float | None:
    """Seconds from an ISO 8601 duration as videos.list gives it (`PT1M2S`, `P0D` for a
    live stream), else None."""
    if not isinstance(value, str) or value in ("P", "PT"):
        return None
    match = _DURATION.fullmatch(value)
    if match is None or value.endswith("T"):
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return from_iso(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ChannelStats:
    subscribers: int | None
    """None when the channel hides its count. YouTube rounds public counts."""
    total_views: int | None
    video_count: int | None
    uploads_playlist: str | None


@dataclass(frozen=True)
class Upload:
    video_id: str
    published_at: datetime


@dataclass(frozen=True)
class VideoStats:
    video_id: str
    views: int | None
    likes: int | None
    comments: int | None
    title: str | None = None
    published_at: datetime | None = None
    duration_s: float | None = None
    """From contentDetails.duration; a Short is 180 s or less."""


class PublicYouTube:
    """Data API reads with an API key. `units` counts the quota units spent."""

    def __init__(self, http: httpx.Client, api_key: SecretStr) -> None:
        self.http = http
        self._key = api_key
        self.units = 0

    def __repr__(self) -> str:
        return "PublicYouTube(<key redacted>)"

    def channel(self, channel_id: str) -> ChannelStats | None:
        body = self._list("channels", {"part": "statistics,contentDetails", "id": channel_id})
        for item in body.get("items") or ():
            if not isinstance(item, dict):
                continue
            stats = item.get("statistics") or {}
            related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
            playlist = related.get("uploads")
            return ChannelStats(
                subscribers=(
                    None
                    if stats.get("hiddenSubscriberCount") is True
                    else count(stats.get("subscriberCount"))
                ),
                total_views=count(stats.get("viewCount")),
                video_count=count(stats.get("videoCount")),
                uploads_playlist=(
                    playlist
                    if isinstance(playlist, str) and _PLAYLIST_ID.fullmatch(playlist)
                    else None
                ),
            )
        return None

    def uploads_since(self, playlist_id: str, since: datetime) -> list[Upload]:
        """Public uploads published at or after `since`, newest first (the uploads
        playlist's order). Stops after the first page that reaches back past `since`."""
        uploads: list[Upload] = []
        token: str | None = None
        for _ in range(MAX_UPLOAD_PAGES):
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": str(MAX_IDS),
            }
            if token:
                params["pageToken"] = token
            body = self._list("playlistItems", params)
            older = False
            for item in body.get("items") or ():
                details = item.get("contentDetails") if isinstance(item, dict) else None
                if not isinstance(details, dict):
                    continue
                vid = details.get("videoId")
                published = _when(details.get("videoPublishedAt"))
                if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or published is None:
                    continue
                if published < since:
                    older = True
                    continue
                uploads.append(Upload(vid, published))
            next_token = body.get("nextPageToken")
            if older or not isinstance(next_token, str) or not next_token:
                break
            token = next_token
        return uploads

    def video_stats(self, video_ids: Sequence[str]) -> dict[str, VideoStats]:
        """Title, publish time, duration and counts of the public ones among `video_ids`;
        private or removed videos are absent from the result. 1 unit per 50 ids."""
        ids = [v for v in dict.fromkeys(video_ids) if VIDEO_ID.fullmatch(v)]
        found: dict[str, VideoStats] = {}
        for first in range(0, len(ids), MAX_IDS):
            batch = ids[first : first + MAX_IDS]
            body = self._list(
                "videos",
                {
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(batch),
                    "maxResults": str(MAX_IDS),
                },
            )
            for item in body.get("items") or ():
                if not isinstance(item, dict) or item.get("id") not in batch:
                    continue
                stats = item.get("statistics") or {}
                snippet = item.get("snippet") or {}
                details = item.get("contentDetails") or {}
                title = snippet.get("title")
                clean = strip_control(title)[:MAX_TITLE] if isinstance(title, str) else ""
                vid = str(item["id"])
                found[vid] = VideoStats(
                    vid,
                    count(stats.get("viewCount")),
                    count(stats.get("likeCount")),
                    count(stats.get("commentCount")),
                    title=clean or None,
                    published_at=_when(snippet.get("publishedAt")),
                    duration_s=parse_duration(details.get("duration")),
                )
        return found

    def comments_enabled(self, video_id: str) -> bool | None:
        """True when the Short takes comments, False when they are off, None when YouTube
        does not say (the video is gone or not public)."""
        response = self._get(
            "commentThreads", {"part": "id", "videoId": video_id, "maxResults": "1"}
        )
        if response.status_code == 200:
            return True
        error = api_error(response, "YouTube commentThreads.list")
        if response.status_code == 403 and "commentsDisabled" in error.reasons:
            return False
        if response.status_code == 404:
            return None
        raise error

    def _list(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._get(path, params)
        if response.status_code != 200:
            raise api_error(response, f"YouTube {path}.list")
        try:
            body = response.json()
        except ValueError:
            raise InsightsError(f"YouTube {path}.list returned an unexpected body") from None
        return body if isinstance(body, dict) else {}

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        try:
            response = self.http.get(
                f"{API_URL}/{path}",
                params={**params, "key": self._key.get_secret_value()},
                timeout=HTTP_TIMEOUT,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise InsightsError(f"YouTube {path}.list unreachable ({type(exc).__name__})") from None
        self.units += 1
        return response
