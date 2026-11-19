"""Private channel and Short analytics from the YouTube Analytics API v2 (CLP-154):
`reports.query` at https://youtubeanalytics.googleapis.com/v2/reports with the account's
own OAuth token, which needs the scope `yt-analytics.readonly`.

The access token is minted from the account's refresh token (`CLIPPER_REFRESH_TOKEN_<ID>`)
with the YouTube publisher's OAuth client and code (`posting.youtube.mint_access_token`).
A token without the scope gets HTTP 403 (`insufficientPermissions`, or
`ACCESS_TOKEN_SCOPE_INSUFFICIENT` in the error details): that raises `ScopeMissing`, and the
insights job keeps the public numbers and flags the account for re-authorisation. Tokens
are held as SecretStr, sent only in the Authorization header to Google's fixed hosts, and
never appear in errors, logs or repr.

Analytics data arrives 1 to 3 days late, and a report on a video published today has no
`rows` at all: every parser below treats that as no data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from pydantic import SecretStr

from clipper.accounts import token_env_name
from clipper.insights.public import HTTP_TIMEOUT, InsightsError, api_error, count
from clipper.insights.repo import VIDEO_ID
from clipper.posting.youtube import (
    ANALYTICS_SCOPE,
    GrantRejected,
    OAuthClient,
    RefreshTokenSource,
    YouTubeError,
    account_refresh_token,
    mint_access_token,
)

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
VIDEO_METRICS = (
    "views",
    "engagedViews",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "comments",
    "shares",
)
SHORTS_FILTER = "creatorContentType==shorts"
"""Lowercase: the API answers `creatorContentType==SHORTS` with HTTP 400 (live check,
2026-11-19)."""
MAX_FILTER_IDS = 50


class NoCredentials(InsightsError):
    """The OAuth client or the account's refresh token is not configured."""


class CredentialsRejected(InsightsError):
    """Google refused the refresh token or the OAuth client."""


class ScopeMissing(InsightsError):
    """The account's token lacks yt-analytics.readonly: re-authorise the account."""


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True)
class VideoTotals:
    views: int | None
    engaged_views: int | None
    avg_view_s: float | None
    avg_view_pct: float | None
    subs_gained: int | None
    subs_lost: int | None
    likes: int | None
    comments: int | None
    shares: int | None


@dataclass(frozen=True)
class ShortsDay:
    day: date
    views: int
    subs_gained: int
    subs_lost: int


class YouTubeAnalytics:
    """Mints a short-lived token per account and hands out its reports."""

    def __init__(
        self,
        http: httpx.Client,
        client: OAuthClient,
        refresh_token: RefreshTokenSource = account_refresh_token,
    ) -> None:
        self.http = http
        self.client = client
        self.refresh_token = refresh_token

    def __repr__(self) -> str:
        return "YouTubeAnalytics()"

    def for_account(self, account_id: str) -> AccountReports:
        secret = self.client.client_secret
        if not self.client.client_id.strip() or secret is None or not secret.get_secret_value():
            raise NoCredentials(
                "the OAuth client is not configured (set YOUTUBE_OAUTH_CLIENT_ID and"
                " YOUTUBE_OAUTH_CLIENT_SECRET)"
            )
        refresh = self.refresh_token(account_id)
        if refresh is None:
            name = token_env_name(account_id, "refresh_token")
            raise NoCredentials(f"no refresh token (set {name})")
        try:
            grant = mint_access_token(self.http, self.client.client_id, secret, refresh)
        except GrantRejected as exc:
            raise CredentialsRejected(
                f"Google rejected the OAuth credentials ({exc.reason}); re-authorise the account"
            ) from None
        except YouTubeError as exc:
            raise InsightsError(str(exc), status=exc.status, reasons=exc.reasons) from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise InsightsError(f"token refresh unreachable ({type(exc).__name__})") from None
        return AccountReports(self.http, grant.value)


class AccountReports:
    """One account's Analytics reports, on one access token (good for about an hour)."""

    def __init__(self, http: httpx.Client, token: SecretStr) -> None:
        self.http = http
        self._token = token

    def __repr__(self) -> str:
        return "AccountReports(<token redacted>)"

    def query(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """`reports.query` for the token's own channel. Rows as dicts keyed by column
        name; no `rows` in the answer is an empty list."""
        try:
            response = self.http.get(
                REPORTS_URL,
                params={"ids": "channel==MINE", **params},
                headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                timeout=HTTP_TIMEOUT,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise InsightsError(
                f"Analytics reports.query unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            error = api_error(response, "Analytics reports.query")
            if response.status_code == 403 and error.reasons & SCOPE_REASONS:
                raise ScopeMissing(
                    f"the token lacks {ANALYTICS_SCOPE}", status=403, reasons=error.reasons
                )
            raise error
        try:
            body = response.json()
        except ValueError:
            raise InsightsError("Analytics reports.query returned an unexpected body") from None
        if not isinstance(body, dict):
            return []
        names = [h.get("name") for h in body.get("columnHeaders") or () if isinstance(h, dict)]
        return [
            dict(zip(names, row, strict=False))
            for row in body.get("rows") or ()
            if isinstance(row, list)
        ]

    def video_totals(
        self, video_ids: Sequence[str], start: date, end: date
    ) -> dict[str, VideoTotals]:
        ids = [v for v in dict.fromkeys(video_ids) if VIDEO_ID.fullmatch(v)]
        found: dict[str, VideoTotals] = {}
        for first in range(0, len(ids), MAX_FILTER_IDS):
            batch = ids[first : first + MAX_FILTER_IDS]
            rows = self.query(
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "metrics": ",".join(VIDEO_METRICS),
                    "dimensions": "video",
                    "filters": "video==" + ",".join(batch),
                    "sort": "-views",
                    "maxResults": str(len(batch)),
                }
            )
            for row in rows:
                vid = row.get("video")
                if not isinstance(vid, str) or vid not in batch:
                    continue
                found[vid] = VideoTotals(
                    views=count(row.get("views")),
                    engaged_views=count(row.get("engagedViews")),
                    avg_view_s=number(row.get("averageViewDuration")),
                    avg_view_pct=number(row.get("averageViewPercentage")),
                    subs_gained=count(row.get("subscribersGained")),
                    subs_lost=count(row.get("subscribersLost")),
                    likes=count(row.get("likes")),
                    comments=count(row.get("comments")),
                    shares=count(row.get("shares")),
                )
        return found

    def traffic(self, video_id: str, start: date, end: date) -> dict[str, int]:
        """Views by traffic source (SHORTS, YT_SEARCH, SUBSCRIBER, EXT_URL, ...)."""
        rows = self.query(
            self._one_video(video_id, start, end, "views", "insightTrafficSourceType")
        )
        found: dict[str, int] = {}
        for row in rows:
            source = row.get("insightTrafficSourceType")
            views = count(row.get("views"))
            if isinstance(source, str) and views is not None:
                found[source] = views
        return found

    def retention(self, video_id: str, start: date, end: date) -> list[tuple[float, float]]:
        """The retention curve: (elapsedVideoTimeRatio, audienceWatchRatio) points."""
        rows = self.query(
            self._one_video(video_id, start, end, "audienceWatchRatio", "elapsedVideoTimeRatio")
        )
        points: list[tuple[float, float]] = []
        for row in rows:
            x = number(row.get("elapsedVideoTimeRatio"))
            y = number(row.get("audienceWatchRatio"))
            if x is not None and y is not None:
                points.append((x, y))
        return points

    def shorts_days(self, start: date, end: date) -> list[ShortsDay]:
        """The channel's Shorts views and subscriber changes per day."""
        rows = self.query(
            {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "metrics": "views,subscribersGained,subscribersLost",
                "dimensions": "day",
                "filters": SHORTS_FILTER,
                "sort": "day",
            }
        )
        days: list[ShortsDay] = []
        for row in rows:
            try:
                day = date.fromisoformat(str(row.get("day")))
            except ValueError:
                continue
            days.append(
                ShortsDay(
                    day,
                    count(row.get("views")) or 0,
                    count(row.get("subscribersGained")) or 0,
                    count(row.get("subscribersLost")) or 0,
                )
            )
        return days

    @staticmethod
    def _one_video(
        video_id: str, start: date, end: date, metrics: str, dimension: str
    ) -> dict[str, str]:
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("invalid video id")
        return {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": metrics,
            "dimensions": dimension,
            "filters": f"video=={video_id}",
            "sort": dimension if dimension == "elapsedVideoTimeRatio" else f"-{metrics}",
        }
