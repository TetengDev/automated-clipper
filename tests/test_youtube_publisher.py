"""YouTube Shorts publisher (CLP-93): resumable `videos.insert`, private + `publishAt`,
OAuth refresh, invalid-grant pause, quota/upload-limit deferral and `lookup`.

Every HTTP exchange is mocked with respx using recorded response shapes from
`tests/fixtures/youtube_publish.json`; respx refuses any request that is not mocked, so
no test reaches Google.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from clipper.accounts import Account, AccountsConfig
from clipper.config import Secrets
from clipper.posting.models import Post, PostMetadata, PostStatus, PublishDeferred, Publisher
from clipper.posting.publishers import build_publishers
from clipper.posting.youtube import (
    API_URL,
    CATEGORY_ENTERTAINMENT,
    TOKEN_URL,
    UPLOAD_URL,
    OAuthClient,
    YouTubePublisher,
    idempotency_tag,
    next_quota_reset,
    video_resource,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "youtube_publish.json").read_text(encoding="utf-8")
)
T0 = datetime(2027, 1, 15, 1, 0, tzinfo=UTC)
KEY = "0123456789abcdef0123456789abcdef"
REFRESH = "1//recorded-refresh-token-value"
CLIENT_SECRET = "GOCSPX-recorded-client-secret"
ACCESS = FIXTURES["token"]["access_token"]
SESSION = f"{UPLOAD_URL}?uploadType=resumable&part=snippet%2Cstatus&upload_id=ABCsession123"
CHUNK = 256 * 1024


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def make_post(
    *, account: str = "yt_main", at: datetime = T0 + timedelta(hours=2), key: str = KEY
) -> Post:
    return Post(
        id=7,
        clip_id=3,
        clip_version=1,
        account_id=account,
        platform="youtube",
        status=PostStatus.UPLOADING,
        scheduled_at=at,
        attempts=0,
        idempotency_key=key,
        video_path="youtube_1/clip_3_v1.mp4",
    )


META = PostMetadata(
    title="Chair falls",
    caption="He did not see it coming",
    hashtags=["#funny", "#fail", "#mrbeast"],
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 1000)
    return path


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


class Tokens:
    """Refresh tokens by account id, changeable mid-test (a re-authorised account)."""

    def __init__(self, **tokens: str | None) -> None:
        self.tokens = {"yt_main": REFRESH, **tokens}

    def __call__(self, account_id: str) -> SecretStr | None:
        value = self.tokens.get(account_id)
        return SecretStr(value) if value else None


def publisher(
    clock: Clock,
    *,
    accounts: tuple[str, ...] = ("yt_main",),
    tokens: Tokens | None = None,
    client: OAuthClient | None = None,
    sleeps: list[float] | None = None,
    chunk_size: int = CHUNK,
) -> YouTubePublisher:
    slept = sleeps if sleeps is not None else []
    return YouTubePublisher(
        account_ids=accounts,
        client=client
        or OAuthClient(
            client_id="123-recorded.apps.googleusercontent.com",
            client_secret=SecretStr(CLIENT_SECRET),
        ),
        refresh_token=tokens or Tokens(),
        http=httpx.Client(),
        clock=clock,
        sleep=slept.append,
        chunk_size=chunk_size,
    )


def ok_token(router: respx.MockRouter, *, body: dict[str, Any] | None = None) -> respx.Route:
    return router.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=body or FIXTURES["token"])
    )


def ok_session(router: respx.MockRouter) -> respx.Route:
    return router.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": SESSION})
    )


def ok_upload(router: respx.MockRouter) -> respx.Route:
    return router.put(SESSION).mock(
        return_value=httpx.Response(201, json=FIXTURES["video_inserted"])
    )


# --- the upload --------------------------------------------------------------------------


def test_approved_clip_is_uploaded_private_with_publish_at(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    token = ok_token(google)
    session = ok_session(google)
    upload = ok_upload(google)
    post = make_post()

    result = publisher(clock).publish(post, video, META)

    assert result.external_id == "Ks-_Mh1QhMc"
    assert result.url == "https://www.youtube.com/shorts/Ks-_Mh1QhMc"
    # Refresh-token grant, with the client credentials.
    form = dict(httpx.QueryParams(token.calls.last.request.content.decode()))
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
        "client_id": "123-recorded.apps.googleusercontent.com",
        "client_secret": CLIENT_SECRET,
    }
    # Resumable session: metadata body, upload headers, bearer token.
    started = session.calls.last.request
    assert started.url.params["uploadType"] == "resumable"
    assert started.url.params["part"] == "snippet,status"
    assert started.headers["Authorization"] == f"Bearer {ACCESS}"
    assert started.headers["X-Upload-Content-Length"] == str(video.stat().st_size)
    assert started.headers["X-Upload-Content-Type"] == "video/mp4"
    body = json.loads(started.content)
    assert body["status"] == {
        "privacyStatus": "private",
        "publishAt": "2027-01-15T03:00:00Z",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
    }
    assert body["snippet"]["categoryId"] == CATEGORY_ENTERTAINMENT == "24"
    assert body["snippet"]["title"] == "Chair falls"
    assert body["snippet"]["description"] == "He did not see it coming\n\n#funny #fail #mrbeast"
    assert body["snippet"]["tags"] == [idempotency_tag(KEY)]
    # The bytes go to the session URI.
    sent = upload.calls.last.request
    assert sent.content == video.read_bytes()
    assert sent.headers["Authorization"] == f"Bearer {ACCESS}"


def test_publish_at_in_the_past_is_still_sent_private(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    """A retry or a deferred post is uploaded after its slot. YouTube publishes a private
    video whose publishAt is in the past right away (videos resource, status.publishAt)."""
    ok_token(google)
    session = ok_session(google)
    ok_upload(google)

    publisher(clock).publish(make_post(at=T0 - timedelta(minutes=10)), video, META)

    body = json.loads(session.calls.last.request.content)
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2027-01-15T00:50:00Z"


def test_ai_elements_set_contains_synthetic_media(clock: Clock) -> None:
    meta = PostMetadata(title="t", caption="c", flags={"ai_generated": True})
    assert video_resource(make_post(), meta)["status"]["containsSyntheticMedia"] is True


def test_title_is_capped_at_100_characters_without_angle_brackets() -> None:
    meta = PostMetadata(title="<b>Wow</b> " + "x" * 200, caption="")
    title = video_resource(make_post(), meta)["snippet"]["title"]
    assert len(title) <= 100
    assert "<" not in title and ">" not in title
    assert title.startswith("bWow/b x")
    assert title.endswith("…")


def test_blank_title_falls_back_to_the_caption() -> None:
    meta = PostMetadata(title="  ", caption="First line of caption\nsecond line")
    assert video_resource(make_post(), meta)["snippet"]["title"] == "First line of caption"


def test_no_title_and_no_caption_is_refused() -> None:
    with pytest.raises(ValueError, match="title"):
        video_resource(make_post(), PostMetadata(title="", caption=""))


def test_description_keeps_at_most_five_hashtags_and_fits_5000_bytes() -> None:
    tags = ["funny", "#fail", "#a", "#b", "#c", "#d", "#bad tag!"]
    meta = PostMetadata(title="t", caption="é" * 4000 + " <script>", hashtags=tags)
    description = video_resource(make_post(), meta)["snippet"]["description"]
    assert len(description.encode("utf-8")) <= 5000
    assert description.endswith("\n\n#funny #fail #a #b #c")
    assert "<" not in description and ">" not in description


def test_upload_is_sent_in_chunks_and_resumed_after_a_server_error(
    clock: Clock, tmp_path: Path, google: respx.MockRouter
) -> None:
    video = tmp_path / "big.mp4"
    data = bytes(range(256)) * (CHUNK * 2 // 256) + b"tail"
    video.write_bytes(data)
    total = len(data)
    ok_token(google)
    ok_session(google)
    seen: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        content_range = request.headers["Content-Range"]
        seen.append(content_range)
        if content_range == f"bytes 0-{CHUNK - 1}/{total}":
            return httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"})
        if content_range == f"bytes {CHUNK}-{2 * CHUNK - 1}/{total}" and len(seen) == 2:
            return httpx.Response(503)
        if content_range == f"bytes */{total}":
            return httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"})
        if content_range == f"bytes {CHUNK}-{2 * CHUNK - 1}/{total}":
            return httpx.Response(308, headers={"Range": f"bytes=0-{2 * CHUNK - 1}"})
        assert content_range == f"bytes {2 * CHUNK}-{total - 1}/{total}"
        assert request.content == b"tail"
        return httpx.Response(201, json=FIXTURES["video_inserted"])

    google.put(SESSION).mock(side_effect=server)
    sleeps: list[float] = []

    result = publisher(clock, sleeps=sleeps).publish(make_post(), video, META)

    assert result.external_id == "Ks-_Mh1QhMc"
    assert seen == [
        f"bytes 0-{CHUNK - 1}/{total}",
        f"bytes {CHUNK}-{2 * CHUNK - 1}/{total}",
        f"bytes */{total}",
        f"bytes {CHUNK}-{2 * CHUNK - 1}/{total}",
        f"bytes {2 * CHUNK}-{total - 1}/{total}",
    ]
    assert len(sleeps) == 1


def test_a_dropped_connection_mid_upload_is_resumed(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    ok_session(google)
    total = video.stat().st_size
    responses = iter(
        [
            httpx.ConnectError("connection reset"),
            httpx.Response(308),  # status query: nothing received yet
            httpx.Response(201, json=FIXTURES["video_inserted"]),
        ]
    )

    def server(request: httpx.Request) -> httpx.Response:
        nxt = next(responses)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    route = google.put(SESSION).mock(side_effect=server)

    result = publisher(clock).publish(make_post(), video, META)

    assert result.external_id == "Ks-_Mh1QhMc"
    ranges = [c.request.headers["Content-Range"] for c in route.calls]
    assert ranges == [
        f"bytes 0-{total - 1}/{total}",
        f"bytes */{total}",
        f"bytes 0-{total - 1}/{total}",
    ]


def test_server_errors_that_never_clear_fail_the_attempt(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    ok_session(google)
    google.put(SESSION).mock(return_value=httpx.Response(503))

    with pytest.raises(RuntimeError, match="503"):
        publisher(clock).publish(make_post(), video, META)


def test_an_expired_upload_session_fails_the_attempt(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    ok_session(google)
    google.put(SESSION).mock(return_value=httpx.Response(404))

    with pytest.raises(RuntimeError, match="session expired"):
        publisher(clock).publish(make_post(), video, META)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/upload?upload_id=1",
        "http://www.googleapis.com/upload/youtube/v3/videos?upload_id=1",
        "https://www.googleapis.com.evil.example/upload",
        "https://user@www.googleapis.com/upload/youtube/v3/videos",
        None,
    ],
)
def test_the_session_uri_must_be_a_google_upload_url(
    clock: Clock, video: Path, google: respx.MockRouter, location: str | None
) -> None:
    """The bearer token goes to the session URI: never to a host other than Google's."""
    ok_token(google)
    headers = {"Location": location} if location else {}
    google.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers=headers))

    with pytest.raises(RuntimeError, match="session URI"):
        publisher(clock).publish(make_post(), video, META)


def test_an_unexpected_video_id_is_refused(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    ok_session(google)
    bad = {**FIXTURES["video_inserted"], "id": "../../x?y"}
    google.put(SESSION).mock(return_value=httpx.Response(201, json=bad))

    with pytest.raises(RuntimeError, match="video id"):
        publisher(clock).publish(make_post(), video, META)


def test_a_rejected_request_fails_the_attempt_with_the_reason(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    google.post(UPLOAD_URL).mock(
        return_value=httpx.Response(400, json=FIXTURES["error_invalid_title"])
    )

    with pytest.raises(RuntimeError, match="invalidTitle"):
        publisher(clock).publish(make_post(), video, META)


def test_rate_limit_is_an_ordinary_failed_attempt(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    google.post(UPLOAD_URL).mock(
        return_value=httpx.Response(403, json=FIXTURES["error_rate_limit_exceeded"])
    )

    with pytest.raises(RuntimeError, match="rateLimitExceeded"):
        publisher(clock).publish(make_post(), video, META)


def test_an_upload_past_its_deadline_gives_up(
    clock: Clock, tmp_path: Path, google: respx.MockRouter
) -> None:
    """`publish` must finish well within the stage's 30-minute stale-upload window."""
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * (CHUNK * 3))
    ok_token(google)
    ok_session(google)

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += timedelta(minutes=15)
        end = int(request.headers["Content-Range"].split("-")[1].split("/")[0])
        return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})

    google.put(SESSION).mock(side_effect=slow)

    with pytest.raises(TimeoutError):
        publisher(clock).publish(make_post(), video, META)


# --- quota and upload limits defer -------------------------------------------------------


@pytest.mark.parametrize("fixture", ["error_quota_exceeded", "error_upload_limit_exceeded"])
def test_quota_and_upload_limit_errors_defer_to_the_next_quota_day(
    clock: Clock, video: Path, google: respx.MockRouter, fixture: str
) -> None:
    ok_token(google)
    code = FIXTURES[fixture]["error"]["code"]
    google.post(UPLOAD_URL).mock(return_value=httpx.Response(code, json=FIXTURES[fixture]))

    with pytest.raises(PublishDeferred) as caught:
        publisher(clock).publish(make_post(), video, META)

    reason = FIXTURES[fixture]["error"]["errors"][0]["reason"]
    assert reason in caught.value.reason
    assert caught.value.until == next_quota_reset(T0)
    assert caught.value.alert is None


def test_quota_errors_on_the_final_chunk_defer_too(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    ok_session(google)
    google.put(SESSION).mock(
        return_value=httpx.Response(403, json=FIXTURES["error_quota_exceeded"])
    )

    with pytest.raises(PublishDeferred):
        publisher(clock).publish(make_post(), video, META)


def test_quota_day_resets_at_midnight_pacific_time() -> None:
    # 01:00 UTC on 15 Jan is 17:00 PST on 14 Jan: the next reset is 08:00 UTC on 15 Jan.
    assert next_quota_reset(T0) == datetime(2027, 1, 15, 8, 5, tzinfo=UTC)
    # Summer time: midnight PDT is 07:00 UTC.
    assert next_quota_reset(datetime(2027, 7, 1, 7, 30, tzinfo=UTC)) == datetime(
        2027, 7, 2, 7, 5, tzinfo=UTC
    )


# --- OAuth --------------------------------------------------------------------------------


def test_access_token_is_reused_until_it_expires(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    token = ok_token(google)
    ok_session(google)
    ok_upload(google)
    yt = publisher(clock)

    yt.publish(make_post(), video, META)
    clock.now += timedelta(minutes=30)
    yt.publish(make_post(), video, META)
    assert token.call_count == 1

    clock.now += timedelta(minutes=31)  # past expires_in (less the safety margin)
    yt.publish(make_post(), video, META)
    assert token.call_count == 2


def test_a_401_refreshes_the_token_once_and_retries(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    token = ok_token(google)
    session = google.post(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(401, json=FIXTURES["error_unauthorized"]),
            httpx.Response(200, headers={"Location": SESSION}),
        ]
    )
    ok_upload(google)

    publisher(clock).publish(make_post(), video, META)

    assert token.call_count == 2
    assert session.call_count == 2


def test_a_second_401_fails_the_attempt(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    ok_token(google)
    google.post(UPLOAD_URL).mock(
        return_value=httpx.Response(401, json=FIXTURES["error_unauthorized"])
    )

    with pytest.raises(RuntimeError, match="401"):
        publisher(clock).publish(make_post(), video, META)


@pytest.mark.parametrize("fixture", ["token_invalid_grant", "token_invalid_client"])
def test_a_rejected_refresh_token_pauses_the_account_and_alerts_once(
    clock: Clock, video: Path, google: respx.MockRouter, fixture: str
) -> None:
    token = google.post(TOKEN_URL).mock(return_value=httpx.Response(400, json=FIXTURES[fixture]))
    session = ok_session(google)
    yt = publisher(clock)

    with pytest.raises(PublishDeferred) as first:
        yt.publish(make_post(), video, META)
    assert first.value.alert is not None
    assert "yt_main" in first.value.alert
    assert FIXTURES[fixture]["error"] in first.value.alert
    assert first.value.until > T0

    # Paused: later posts are deferred without asking Google again and without alerting.
    with pytest.raises(PublishDeferred) as second:
        yt.publish(make_post(), video, META)
    assert second.value.alert is None
    assert token.call_count == 1
    assert session.call_count == 0


def test_a_new_refresh_token_lifts_the_pause(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    tokens = Tokens()
    token = google.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json=FIXTURES["token_invalid_grant"]),
            httpx.Response(200, json=FIXTURES["token"]),
        ]
    )
    ok_session(google)
    ok_upload(google)
    yt = publisher(clock, tokens=tokens)
    with pytest.raises(PublishDeferred):
        yt.publish(make_post(), video, META)

    tokens.tokens["yt_main"] = "1//re-authorised-refresh-token"
    result = yt.publish(make_post(), video, META)

    assert result.external_id == "Ks-_Mh1QhMc"
    assert token.call_count == 2


def test_a_paused_account_does_not_pause_the_others(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    tokens = Tokens(yt_other="1//other-refresh-token")

    def grant(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if form["refresh_token"] == REFRESH:
            return httpx.Response(400, json=FIXTURES["token_invalid_grant"])
        return httpx.Response(200, json=FIXTURES["token"])

    google.post(TOKEN_URL).mock(side_effect=grant)
    ok_session(google)
    ok_upload(google)
    yt = publisher(clock, accounts=("yt_main", "yt_other"), tokens=tokens)

    with pytest.raises(PublishDeferred):
        yt.publish(make_post(), video, META)
    assert yt.publish(make_post(account="yt_other"), video, META).external_id


def test_missing_refresh_token_pauses_with_an_alert(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    with pytest.raises(PublishDeferred) as caught:
        publisher(clock, tokens=Tokens(yt_main=None)).publish(make_post(), video, META)
    assert caught.value.alert is not None
    assert "CLIPPER_REFRESH_TOKEN_YT_MAIN" in caught.value.alert


def test_missing_oauth_client_pauses_with_an_alert(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    yt = publisher(clock, client=OAuthClient(client_id="", client_secret=None))
    with pytest.raises(PublishDeferred) as caught:
        yt.publish(make_post(), video, META)
    assert caught.value.alert is not None
    assert "YOUTUBE_OAUTH_CLIENT_ID" in caught.value.alert


def test_a_token_endpoint_outage_is_an_ordinary_failed_attempt(
    clock: Clock, video: Path, google: respx.MockRouter
) -> None:
    google.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError, match="503"):
        publisher(clock).publish(make_post(), video, META)


def test_secrets_never_reach_logs_errors_or_repr(
    clock: Clock, video: Path, google: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    google.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=FIXTURES["token"]),
            httpx.Response(400, json=FIXTURES["token_invalid_grant"]),
        ]
    )
    google.post(UPLOAD_URL).mock(
        return_value=httpx.Response(401, json=FIXTURES["error_unauthorized"])
    )
    yt = publisher(clock)
    messages: list[str] = [repr(yt)]
    with pytest.raises(PublishDeferred) as caught:
        yt.publish(make_post(), video, META)
    messages += [str(caught.value), caught.value.reason, caught.value.alert or ""]
    messages += [r.getMessage() for r in caplog.records]
    text = "\n".join(messages)
    for secret in (REFRESH, CLIENT_SECRET, ACCESS):
        assert secret not in text


# --- lookup ---------------------------------------------------------------------------------


def mock_lookup(router: respx.MockRouter) -> tuple[respx.Route, respx.Route, respx.Route]:
    channels = router.get(f"{API_URL}/channels").mock(
        return_value=httpx.Response(200, json=FIXTURES["channels_mine"])
    )
    items = router.get(f"{API_URL}/playlistItems").mock(
        return_value=httpx.Response(200, json=FIXTURES["playlist_items"])
    )
    videos = router.get(f"{API_URL}/videos").mock(
        return_value=httpx.Response(200, json=FIXTURES["videos_list"])
    )
    return channels, items, videos


def test_lookup_finds_an_earlier_upload_by_its_idempotency_tag(
    clock: Clock, google: respx.MockRouter
) -> None:
    ok_token(google)
    channels, items, videos = mock_lookup(google)

    found = publisher(clock).lookup(KEY)

    assert found is not None
    assert found.external_id == "Ks-_Mh1QhMc"
    assert found.url == "https://www.youtube.com/shorts/Ks-_Mh1QhMc"
    assert channels.calls.last.request.url.params["mine"] == "true"
    assert items.calls.last.request.url.params["playlistId"] == "UUrecordedChannel000000"
    assert videos.calls.last.request.url.params["id"] == "Ks-_Mh1QhMc,Older000001"
    assert videos.calls.last.request.headers["Authorization"] == f"Bearer {ACCESS}"


def test_lookup_returns_none_when_no_upload_has_the_tag(
    clock: Clock, google: respx.MockRouter
) -> None:
    ok_token(google)
    mock_lookup(google)
    assert publisher(clock).lookup("f" * 32) is None


def test_lookup_skips_a_paused_account(clock: Clock, google: respx.MockRouter) -> None:
    google.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json=FIXTURES["token_invalid_grant"])
    )
    channels, _, _ = mock_lookup(google)
    assert publisher(clock).lookup(KEY) is None
    assert channels.call_count == 0


def test_lookup_errors_propagate(clock: Clock, google: respx.MockRouter) -> None:
    """The posting stage then hands the post to a human instead of re-uploading it."""
    ok_token(google)
    google.get(f"{API_URL}/channels").mock(return_value=httpx.Response(500))
    with pytest.raises(RuntimeError, match="500"):
        publisher(clock).lookup(KEY)


# --- registration ---------------------------------------------------------------------------


def _accounts(*specs: tuple[str, str, bool]) -> AccountsConfig:
    return AccountsConfig.model_construct(
        accounts=[
            Account.model_construct(id=account_id, platform=platform, enabled=enabled)
            for account_id, platform, enabled in specs
        ]
    )


def test_youtube_publisher_is_registered_for_enabled_youtube_accounts() -> None:
    accounts = _accounts(
        ("yt_main", "youtube", True),
        ("yt_off", "youtube", False),
        ("tt_main", "tiktok", True),
    )
    secrets = Secrets(
        youtube_oauth_client_id=SecretStr("123-recorded.apps.googleusercontent.com"),
        youtube_oauth_client_secret=SecretStr(CLIENT_SECRET),
    )

    publishers = build_publishers(accounts, secrets)

    assert set(publishers) == {"youtube"}
    yt = publishers["youtube"]
    assert isinstance(yt, YouTubePublisher) and isinstance(yt, Publisher)
    assert yt.account_ids == ("yt_main",)
    assert CLIENT_SECRET not in repr(yt)


def test_no_youtube_accounts_means_no_youtube_publisher() -> None:
    assert build_publishers(_accounts(("tt_main", "tiktok", True)), Secrets()) == {}


def test_refresh_tokens_are_read_from_the_account_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPPER_REFRESH_TOKEN_YT_MAIN", REFRESH)
    yt = build_publishers(_accounts(("yt_main", "youtube", True)), Secrets())["youtube"]
    assert isinstance(yt, YouTubePublisher)
    token = yt.refresh_token("yt_main")
    assert token is not None and token.get_secret_value() == REFRESH


ACCOUNTS_YAML = """\
accounts:
  - id: yt_main
    platform: youtube
    handle: "@MrBeastClipsDaily"
    market: US
    timezone: America/New_York
    creators: [MrBeast]
"""


def _run_worker(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, accounts: str
) -> dict[str, Any]:
    from typer.testing import CliRunner

    from clipper import cli

    seen: dict[str, Any] = {}
    real = cli.PostingStage

    def capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(cli, "PostingStage", capture)
    (config_file.parent / "accounts.yaml").write_text(accounts)
    opts = ["--db", str(tmp_path / "s.db"), "--config", str(config_file)]
    result = CliRunner().invoke(
        cli.app, [*opts, "run-worker", "--once", "--media-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    return seen


def test_run_worker_registers_the_youtube_publisher(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _run_worker(tmp_path, config_file, monkeypatch, ACCOUNTS_YAML)
    assert set(seen["publishers"]) == {"youtube"}


def test_run_worker_starts_without_publishers_when_accounts_yaml_is_invalid(
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        seen = _run_worker(tmp_path, config_file, monkeypatch, "accounts: [{id: Bad}]\n")
    assert seen["publishers"] == {}
    assert "posting is off" in caplog.text
