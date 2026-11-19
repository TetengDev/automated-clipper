"""YouTube Analytics reports with the account's own token (CLP-154). Google's token
endpoint and the reports endpoint are an httpx MockTransport."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from clipper.insights.analytics import (
    REPORTS_URL,
    CredentialsRejected,
    NoCredentials,
    ScopeMissing,
    ShortsDay,
    VideoTotals,
    YouTubeAnalytics,
)
from clipper.insights.public import InsightsError
from clipper.posting.youtube import TOKEN_URL, OAuthClient

ACCOUNT = "random_gaming_yt"
REFRESH = "1//recorded-refresh-token-value"
SECRET = "GOCSPX-recorded-client-secret"
ACCESS = "ya29.recorded-access-token-value"
START, END = date(2026, 10, 15), date(2027, 1, 15)


def header(*names: str) -> list[dict[str, str]]:
    return [{"name": n, "columnType": "METRIC", "dataType": "INTEGER"} for n in names]


class Google:
    """Fake token endpoint plus reports answers keyed by the `dimensions` parameter."""

    def __init__(self, reports: dict[str, Any] | None = None, token_status: int = 200) -> None:
        self.reports = reports or {}
        self.token_status = token_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3599})
        assert str(request.url.copy_with(query=None)) == REPORTS_URL
        assert request.headers["Authorization"] == f"Bearer {ACCESS}"
        assert request.url.params["ids"] == "channel==MINE"
        answer = self.reports[request.url.params["dimensions"]]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)


def analytics(
    google: Google, *, refresh: str | None = REFRESH, client_id: str = "123"
) -> YouTubeAnalytics:
    return YouTubeAnalytics(
        httpx.Client(transport=httpx.MockTransport(google)),
        OAuthClient(client_id=client_id, client_secret=SecretStr(SECRET)),
        refresh_token=lambda account_id: SecretStr(refresh) if refresh else None,
    )


def test_video_totals_query_and_parse() -> None:
    google = Google(
        {
            "video": {
                "columnHeaders": header(
                    "video",
                    "views",
                    "engagedViews",
                    "averageViewDuration",
                    "averageViewPercentage",
                    "subscribersGained",
                    "subscribersLost",
                    "likes",
                    "comments",
                    "shares",
                ),
                "rows": [["vid00000001", 4000, 3100, 21, 55.5, 12, 1, 300, 20, 15]],
            }
        }
    )

    totals = analytics(google).for_account(ACCOUNT).video_totals(["vid00000001"], START, END)

    assert totals == {"vid00000001": VideoTotals(4000, 3100, 21.0, 55.5, 12, 1, 300, 20, 15)}
    params = google.requests[-1].url.params
    assert params["filters"] == "video==vid00000001"
    assert params["startDate"] == "2026-10-15" and params["endDate"] == "2027-01-15"
    assert params["metrics"].split(",")[:2] == ["views", "engagedViews"]


def test_reports_without_rows_are_no_data_not_an_error() -> None:
    empty = {"columnHeaders": header("video", "views")}  # no "rows": published today
    google = Google(
        {
            "video": empty,
            "day": {"columnHeaders": header("day", "views"), "rows": []},
            "insightTrafficSourceType": empty,
            "elapsedVideoTimeRatio": empty,
        }
    )
    reports = analytics(google).for_account(ACCOUNT)

    assert reports.video_totals(["vid00000001"], START, END) == {}
    assert reports.shorts_days(START, END) == []
    assert reports.traffic("vid00000001", START, END) == {}
    assert reports.retention("vid00000001", START, END) == []


def test_shorts_days_use_the_lowercase_shorts_filter() -> None:
    google = Google(
        {
            "day": {
                "columnHeaders": header("day", "views", "subscribersGained", "subscribersLost"),
                "rows": [["2027-01-12", 1000, 3, 1], ["2027-01-13", 1500, 4, 0]],
            }
        }
    )

    days = analytics(google).for_account(ACCOUNT).shorts_days(START, END)

    assert days == [
        ShortsDay(date(2027, 1, 12), 1000, 3, 1),
        ShortsDay(date(2027, 1, 13), 1500, 4, 0),
    ]
    assert google.requests[-1].url.params["filters"] == "creatorContentType==shorts"


def test_traffic_and_retention() -> None:
    google = Google(
        {
            "insightTrafficSourceType": {
                "columnHeaders": header("insightTrafficSourceType", "views"),
                "rows": [["SHORTS", 900], ["YT_SEARCH", 100]],
            },
            "elapsedVideoTimeRatio": {
                "columnHeaders": header("elapsedVideoTimeRatio", "audienceWatchRatio"),
                "rows": [[0.01, 1.0], [0.5, 0.62], [1.0, 0.4]],
            },
        }
    )
    reports = analytics(google).for_account(ACCOUNT)

    assert reports.traffic("vid00000001", START, END) == {"SHORTS": 900, "YT_SEARCH": 100}
    assert reports.retention("vid00000001", START, END) == [(0.01, 1.0), (0.5, 0.62), (1.0, 0.4)]


@pytest.mark.parametrize(
    "error",
    [
        {"code": 403, "errors": [{"reason": "insufficientPermissions"}]},
        {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                }
            ],
        },
    ],
)
def test_a_token_without_the_analytics_scope_raises_scope_missing(error: dict) -> None:
    google = Google({"day": httpx.Response(403, json={"error": error})})

    with pytest.raises(ScopeMissing) as info:
        analytics(google).for_account(ACCOUNT).shorts_days(START, END)

    assert "yt-analytics.readonly" in str(info.value)
    assert ACCESS not in str(info.value)


def test_other_403s_are_plain_errors() -> None:
    google = Google(
        {"day": httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})}
    )

    with pytest.raises(InsightsError) as info:
        analytics(google).for_account(ACCOUNT).shorts_days(START, END)

    assert not isinstance(info.value, ScopeMissing)


def test_no_refresh_token_names_the_variable_and_sends_nothing() -> None:
    google = Google()

    with pytest.raises(NoCredentials, match="CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT"):
        analytics(google, refresh=None).for_account(ACCOUNT)

    with pytest.raises(NoCredentials, match="YOUTUBE_OAUTH_CLIENT_ID"):
        analytics(google, client_id="").for_account(ACCOUNT)
    assert google.requests == []


def test_a_rejected_refresh_token_raises_credentials_rejected_without_secrets() -> None:
    google = Google(token_status=400)

    with pytest.raises(CredentialsRejected) as info:
        analytics(google).for_account(ACCOUNT)

    assert REFRESH not in str(info.value) and SECRET not in str(info.value)


def test_repr_never_shows_the_token() -> None:
    reports = analytics(Google()).for_account(ACCOUNT)

    assert ACCESS not in repr(reports)
