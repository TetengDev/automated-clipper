"""YouTube source (CLP-23): RSS gate, keyless (yt-dlp) and Data API metadata providers.

HTTP is mocked with respx; yt-dlp runs through a recording fake runner. No network.
"""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime

import httpx
import pytest
import respx

from clipper.config import Secrets
from clipper.pipeline.ytdlp import RunResult, YtDlp
from clipper.sources import youtube as yt
from clipper.sources.youtube import (
    ApiProvider,
    FeedError,
    HandleNotFound,
    SourceError,
    Upload,
    YouTubeSource,
    YtDlpProvider,
)

CHANNEL = "UCX6OQ3DkcsbYNE6H8uQQuVA"
KEY = "AIzaTESTKEY1234567890"
V1, V2, V3 = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def feed_xml(*entries: tuple[str, str]) -> str:
    items = "".join(
        f"""<entry>
    <id>yt:video:{vid}</id>
    <yt:videoId>{vid}</yt:videoId>
    <yt:channelId>{CHANNEL}</yt:channelId>
    <title>Video {vid}</title>
    <published>{published}</published>
    <updated>{published}</updated>
  </entry>"""
        for vid, published in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>{CHANNEL}</yt:channelId>
  <title>MrBeast</title>
  {items}
</feed>"""


class FakeRunner:
    """Answers yt-dlp calls by URL; records every argument list."""

    def __init__(self, answers: dict[str, RunResult | Callable[[], RunResult]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout_s: float) -> RunResult:
        self.calls.append(list(args))
        answer = self.answers[args[-1]]
        return answer() if callable(answer) else answer

    def urls(self) -> list[str]:
        return [c[-1] for c in self.calls]


def ok_json(data: object) -> RunResult:
    return RunResult(0, json.dumps(data), "")


def watch(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def info(vid: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": vid,
        "title": f"Title {vid}",
        "duration": 1200,
        "live_status": "not_live",
        "timestamp": 1792252818,
        "upload_date": "20261017",
    }
    return {**base, **extra}


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


# --- RSS gate -------------------------------------------------------------------------


@respx.mock
def test_feed_lists_uploads_with_published_time(http: httpx.Client) -> None:
    route = respx.get(yt.FEED_URL, params={"channel_id": CHANNEL}).mock(
        return_value=httpx.Response(
            200, text=feed_xml((V1, "2026-10-17T15:00:18+00:00"), (V2, "2026-10-10T12:00:00+00:00"))
        )
    )

    uploads = yt.feed_uploads(http, CHANNEL)

    assert route.called
    assert uploads == [
        Upload(V1, datetime(2026, 10, 17, 15, 0, 18, tzinfo=UTC)),
        Upload(V2, datetime(2026, 10, 10, 12, 0, tzinfo=UTC)),
    ]


@respx.mock
def test_feed_with_nothing_new_is_an_empty_list(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(200, text=feed_xml()))

    assert yt.feed_uploads(http, CHANNEL) == []


@respx.mock
def test_feed_skips_entries_with_invalid_video_ids(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(
        return_value=httpx.Response(
            200, text=feed_xml(("../etc", "2026-10-17T15:00:18+00:00"), (V1, "garbage"))
        )
    )

    assert yt.feed_uploads(http, CHANNEL) == [Upload(V1, None)]


@pytest.mark.parametrize("status", [404, 500, 503])
@respx.mock
def test_feed_http_error_raises_feed_error(http: httpx.Client, status: int) -> None:
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(status, text="<html>no</html>"))

    with pytest.raises(FeedError, match=str(status)):
        yt.feed_uploads(http, CHANNEL)


@respx.mock
def test_feed_timeout_raises_feed_error(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(FeedError, match="ReadTimeout"):
        yt.feed_uploads(http, CHANNEL)


@pytest.mark.parametrize(
    "body",
    [
        "<feed><entry>",  # truncated
        "not xml at all",
        '<?xml version="1.0"?><rss/>',  # wrong root
    ],
)
@respx.mock
def test_malformed_feed_raises_feed_error(http: httpx.Client, body: str) -> None:
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(FeedError):
        yt.feed_uploads(http, CHANNEL)


@respx.mock
def test_feed_rejects_entity_expansion(http: httpx.Client) -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>'
    )
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(200, text=bomb))

    with pytest.raises(FeedError, match="malformed"):
        yt.feed_uploads(http, CHANNEL)


@respx.mock
def test_oversized_feed_raises_feed_error(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(
        return_value=httpx.Response(200, content=b" " * (yt.FEED_MAX_BYTES + 1))
    )

    with pytest.raises(FeedError, match="too large"):
        yt.feed_uploads(http, CHANNEL)


# --- keyless provider (yt-dlp) ----------------------------------------------------------


def test_ytdlp_provider_fetches_metadata_per_new_id() -> None:
    runner = FakeRunner(
        {
            watch(V1): ok_json(info(V1)),
            watch(V2): ok_json(info(V2, duration=59, live_status="was_live")),
        }
    )
    provider = YtDlpProvider(YtDlp(runner=runner))

    meta = provider.videos([V1, V2])

    assert runner.urls() == [watch(V1), watch(V2)]
    assert all("--skip-download" in c and "--no-playlist" in c for c in runner.calls)
    assert meta.videos[V1] == yt.VideoMeta(
        V1, f"Title {V1}", 1200, "not_live", datetime(2026, 10, 17, 16, 0, 18, tzinfo=UTC)
    )
    assert meta.videos[V2].duration_s == 59
    assert meta.videos[V2].live_status == "was_live"
    assert provider.quota.units == 0


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({"live_status": "is_live", "duration": None}, "is_live"),
        ({"live_status": "post_live"}, "post_live"),
        ({"live_status": None, "is_live": True}, "is_live"),
        ({"live_status": None}, "not_live"),
        ({"live_status": "weird"}, "not_live"),
    ],
)
def test_ytdlp_provider_maps_live_status(extra: dict[str, object], status: str) -> None:
    runner = FakeRunner({watch(V1): ok_json(info(V1, **extra))})

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1])

    assert meta.videos[V1].live_status == status


def test_ytdlp_provider_upcoming_event_error_is_upcoming() -> None:
    runner = FakeRunner(
        {watch(V1): RunResult(1, "", "ERROR: [youtube] x: This live event will begin in 3 hours.")}
    )

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1])

    assert meta.videos[V1].live_status == "is_upcoming"


def test_ytdlp_provider_exact_timestamp_is_not_day_precision() -> None:
    runner = FakeRunner({watch(V1): ok_json(info(V1))})

    assert YtDlpProvider(YtDlp(runner=runner)).videos([V1]).videos[V1].day_precision is False


def test_ytdlp_provider_falls_back_to_upload_date_and_drops_bad_duration() -> None:
    runner = FakeRunner(
        {watch(V1): ok_json(info(V1, timestamp=None, upload_date="20261017", duration="x"))}
    )

    video = YtDlpProvider(YtDlp(runner=runner)).videos([V1]).videos[V1]

    assert video.published == datetime(2026, 10, 17, tzinfo=UTC)
    assert video.day_precision is True
    assert video.duration_s is None


def test_ytdlp_provider_separates_unavailable_and_retryable_errors() -> None:
    runner = FakeRunner(
        {
            watch(V1): RunResult(1, "", "ERROR: [youtube] x: Private video. Sign in"),
            watch(V2): RunResult(1, "", "ERROR: unable to download: Connection reset by peer"),
            watch(V3): ok_json(info(V3)),
        }
    )

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1, V2, V3])

    assert meta.unavailable == {V1}
    assert set(meta.errors) == {V2}
    assert "Connection reset" in meta.errors[V2]
    assert set(meta.videos) == {V3}


def test_ytdlp_provider_stops_between_videos_when_asked() -> None:
    runner = FakeRunner({watch(V1): ok_json(info(V1)), watch(V2): ok_json(info(V2))})

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1, V2], should_stop=lambda: True)

    assert runner.calls == []
    assert meta.videos == {} and meta.errors == {} and meta.unavailable == set()


def test_ytdlp_provider_lists_channel_videos_tab_as_fallback() -> None:
    url = f"https://www.youtube.com/channel/{CHANNEL}/videos"
    entries = [{"id": V1, "timestamp": 1792281600}, {"id": "bad"}, {"id": V2, "timestamp": True}]
    runner = FakeRunner({url: ok_json({"channel_id": CHANNEL, "entries": entries})})

    uploads = YtDlpProvider(YtDlp(runner=runner)).uploads(CHANNEL)

    assert uploads == [Upload(V1, datetime(2026, 10, 18, tzinfo=UTC)), Upload(V2)]
    args = runner.calls[0]
    assert "--flat-playlist" in args
    assert args[args.index("--playlist-items") + 1] == f"1:{yt.FALLBACK_ITEMS}"
    # "N weeks ago" -> a date never earlier than the real one, so the first-run lookback
    # filter cannot drop a recent upload on it.
    assert args[args.index("--extractor-args") + 1] == "youtubetab:approximate_date"
    assert args[-2:] == ["--", url]


def test_ytdlp_provider_resolves_handle_to_channel_id() -> None:
    url = "https://www.youtube.com/@MrBeast"
    runner = FakeRunner({url: ok_json({"id": "@MrBeast", "channel_id": CHANNEL})})

    assert YtDlpProvider(YtDlp(runner=runner)).channel_id("@MrBeast") == CHANNEL
    args = runner.calls[0]
    assert args[args.index("--playlist-items") + 1] == "0"


@pytest.mark.parametrize(
    "result",
    [
        RunResult(1, "", "ERROR: [youtube:tab] @Nope: Unable to download API page: HTTP Error 404"),
        RunResult(0, json.dumps({"id": "@Nope"}), ""),
        RunResult(0, json.dumps({"id": "@Nope", "channel_id": "not-a-channel"}), ""),
    ],
)
def test_ytdlp_provider_unknown_handle(result: RunResult) -> None:
    runner = FakeRunner({"https://www.youtube.com/@Nope": result})

    with pytest.raises(HandleNotFound, match="@Nope"):
        YtDlpProvider(YtDlp(runner=runner)).channel_id("@Nope")


def test_ytdlp_provider_handle_lookup_network_error_is_not_not_found() -> None:
    runner = FakeRunner({"https://www.youtube.com/@Nope": RunResult(1, "", "ERROR: timed out")})

    with pytest.raises(SourceError) as info_:
        YtDlpProvider(YtDlp(runner=runner)).channel_id("@Nope")
    assert not isinstance(info_.value, HandleNotFound)


# --- Data API provider ------------------------------------------------------------------


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=yt.API_BASE, assert_all_called=False) as mock:
        # search.list costs 100 units; it must never be called.
        mock.route(name="search", path__startswith="/search").mock(
            side_effect=AssertionError("search.list must never be called")
        )
        yield mock


def api_video(vid: str, duration: str = "PT20M", live: str = "none", **extra: object) -> dict:
    item: dict[str, object] = {
        "id": vid,
        "snippet": {
            "title": f"Title {vid}",
            "publishedAt": "2026-10-17T15:00:18Z",
            "liveBroadcastContent": live,
        },
        "contentDetails": {"duration": duration},
    }
    return {**item, **extra}


def test_api_videos_are_batched_fifty_per_call(api: respx.MockRouter, http: httpx.Client) -> None:
    ids = [f"{i:011d}" for i in range(120)]

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Goog-Api-Key"] == KEY
        assert "key" not in request.url.params
        assert request.url.params["part"] == "snippet,contentDetails,liveStreamingDetails"
        batch = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [api_video(v) for v in batch]})

    route = api.get("/videos").mock(side_effect=answer)
    provider = ApiProvider(http, KEY)

    meta = provider.videos(ids)

    assert route.call_count == 3
    sizes = [len(c.request.url.params["id"].split(",")) for c in route.calls]
    assert sizes == [50, 50, 20]
    assert set(meta.videos) == set(ids)
    assert provider.quota.units == 3


def test_api_video_fields_and_missing_ids(api: respx.MockRouter, http: httpx.Client) -> None:
    api.get("/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    api_video(V1, "PT1H2M3S"),
                    api_video(V2, "P0D", live="upcoming"),
                ]
            },
        )
    )

    meta = ApiProvider(http, KEY).videos([V1, V2, V3])

    assert meta.videos[V1] == yt.VideoMeta(
        V1, f"Title {V1}", 3723, "not_live", datetime(2026, 10, 17, 15, 0, 18, tzinfo=UTC)
    )
    assert meta.videos[V2].live_status == "is_upcoming"
    assert meta.unavailable == {V3}  # private/removed videos are left out of the response


@pytest.mark.parametrize(
    ("item", "status"),
    [
        (api_video(V1, live="live"), "is_live"),
        (api_video(V1, "P0D", liveStreamingDetails={"actualEndTime": "x"}), "post_live"),
        (api_video(V1, "PT3H", liveStreamingDetails={"actualEndTime": "x"}), "was_live"),
        (api_video(V1, "PT3H"), "not_live"),
    ],
)
def test_api_live_status(
    api: respx.MockRouter, http: httpx.Client, item: dict, status: str
) -> None:
    api.get("/videos").mock(return_value=httpx.Response(200, json={"items": [item]}))

    assert ApiProvider(http, KEY).videos([V1]).videos[V1].live_status == status


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("PT45S", 45),
        ("PT1H", 3600),
        ("PT10M5S", 605),
        ("P1DT2H", 93600),
        ("P0D", 0),
        ("garbage", None),
        ("", None),
    ],
)
def test_iso_duration(text: str, seconds: int | None) -> None:
    assert yt.iso_duration(text) == seconds


def test_api_error_is_redacted_and_counted(api: respx.MockRouter, http: httpx.Client) -> None:
    api.get("/videos").mock(
        return_value=httpx.Response(
            403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "over"}}
        )
    )
    provider = ApiProvider(http, KEY)

    with pytest.raises(SourceError, match="403.*quotaExceeded") as info_:
        provider.videos([V1])
    assert KEY not in str(info_.value)
    assert provider.quota.units == 1


def test_api_transport_error_never_shows_the_key(api: respx.MockRouter, http: httpx.Client) -> None:
    api.get("/videos").mock(side_effect=httpx.ConnectError(f"boom ?key={KEY}"))

    with pytest.raises(SourceError) as info_:
        ApiProvider(http, KEY).videos([V1])
    assert KEY not in str(info_.value)


def test_api_uploads_use_long_form_playlist(api: respx.MockRouter, http: httpx.Client) -> None:
    route = api.get("/playlistItems").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"contentDetails": {"videoId": V1, "videoPublishedAt": "2026-10-17T15:00:18Z"}},
                    {"contentDetails": {"videoId": "bad"}},
                ]
            },
        )
    )
    provider = ApiProvider(http, KEY)

    uploads = provider.uploads(CHANNEL)

    assert uploads == [Upload(V1, datetime(2026, 10, 17, 15, 0, 18, tzinfo=UTC))]
    assert route.calls[0].request.url.params["playlistId"] == "UULF" + CHANNEL[2:]
    assert provider.quota.units == 1


def test_api_uploads_fall_back_to_all_uploads_on_404(
    api: respx.MockRouter, http: httpx.Client
) -> None:
    long_form = "UULF" + CHANNEL[2:]
    all_uploads = "UU" + CHANNEL[2:]
    api.get("/playlistItems", params={"playlistId": long_form}).mock(
        return_value=httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
    )
    route = api.get("/playlistItems", params={"playlistId": all_uploads}).mock(
        return_value=httpx.Response(200, json={"items": [{"contentDetails": {"videoId": V2}}]})
    )
    provider = ApiProvider(http, KEY)

    assert provider.uploads(CHANNEL) == [Upload(V2)]
    assert route.called
    assert provider.quota.units == 2


def test_api_resolves_handle_with_channels_list(api: respx.MockRouter, http: httpx.Client) -> None:
    route = api.get("/channels").mock(
        return_value=httpx.Response(200, json={"items": [{"id": CHANNEL}]})
    )
    provider = ApiProvider(http, KEY)

    assert provider.channel_id("@MrBeast") == CHANNEL
    assert route.calls[0].request.url.params["forHandle"] == "@MrBeast"
    assert provider.quota.units == 1


def test_api_unknown_handle(api: respx.MockRouter, http: httpx.Client) -> None:
    api.get("/channels").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(HandleNotFound, match="@Nope"):
        ApiProvider(http, KEY).channel_id("@Nope")


def test_api_provider_never_calls_search(api: respx.MockRouter, http: httpx.Client) -> None:
    api.get("/videos").mock(return_value=httpx.Response(200, json={"items": [api_video(V1)]}))
    api.get("/playlistItems").mock(return_value=httpx.Response(200, json={"items": []}))
    api.get("/channels").mock(return_value=httpx.Response(200, json={"items": [{"id": CHANNEL}]}))
    provider = ApiProvider(http, KEY)

    provider.channel_id("@MrBeast")
    provider.uploads(CHANNEL)
    provider.videos([V1])

    assert not api.routes["search"].called
    assert all("/search" not in str(call.request.url) for call in api.calls)


# --- source: RSS first, fallback listing ------------------------------------------------


class StubProvider:
    def __init__(self, uploads: list[Upload] | Exception) -> None:
        self._uploads = uploads
        self.quota = yt.QuotaMeter()
        self.listed: list[str] = []

    def uploads(self, channel_id: str) -> list[Upload]:
        self.listed.append(channel_id)
        if isinstance(self._uploads, Exception):
            raise self._uploads
        return self._uploads

    def videos(self, ids, should_stop=None):  # pragma: no cover - not used here
        raise AssertionError

    def channel_id(self, handle):  # pragma: no cover - not used here
        raise AssertionError


@respx.mock
def test_source_uses_feed_and_never_the_fallback_when_feed_works(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(
        return_value=httpx.Response(200, text=feed_xml((V1, "2026-10-17T15:00:18+00:00")))
    )
    provider = StubProvider([Upload(V2)])

    uploads = YouTubeSource(http, provider).recent_uploads(CHANNEL)

    assert [u.video_id for u in uploads] == [V1]
    assert provider.listed == []
    assert YouTubeSource(http, provider).quota_units == 0


@pytest.mark.parametrize(
    "mock",
    [
        {"return_value": httpx.Response(500)},
        {"return_value": httpx.Response(404)},
        {"return_value": httpx.Response(200, text="<broken")},
        {"side_effect": httpx.ConnectTimeout("slow")},
    ],
)
@respx.mock
def test_source_falls_back_when_feed_fails(http: httpx.Client, mock: dict) -> None:
    respx.get(yt.FEED_URL).mock(**mock)
    provider = StubProvider([Upload(V2)])

    uploads = YouTubeSource(http, provider).recent_uploads(CHANNEL)

    assert uploads == [Upload(V2)]
    assert provider.listed == [CHANNEL]


@respx.mock
def test_source_raises_when_feed_and_fallback_fail(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(500))
    provider = StubProvider(SourceError("listing failed"))

    with pytest.raises(SourceError, match="feed.*500.*listing failed"):
        YouTubeSource(http, provider).recent_uploads(CHANNEL)


def test_cancelled_listing_raises_source_cancelled() -> None:
    from clipper.pipeline.ytdlp import CommandCancelled

    def cancelled(args, timeout_s):
        raise CommandCancelled("python cancelled")

    with pytest.raises(yt.SourceCancelled):
        YtDlpProvider(YtDlp(runner=cancelled)).uploads(CHANNEL)


@respx.mock
def test_source_does_not_rewrap_a_cancelled_fallback(http: httpx.Client) -> None:
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(500))
    provider = StubProvider(yt.SourceCancelled("yt-dlp listing cancelled"))

    with pytest.raises(yt.SourceCancelled):
        YouTubeSource(http, provider).recent_uploads(CHANNEL)


def test_build_source_is_keyless_without_api_key(http: httpx.Client) -> None:
    source = yt.build_source(Secrets(), http, YtDlp(runner=FakeRunner({})))

    assert isinstance(source.provider, YtDlpProvider)


def test_build_source_uses_api_with_key(http: httpx.Client) -> None:
    source = yt.build_source(Secrets(youtube_api_key=KEY), http, YtDlp(runner=FakeRunner({})))

    assert isinstance(source.provider, ApiProvider)


# --- review fixes -------------------------------------------------------------------------


def test_api_key_travels_in_a_header_and_never_reaches_the_httpx_log(
    api: respx.MockRouter, http: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    route = api.get("/videos").mock(
        return_value=httpx.Response(200, json={"items": [api_video(V1)]})
    )
    api.get("/channels").mock(return_value=httpx.Response(200, json={"items": [{"id": CHANNEL}]}))

    with caplog.at_level(logging.INFO, logger="httpx"):
        provider = ApiProvider(http, KEY)
        provider.videos([V1])
        provider.channel_id("@MrBeast")

    request = route.calls[0].request
    assert request.headers["X-Goog-Api-Key"] == KEY
    assert "key" not in request.url.params
    assert "HTTP Request" in caplog.text  # httpx did log the request line ...
    assert KEY not in caplog.text  # ... without the key


def test_cli_logging_keeps_httpx_request_lines_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper import cli

    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    # What basicConfig(level=INFO) does to the root logger, without touching handlers.
    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)

    cli._setup_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader",
        "ERROR: [youtube] x: Join this channel to get access to members-only content",
    ],
)
def test_only_private_removed_and_members_only_are_unavailable(
    stderr: str, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner({watch(V1): RunResult(1, "", stderr)})

    with caplog.at_level(logging.WARNING):
        meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1])

    assert meta.unavailable == {V1} and meta.errors == {}
    assert f"{V1}: unavailable" in caplog.text


@pytest.mark.parametrize(
    "stderr",
    [
        # Also what a YouTube player / n-sig breakage looks like: retry, never give up.
        "ERROR: [youtube] x: Requested format is not available. Use --list-formats",
        "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate",
    ],
)
def test_format_and_age_errors_are_retryable_not_unavailable(stderr: str) -> None:
    runner = FakeRunner({watch(V1): RunResult(1, "", stderr)})

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1])

    assert meta.unavailable == set()
    assert set(meta.errors) == {V1}


FORMAT_ERROR = "ERROR: [youtube] x: Requested format is not available. Use --list-formats"
AUDIO = {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "url": "https://x/140"}


def test_format_error_stops_the_batch_and_is_never_reprobed() -> None:
    # A player breakage (SABR, n-sig) hits every video alike: one probe tells.
    runner = FakeRunner({watch(V1): RunResult(1, "", FORMAT_ERROR), watch(V2): ok_json(info(V2))})

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1, V2])

    assert runner.urls() == [watch(V1)]  # V2 waits for the next poll
    assert set(meta.errors) == {V1} and "Requested format" in meta.errors[V1]
    assert meta.format_errors == {V1}
    assert meta.videos == {} and meta.unavailable == set() and meta.blocked == set()
    # With the flag, the YouTube extractor appends storyboard formats after its
    # playability check, so a private or bot-checked video would look fetchable.
    assert all("--ignore-no-formats-error" not in call for call in runner.calls)


def test_bot_check_stops_probing_the_rest_of_the_batch() -> None:
    runner = FakeRunner(
        {
            watch(V1): RunResult(1, "", "ERROR: [youtube] x: Sign in to confirm you're not a bot"),
            watch(V2): ok_json(info(V2)),
        }
    )

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1, V2])

    assert runner.urls() == [watch(V1)]  # V2 waits for the next poll
    assert set(meta.errors) == {V1} and meta.blocked == {V1}
    assert meta.videos == {}


def test_metadata_probes_never_ignore_missing_formats() -> None:
    # With --ignore-no-formats-error, yt-dlp turns private, members-only, age-gate and bot
    # check failures into a warning and exits 0, so none of them would ever be classified.
    url = f"https://www.youtube.com/channel/{CHANNEL}/videos"
    runner = FakeRunner({watch(V1): ok_json(info(V1)), url: ok_json({"entries": []})})
    provider = YtDlpProvider(YtDlp(runner=runner))

    provider.videos([V1])
    provider.uploads(CHANNEL)

    probe, listing = runner.calls
    assert "--ignore-no-formats-error" not in probe
    assert "--ignore-no-formats-error" in listing  # harmless with --flat-playlist


@pytest.mark.parametrize("availability", ["needs_auth", "subscriber_only", "premium_only"])
def test_probe_listing_formats_is_fetchable_whatever_its_availability(availability: str) -> None:
    # With CLIPPER_YTDLP_COOKIES, an 18+ or members' video the account can watch exits 0
    # with formats: it is fetchable, so it is returned, not retried or dropped.
    body = info(V1, formats=[AUDIO], availability=availability)
    runner = FakeRunner({watch(V1): ok_json(body)})

    meta = YtDlpProvider(YtDlp(runner=runner)).videos([V1])

    assert set(meta.videos) == {V1}
    assert meta.errors == {} and meta.unavailable == set()


def test_pipeline_probe_still_fails_on_missing_formats() -> None:
    runner = FakeRunner({watch(V1): ok_json(info(V1))})

    YtDlp(runner=runner).probe(watch(V1))

    assert "--ignore-no-formats-error" not in runner.calls[0]


def test_lookalike_channel_id_from_ytdlp_is_rejected() -> None:
    lookalike = "UCХ6OQ3DkcsbYNE6H8uQQuVA"  # Cyrillic Ha instead of X
    runner = FakeRunner(
        {"https://www.youtube.com/@MrBeast": ok_json({"id": "@MrBeast", "channel_id": lookalike})}
    )

    with pytest.raises(HandleNotFound):
        YtDlpProvider(YtDlp(runner=runner)).channel_id("@MrBeast")
    with pytest.raises(ValueError, match="invalid channel id"):
        YtDlpProvider(YtDlp(runner=runner)).uploads(lookalike)


class Endless:
    """A streamed body of 1 MiB chunks; counts how many were pulled."""

    def __init__(self, chunks: int = 100) -> None:
        self.chunks = chunks
        self.pulled = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.pulled += 1
            yield b" " * (1 << 20)


@respx.mock
def test_oversized_streamed_feed_is_cut_off_early(http: httpx.Client) -> None:
    body = Endless()
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(200, content=body))

    with pytest.raises(FeedError, match="too large"):
        yt.feed_uploads(http, CHANNEL)
    assert body.pulled <= yt.FEED_MAX_BYTES // (1 << 20) + 1


def test_oversized_api_response_is_cut_off_early(api: respx.MockRouter, http: httpx.Client) -> None:
    body = Endless()
    api.get("/videos").mock(return_value=httpx.Response(200, content=body))

    with pytest.raises(SourceError, match="too large"):
        ApiProvider(http, KEY).videos([V1])
    assert body.pulled <= yt.API_MAX_BYTES // (1 << 20) + 1


@pytest.mark.parametrize("error", [httpx.InvalidURL("bad"), httpx.StreamConsumed()])
@respx.mock
def test_any_httpx_failure_of_the_feed_is_a_feed_error(
    http: httpx.Client, error: Exception
) -> None:
    respx.get(yt.FEED_URL).mock(side_effect=error)

    with pytest.raises(FeedError, match=type(error).__name__) as info_:
        yt.feed_uploads(http, CHANNEL)
    assert info_.value.__cause__ is None and info_.value.__suppress_context__


@pytest.mark.parametrize("error", [httpx.InvalidURL("bad"), httpx.StreamConsumed()])
def test_any_httpx_failure_of_the_api_is_a_source_error(
    api: respx.MockRouter, http: httpx.Client, error: Exception
) -> None:
    api.get("/videos").mock(side_effect=error)

    with pytest.raises(SourceError, match=type(error).__name__) as info_:
        ApiProvider(http, KEY).videos([V1])
    assert info_.value.__suppress_context__


class Ticking:
    """A clock that moves `step` seconds every time it is read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@respx.mock
def test_trickling_response_is_cut_off_at_the_deadline(http: httpx.Client) -> None:
    body = Endless()  # every chunk "takes" 2 s on the ticking clock
    respx.get(yt.FEED_URL).mock(return_value=httpx.Response(200, content=body))

    with pytest.raises(SourceError, match="took longer than"):
        yt.get_capped(http, yt.FEED_URL, params={}, limit=1 << 40, what="feed", clock=Ticking(2.0))
    assert body.pulled <= yt.HTTP_DEADLINE_S // 2 + 1
