"""Public channel and Short numbers from the Data API with YOUTUBE_API_KEY (CLP-154).
Every request goes to an httpx MockTransport; nothing reaches Google."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from clipper.insights.public import API_URL, InsightsError, PublicYouTube, Upload, parse_duration

KEY = "AIzaSyRecordedTestKeyValue0000000000000"
CHANNEL = "UCcMYw3Ae2crqXE2yzFBSUig"
UPLOADS = "UUcMYw3Ae2crqXE2yzFBSUig"

Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler) -> tuple[PublicYouTube, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return PublicYouTube(http, SecretStr(KEY)), seen


def path(request: httpx.Request) -> str:
    return str(request.url.copy_with(query=None)).removeprefix(f"{API_URL}/")


def test_channel_reads_statistics_and_the_uploads_playlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert path(request) == "channels"
        assert request.url.params["id"] == CHANNEL
        assert request.url.params["key"] == KEY
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": CHANNEL,
                        "statistics": {
                            "subscriberCount": "320",
                            "viewCount": "50000",
                            "videoCount": "41",
                            "hiddenSubscriberCount": False,
                        },
                        "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                    }
                ]
            },
        )

    public, seen = make(handler)
    stats = public.channel(CHANNEL)

    assert stats is not None
    assert (stats.subscribers, stats.total_views, stats.video_count) == (320, 50000, 41)
    assert stats.uploads_playlist == UPLOADS
    assert public.units == len(seen) == 1


def test_a_hidden_subscriber_count_is_none_and_an_unknown_channel_is_none() -> None:
    answers = iter(
        [
            {"items": [{"statistics": {"subscriberCount": "0", "hiddenSubscriberCount": True}}]},
            {"items": []},
        ]
    )
    public, _ = make(lambda request: httpx.Response(200, json=next(answers)))

    hidden = public.channel(CHANNEL)

    assert hidden is not None and hidden.subscribers is None
    assert public.channel(CHANNEL) is None


def test_uploads_since_follows_pages_and_stops_at_the_window() -> None:
    pages = {
        None: {
            "nextPageToken": "p2",
            "items": [
                {
                    "contentDetails": {
                        "videoId": "vid00000001",
                        "videoPublishedAt": "2027-01-14T18:00:00Z",
                    }
                },
            ],
        },
        "p2": {
            "nextPageToken": "p3",
            "items": [
                {
                    "contentDetails": {
                        "videoId": "vid00000002",
                        "videoPublishedAt": "2026-12-01T18:00:00Z",
                    }
                },
                {
                    "contentDetails": {
                        "videoId": "vid00000003",
                        "videoPublishedAt": "2026-09-01T18:00:00Z",
                    }
                },
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert path(request) == "playlistItems"
        assert request.url.params["playlistId"] == UPLOADS
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    public, seen = make(handler)
    found = public.uploads_since(UPLOADS, datetime(2026, 10, 17, tzinfo=UTC))

    assert found == [
        Upload("vid00000001", datetime(2027, 1, 14, 18, tzinfo=UTC)),
        Upload("vid00000002", datetime(2026, 12, 1, 18, tzinfo=UTC)),
    ]
    assert len(seen) == 2  # page p3 is never asked for


def test_video_stats_batches_50_ids_and_leaves_out_videos_that_are_not_public() -> None:
    ids = [f"vid{n:08d}" for n in range(60)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["part"] == "snippet,statistics,contentDetails"
        asked = request.url.params["id"].split(",")
        assert len(asked) <= 50
        items = [
            {
                "id": vid,
                "snippet": {"title": f"Short {vid}", "publishedAt": "2027-01-14T18:00:00Z"},
                "contentDetails": {"duration": "PT42S"},
                "statistics": {"viewCount": "100", "likeCount": "7", "commentCount": "1"},
            }
            for vid in asked
            if vid != "vid00000003"  # still private: an API key does not see it
        ]
        return httpx.Response(200, json={"items": items})

    public, seen = make(handler)
    stats = public.video_stats([*ids, "not a video id"])

    assert len(seen) == 2
    assert "vid00000003" not in stats and len(stats) == 59
    one = stats["vid00000001"]
    assert (one.views, one.likes, one.comments) == (100, 7, 1)
    assert one.title == "Short vid00000001" and one.duration_s == 42.0
    assert one.published_at == datetime(2027, 1, 14, 18, tzinfo=UTC)


def test_a_hand_upload_and_a_long_video_report_their_durations() -> None:
    body = {
        "items": [
            {
                "id": "2BpbZneO_To",
                "snippet": {"title": "Hand upload\u202e"},
                "contentDetails": {"duration": "PT2M59S"},
                "statistics": {"viewCount": "5"},
            },
            {"id": "longVideo01", "contentDetails": {"duration": "PT1H2M3S"}, "statistics": {}},
        ]
    }
    public, _ = make(lambda request: httpx.Response(200, json=body))

    stats = public.video_stats(["2BpbZneO_To", "longVideo01"])

    assert stats["2BpbZneO_To"].duration_s == 179.0
    assert stats["2BpbZneO_To"].title == "Hand upload"  # control characters dropped
    assert stats["longVideo01"].duration_s == 3723.0 and stats["longVideo01"].views is None


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT42S", 42.0),
        ("PT3M", 180.0),
        ("PT1H0M1S", 3601.0),
        ("P1DT1S", 86401.0),
        ("P0D", 0.0),
        ("PT", None),
        ("42", None),
        (None, None),
    ],
)
def test_parse_duration(value: object, seconds: float | None) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"items": []}, True),
        (403, {"error": {"errors": [{"reason": "commentsDisabled"}]}}, False),
        (404, {"error": {"errors": [{"reason": "videoNotFound"}]}}, None),
    ],
)
def test_comments_enabled(status: int, body: dict, expected: bool | None) -> None:
    public, seen = make(lambda request: httpx.Response(status, json=body))

    assert public.comments_enabled("vid00000001") is expected
    assert path(seen[0]) == "commentThreads"


def test_errors_and_repr_never_carry_the_key() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    public, _ = make(unreachable)
    with pytest.raises(InsightsError) as info:
        public.channel(CHANNEL)
    assert KEY not in str(info.value) and "unreachable" in str(info.value)
    assert KEY not in repr(public)

    bad_key = {
        "error": {
            "message": "API key not valid.",
            "errors": [{"reason": "badRequest"}],
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    public, _ = make(lambda request: httpx.Response(400, json=bad_key))
    with pytest.raises(InsightsError) as info:
        public.channel(CHANNEL)
    assert info.value.status == 400
    assert info.value.reasons == {"badRequest", "API_KEY_INVALID"}
    assert KEY not in str(info.value)
