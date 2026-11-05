"""Facebook Page Reels publisher (CLP-94): video_reels start, rupload, finish with
SCHEDULED + scheduled_publish_time, so Facebook publishes at the slot. Recorded responses."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from clipper.posting.models import PublishDeferred, PublishRejected, PublishResult
from clipper.posting.stage import STALE_UPLOAD
from clipper.publish.facebook import LEAD_TIME, SCHEDULE_MIN_AHEAD, FacebookPublisher
from clipper.publish.meta import GraphClient, MetaError
from meta_support import (
    ENV,
    FB_TOKEN,
    PAGE_ID,
    T0,
    Alerts,
    Clock,
    credentials,
    graph,
    guard,
    make_post,
    metadata,
    recorded,
    reply,
    video,
)

REELS = graph(f"{PAGE_ID}/video_reels")
UPLOAD = recorded("fb_reels_start")["upload_url"]
VIDEO_ID = recorded("fb_reels_start")["video_id"]


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(graph("debug_token")).mock(return_value=reply("debug_token_page"))
        yield router


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def alerts() -> Alerts:
    return Alerts()


@pytest.fixture
def publisher(clock: Clock, alerts: Alerts) -> Iterator[FacebookPublisher]:
    with httpx.Client() as http:
        yield FacebookPublisher(
            GraphClient(http), guard(http, clock, alerts), credentials=credentials(), clock=clock
        )


def happy(mock: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    reels = mock.post(REELS)
    reels.side_effect = [reply("fb_reels_start"), reply("fb_reels_finish")]
    upload = mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))
    return reels, upload


def test_lead_time_leaves_room_for_native_scheduling() -> None:
    assert LEAD_TIME > SCHEDULE_MIN_AHEAD >= timedelta(minutes=10)
    assert LEAD_TIME < STALE_UPLOAD


def test_reel_is_scheduled_for_the_slot(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels, upload = happy(mock)
    post = make_post(at=T0 + timedelta(minutes=30))

    result = publisher.publish(post, video(tmp_path, 1000), metadata())

    assert result == PublishResult(
        external_id=VIDEO_ID, url=f"https://www.facebook.com/reel/{VIDEO_ID}"
    )
    start, finish = (form(call.request) for call in reels.calls)
    assert start == {"upload_phase": "start"}
    assert finish == {
        "upload_phase": "finish",
        "video_id": VIDEO_ID,
        "video_state": "SCHEDULED",
        "scheduled_publish_time": str(int(post.scheduled_at.timestamp())),
        "description": "He did not see it coming\n\n#funny #fail #chair",
        "title": "Chair falls",
        "is_ai_generated": "false",
    }
    sent = upload.calls.last.request
    assert sent.headers["file_size"] == "1000" and sent.headers["offset"] == "0"
    for call in [*reels.calls, *upload.calls]:
        assert FB_TOKEN not in str(call.request.url)
        assert call.request.headers["authorization"] == f"OAuth {FB_TOKEN}"


def test_late_post_is_published_now(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels, _ = happy(mock)
    post = make_post(at=T0 + SCHEDULE_MIN_AHEAD - timedelta(seconds=1))

    publisher.publish(post, video(tmp_path), metadata())

    finish = form(reels.calls[1].request)
    assert finish["video_state"] == "PUBLISHED"
    assert "scheduled_publish_time" not in finish


def test_ai_disclosure_maps_to_is_ai_generated(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels, _ = happy(mock)
    flags = {"is_ai_generated": True, "paid_partnership": False}

    publisher.publish(make_post(), video(tmp_path), metadata(flags=flags))

    assert form(reels.calls[1].request)["is_ai_generated"] == "true"


def test_paid_partnership_is_refused_before_any_upload(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels, upload = happy(mock)
    flags = {"is_ai_generated": False, "paid_partnership": True}

    with pytest.raises(PublishRejected, match="paid partnership"):
        publisher.publish(make_post(), video(tmp_path), metadata(flags=flags))

    assert reels.call_count == 0 and upload.call_count == 0


def test_expired_token_defers_and_pauses_the_account(
    mock: respx.MockRouter,
    publisher: FacebookPublisher,
    alerts: Alerts,
    tmp_path: Path,
) -> None:
    reels = mock.post(REELS).mock(return_value=reply("error_token_expired", 400))

    with pytest.raises(PublishDeferred) as caught:
        publisher.publish(make_post(), video(tmp_path), metadata())
    with pytest.raises(PublishDeferred):
        publisher.publish(make_post(key="idem-0002"), video(tmp_path), metadata())

    assert caught.value.until == T0 + timedelta(hours=1)
    assert reels.call_count == 1  # the second post never reaches the API
    assert len(alerts.messages) == 1 and "fb_main" in alerts.messages[0]
    assert FB_TOKEN not in alerts.messages[0]


def test_rate_limit_defers(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels = mock.post(REELS)
    reels.side_effect = [reply("fb_reels_start"), reply("error_rate_limit", 400)]
    mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))

    with pytest.raises(PublishDeferred, match="rate limit") as caught:
        publisher.publish(make_post(), video(tmp_path), metadata())

    assert caught.value.until == T0 + timedelta(hours=1)


def test_other_errors_raise(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    mock.post(REELS).mock(return_value=reply("error_invalid_parameter", 400))

    with pytest.raises(MetaError, match="video_id is required"):
        publisher.publish(make_post(), video(tmp_path), metadata())


def test_upload_url_on_another_host_is_refused(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    body = {**recorded("fb_reels_start"), "upload_url": "https://evil.example/up"}
    mock.post(REELS).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(MetaError, match="upload URL"):
        publisher.publish(make_post(), video(tmp_path), metadata())


def test_missing_upload_url_uses_the_documented_rupload_url(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels = mock.post(REELS)
    reels.side_effect = [
        httpx.Response(200, json={"video_id": VIDEO_ID}),
        reply("fb_reels_finish"),
    ]
    upload = mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))

    publisher.publish(make_post(), video(tmp_path), metadata())

    assert upload.call_count == 1


def test_bad_video_id_is_refused(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    mock.post(REELS).mock(return_value=httpx.Response(200, json={"video_id": "../me"}))

    with pytest.raises(MetaError, match="video id"):
        publisher.publish(make_post(), video(tmp_path), metadata())


def test_upload_not_accepted_raises(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    mock.post(REELS).mock(return_value=reply("fb_reels_start"))
    mock.post(UPLOAD).mock(return_value=httpx.Response(200, json={"success": False}))

    with pytest.raises(MetaError, match="upload"):
        publisher.publish(make_post(), video(tmp_path), metadata())


def test_missing_credentials_defer_with_an_alert(
    clock: Clock, alerts: Alerts, tmp_path: Path
) -> None:
    env = {k: v for k, v in ENV.items() if k != "CLIPPER_META_ID_FB_MAIN"}
    with httpx.Client() as http:
        publisher = FacebookPublisher(
            GraphClient(http), guard(http, clock, alerts), credentials=credentials(env), clock=clock
        )
        with pytest.raises(PublishDeferred):
            publisher.publish(make_post(), video(tmp_path), metadata())

    assert "CLIPPER_META_ID_FB_MAIN" in alerts.messages[0]


# --- lookup (no double post on retry) ----------------------------------------------------


def test_lookup_finds_a_reel_whose_finish_reply_was_lost(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels = mock.post(REELS)
    reels.side_effect = [reply("fb_reels_start"), httpx.ReadTimeout("lost")]
    mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))
    status = mock.get(graph(VIDEO_ID)).mock(return_value=reply("fb_video_scheduled"))
    post = make_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    found = publisher.lookup(post.idempotency_key)

    assert found == PublishResult(
        external_id=VIDEO_ID, url=f"https://www.facebook.com/reel/{VIDEO_ID}/"
    )
    assert "scheduled_publish_time" in status.calls.last.request.url.params["fields"]


def test_lookup_of_a_reel_still_processing_raises_instead_of_reposting(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels = mock.post(REELS)
    reels.side_effect = [reply("fb_reels_start"), httpx.ReadTimeout("lost")]
    mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))
    status = mock.get(graph(VIDEO_ID)).mock(return_value=reply("fb_video_unpublished"))
    post = make_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    # Finished but neither scheduled nor published: it may still go live, so the stage
    # must hand the post to a human rather than upload it again.
    with pytest.raises(MetaError, match="neither scheduled nor published"):
        publisher.lookup(post.idempotency_key)
    assert "status" in status.calls.last.request.url.params["fields"]


def test_lookup_of_a_failed_reel_is_not_found(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    reels = mock.post(REELS)
    reels.side_effect = [reply("fb_reels_start"), httpx.ReadTimeout("lost")]
    mock.post(UPLOAD).mock(return_value=reply("fb_upload_ok"))
    failed = {**recorded("fb_video_unpublished"), "status": {"video_status": "error"}}
    mock.get(graph(VIDEO_ID)).mock(return_value=httpx.Response(200, json=failed))
    post = make_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    assert publisher.lookup(post.idempotency_key) is None


def test_campaign_account_is_refused_until_disclosure_is_wired(
    mock: respx.MockRouter, clock: Clock, alerts: Alerts, tmp_path: Path
) -> None:
    reels, _ = happy(mock)
    with httpx.Client() as http:
        publisher = FacebookPublisher(
            GraphClient(http),
            guard(http, clock, alerts),
            credentials=credentials(),
            clock=clock,
            campaigns={"fb_main": "vyro_mrbeast"},
        )
        with pytest.raises(PublishRejected, match="vyro_mrbeast"):
            publisher.publish(make_post(), video(tmp_path), metadata())

    assert reels.call_count == 0


def test_permission_error_pauses_the_account(
    mock: respx.MockRouter, publisher: FacebookPublisher, alerts: Alerts, tmp_path: Path
) -> None:
    error = {
        "error": {"message": "(#200) Permissions error", "type": "OAuthException", "code": 200}
    }
    mock.post(REELS).mock(return_value=httpx.Response(403, json=error))

    with pytest.raises(PublishDeferred):
        publisher.publish(make_post(), video(tmp_path), metadata())

    assert len(alerts.messages) == 1 and "paused" in alerts.messages[0]


def test_rate_limit_holds_the_whole_account(
    mock: respx.MockRouter, publisher: FacebookPublisher, clock: Clock, tmp_path: Path
) -> None:
    reels = mock.post(REELS).mock(return_value=reply("error_rate_limit", 400))

    with pytest.raises(PublishDeferred):
        publisher.publish(make_post(), video(tmp_path), metadata())
    with pytest.raises(PublishDeferred, match="rate limit"):
        publisher.publish(make_post(key="idem-0002"), video(tmp_path), metadata())
    assert reels.call_count == 1  # the second post made no call

    clock.now += timedelta(hours=1)
    with pytest.raises(PublishDeferred):
        publisher.publish(make_post(key="idem-0003"), video(tmp_path), metadata())
    assert reels.call_count == 2  # the hold has expired


def test_publish_gives_up_at_the_deadline(
    mock: respx.MockRouter, clock: Clock, alerts: Alerts, tmp_path: Path
) -> None:
    reels, upload = happy(mock)

    def slow_start(request: httpx.Request) -> httpx.Response:
        clock.now += timedelta(minutes=16)
        return reply("fb_reels_start")

    reels.side_effect = [slow_start]
    with httpx.Client() as http:
        publisher = FacebookPublisher(
            GraphClient(http), guard(http, clock, alerts), credentials=credentials(), clock=clock
        )
        with pytest.raises(MetaError, match="deadline"):
            publisher.publish(make_post(at=T0 + timedelta(hours=1)), video(tmp_path), metadata())

    assert upload.call_count == 0


def test_permalink_must_be_a_plain_facebook_url() -> None:
    from clipper.publish.facebook import _permalink

    assert _permalink("/reel/1/") == "https://www.facebook.com/reel/1/"
    assert _permalink("https://www.facebook.com/reel/1") == "https://www.facebook.com/reel/1"
    for bad in (
        "https://evil.example\\@www.facebook.com/x",
        "https://user@www.facebook.com/x",
        "https://www.facebook.com:8443/x",
        "https://l.facebook.com/l.php?u=https://evil.example",
        "javascript:alert(1)",
        "//evil.example/x",
    ):
        assert _permalink(bad) is None, bad


def test_lookup_without_a_finish_call_is_not_found(
    mock: respx.MockRouter, publisher: FacebookPublisher, tmp_path: Path
) -> None:
    mock.post(REELS).mock(return_value=reply("fb_reels_start"))
    mock.post(UPLOAD).mock(return_value=reply("error_transient", 500))
    post = make_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    assert publisher.lookup(post.idempotency_key) is None
    assert publisher.lookup("never-seen") is None
