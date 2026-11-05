"""Instagram Reels publisher (CLP-94): REELS container with a resumable upload (no public
video URL), status_code polling IN_PROGRESS -> FINISHED, media_publish, the publishing
limit and at most 5 hashtags. Recorded responses."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from clipper.posting.models import PublishDeferred, PublishRejected, PublishResult
from clipper.publish.instagram import InstagramPublisher
from clipper.publish.meta import GraphClient, MetaError
from meta_support import (
    IG_ID,
    IG_TOKEN,
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

CONTAINER = recorded("ig_container")["id"]
UPLOAD = recorded("ig_container")["uri"]
MEDIA = recorded("ig_publish")["id"]
PERMALINK = recorded("ig_media_permalink")["permalink"]


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def ig_post(**kwargs: object):  # noqa: ANN201
    return make_post(platform="instagram", account="ig_main", at=T0, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(graph("debug_token")).mock(return_value=reply("debug_token_page"))
        router.get(graph(f"{IG_ID}/content_publishing_limit")).mock(
            return_value=reply("ig_publishing_limit")
        )
        yield router


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def alerts() -> Alerts:
    return Alerts()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def publisher(clock: Clock, alerts: Alerts, sleeps: list[float]) -> Iterator[InstagramPublisher]:
    with httpx.Client() as http:
        yield InstagramPublisher(
            GraphClient(http),
            guard(http, clock, alerts),
            credentials=credentials(),
            clock=clock,
            sleep=sleeps.append,
            poll_every=5.0,
            max_polls=4,
        )


def container_flow(mock: respx.MockRouter, *statuses: str) -> dict[str, respx.Route]:
    routes = {
        "media": mock.post(graph(f"{IG_ID}/media")).mock(return_value=reply("ig_container")),
        "upload": mock.post(UPLOAD).mock(return_value=reply("ig_upload_ok")),
        "status": mock.get(graph(CONTAINER)),
        "publish": mock.post(graph(f"{IG_ID}/media_publish")).mock(
            return_value=reply("ig_publish")
        ),
        "permalink": mock.get(graph(MEDIA)).mock(return_value=reply("ig_media_permalink")),
    }
    routes["status"].side_effect = [reply(f"ig_status_{s}") for s in statuses]
    return routes


def test_reel_is_published_after_the_container_finishes(
    mock: respx.MockRouter, publisher: InstagramPublisher, sleeps: list[float], tmp_path: Path
) -> None:
    routes = container_flow(mock, "in_progress", "in_progress", "finished")

    result = publisher.publish(ig_post(), video(tmp_path, 3000), metadata())

    assert result == PublishResult(external_id=MEDIA, url=PERMALINK)
    assert form(routes["media"].calls.last.request) == {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "He did not see it coming\n\n#funny #fail #chair",
        "share_to_feed": "true",
        "is_ai_generated": "false",
        "is_paid_partnership": "false",
    }
    sent = routes["upload"].calls.last.request
    assert sent.headers["file_size"] == "3000" and sent.headers["offset"] == "0"
    assert routes["status"].call_count == 3
    assert sleeps == [5.0, 5.0]
    assert form(routes["publish"].calls.last.request) == {"creation_id": CONTAINER}
    for route in routes.values():
        for call in route.calls:
            assert IG_TOKEN not in str(call.request.url)


def test_no_video_url_is_ever_sent(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")

    publisher.publish(ig_post(), video(tmp_path), metadata())

    assert "video_url" not in form(routes["media"].calls.last.request)


def test_error_container_is_never_published(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "in_progress", "error")

    with pytest.raises(MetaError, match="2207026"):
        publisher.publish(ig_post(), video(tmp_path), metadata())

    assert routes["publish"].call_count == 0


def test_container_that_never_finishes_times_out(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, *["in_progress"] * 4)

    with pytest.raises(MetaError, match="still IN_PROGRESS"):
        publisher.publish(ig_post(), video(tmp_path), metadata())

    assert routes["publish"].call_count == 0


def test_disclosure_flags_map_to_the_container_fields(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    flags = {"is_ai_generated": True, "paid_partnership": True}

    publisher.publish(ig_post(), video(tmp_path), metadata(flags=flags))

    sent = form(routes["media"].calls.last.request)
    assert sent["is_ai_generated"] == "true"
    assert sent["is_paid_partnership"] == "true"


def test_more_than_five_hashtags_is_refused(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    tags = ["#a1", "#b2", "#c3", "#d4", "#e5", "#f6"]

    with pytest.raises(PublishRejected, match="at most 5 hashtags"):
        publisher.publish(
            ig_post(), video(tmp_path), metadata(caption="x " + " ".join(tags), hashtags=tags)
        )

    assert routes["media"].call_count == 0


def test_hashtags_hidden_in_the_caption_count_too(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    container_flow(mock, "finished")
    caption = "#one #two #three #four #five #six"

    with pytest.raises(PublishRejected, match="at most 5 hashtags"):
        publisher.publish(ig_post(), video(tmp_path), metadata(caption=caption, hashtags=[]))


def test_long_caption_is_refused(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    container_flow(mock, "finished")

    with pytest.raises(PublishRejected, match="2200"):
        publisher.publish(ig_post(), video(tmp_path), metadata(caption="x" * 2201, hashtags=[]))


def test_near_the_publishing_limit_defers(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    mock.get(graph(f"{IG_ID}/content_publishing_limit")).mock(
        return_value=reply("ig_publishing_limit_near_cap")
    )
    routes = container_flow(mock, "finished")

    with pytest.raises(PublishDeferred, match="publishing limit") as caught:
        publisher.publish(ig_post(), video(tmp_path), metadata())

    assert caught.value.until == T0 + timedelta(hours=1)
    assert routes["media"].call_count == 0


def test_publish_limit_error_defers(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    routes["publish"].mock(return_value=reply("error_ig_publish_limit", 400))

    with pytest.raises(PublishDeferred):
        publisher.publish(ig_post(), video(tmp_path), metadata())


def test_expired_token_defers_and_alerts(
    mock: respx.MockRouter, publisher: InstagramPublisher, alerts: Alerts, tmp_path: Path
) -> None:
    mock.get(graph(f"{IG_ID}/content_publishing_limit")).mock(
        return_value=reply("error_token_expired", 400)
    )

    with pytest.raises(PublishDeferred):
        publisher.publish(ig_post(), video(tmp_path), metadata())

    assert len(alerts.messages) == 1 and "ig_main" in alerts.messages[0]


def test_limit_check_failure_does_not_block(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    mock.get(graph(f"{IG_ID}/content_publishing_limit")).mock(
        return_value=reply("error_transient", 500)
    )
    container_flow(mock, "finished")

    assert publisher.publish(ig_post(), video(tmp_path), metadata()).external_id == MEDIA


def test_missing_permalink_still_records_the_post(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    routes["permalink"].mock(return_value=reply("error_transient", 500))

    assert publisher.publish(ig_post(), video(tmp_path), metadata()) == PublishResult(
        external_id=MEDIA, url=None
    )


def test_non_instagram_permalink_is_dropped(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    routes["permalink"].mock(
        return_value=httpx.Response(200, json={"permalink": "javascript:alert(1)"})
    )

    assert publisher.publish(ig_post(), video(tmp_path), metadata()).url is None


# --- lookup (no double post on retry) ----------------------------------------------------


def test_lookup_finds_a_reel_whose_publish_reply_was_lost(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    mock.get(graph(f"{IG_ID}/media")).mock(return_value=reply("ig_recent_media"))
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    assert publisher.lookup(post.idempotency_key) == PublishResult(external_id=MEDIA, url=PERMALINK)


def test_lookup_of_an_unpublished_container_rechecks_once_then_is_not_found(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished", "finished", "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    # media_publish may still be in flight: check again later before publishing again.
    with pytest.raises(PublishDeferred) as caught:
        publisher.lookup(post.idempotency_key)
    assert caught.value.until == T0 + timedelta(minutes=5)
    assert publisher.lookup(post.idempotency_key) is None


def test_lookup_with_two_matching_recent_posts_raises(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    recent = recorded("ig_recent_media")
    recent["data"][1]["caption"] = recent["data"][0]["caption"]
    recent["data"][1]["timestamp"] = "2027-01-15T01:00:30+0000"
    mock.get(graph(f"{IG_ID}/media")).mock(return_value=httpx.Response(200, json=recent))
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    with pytest.raises(MetaError, match="2 recent posts"):
        publisher.lookup(post.idempotency_key)


def test_lookup_ignores_an_older_post_with_the_same_caption(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    recent = recorded("ig_recent_media")
    recent["data"][0]["timestamp"] = "2027-01-10T01:00:00+0000"  # before this attempt
    mock.get(graph(f"{IG_ID}/media")).mock(return_value=httpx.Response(200, json=recent))
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    with pytest.raises(MetaError, match="0 recent posts"):
        publisher.lookup(post.idempotency_key)


def test_polling_stops_at_the_deadline(
    mock: respx.MockRouter, clock: Clock, alerts: Alerts, tmp_path: Path
) -> None:
    routes = container_flow(mock, *["in_progress"] * 60)

    def slow_sleep(seconds: float) -> None:
        clock.now += timedelta(minutes=5)

    with httpx.Client() as http:
        publisher = InstagramPublisher(
            GraphClient(http),
            guard(http, clock, alerts),
            credentials=credentials(),
            clock=clock,
            sleep=slow_sleep,
        )
        with pytest.raises(MetaError, match="still IN_PROGRESS"):
            publisher.publish(ig_post(), video(tmp_path), metadata())

    assert routes["status"].call_count == 3  # 0, 5 and 10 minutes; stopped at 15
    assert routes["publish"].call_count == 0


def test_campaign_account_is_refused_until_disclosure_is_wired(
    mock: respx.MockRouter, clock: Clock, alerts: Alerts, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished")
    with httpx.Client() as http:
        publisher = InstagramPublisher(
            GraphClient(http),
            guard(http, clock, alerts),
            credentials=credentials(),
            clock=clock,
            campaigns={"ig_main": "vyro_mrbeast"},
        )
        with pytest.raises(PublishRejected, match="CLP-97"):
            publisher.publish(ig_post(), video(tmp_path), metadata())

    assert routes["media"].call_count == 0


def test_lookup_of_a_published_container_without_a_match_raises(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    routes = container_flow(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    mock.get(graph(f"{IG_ID}/media")).mock(return_value=httpx.Response(200, json={"data": []}))
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    # Live but unidentifiable: the stage turns this into post_failed for a human.
    with pytest.raises(MetaError, match="published"):
        publisher.lookup(post.idempotency_key)


def test_lookup_before_media_publish_is_not_found(
    mock: respx.MockRouter, publisher: InstagramPublisher, tmp_path: Path
) -> None:
    container_flow(mock, "error")
    post = ig_post()

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    assert publisher.lookup(post.idempotency_key) is None
    assert publisher.lookup("never-seen") is None
