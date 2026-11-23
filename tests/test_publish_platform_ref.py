"""The Meta upload id survives a worker restart (CLP-122).

The Facebook `video_id` and the Instagram container id are stored on the post
(`posts.platform_ref`) right after Meta creates them, and again just before the call that
publishes, so a new worker's `lookup` checks the upload by id instead of guessing (or not
finding it at all). Each restart test builds a new publisher on the same database: nothing
of the first attempt is left in memory. No test talks to Meta: every request goes to respx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from clipper.db import connect
from clipper.posting import repo
from clipper.posting.models import (
    Post,
    PostConflict,
    PostStatus,
    PublishDeferred,
    PublishResult,
    StoredRef,
)
from clipper.posting.stage import STALE_UPLOAD, PostingStage, StoredRefs, allow_all
from clipper.publish.facebook import FacebookPublisher
from clipper.publish.instagram import InstagramPublisher
from clipper.publish.meta import GraphClient, MetaError
from meta_support import (
    FB_TOKEN,
    IG_ID,
    PAGE_ID,
    T0,
    Alerts,
    Clock,
    credentials,
    graph,
    guard,
    metadata,
    recorded,
    reply,
    video,
)
from meta_support import make_post as meta_post
from review_support import Env, make_env

CONTAINER = recorded("ig_container")["id"]
IG_UPLOAD = recorded("ig_container")["uri"]
MEDIA = recorded("ig_publish")["id"]
IG_PERMALINK = recorded("ig_media_permalink")["permalink"]
RECENT_PERMALINK = recorded("ig_recent_media")["data"][0]["permalink"]
VIDEO_ID = recorded("fb_reels_start")["video_id"]
FB_UPLOAD = recorded("fb_reels_start")["upload_url"]
REELS = graph(f"{PAGE_ID}/video_reels")
LATER = STALE_UPLOAD + timedelta(minutes=1)
MEDIA_NODE = {  # GET /{media-id}?fields=id,caption,owner,permalink,timestamp (IG Media)
    "id": MEDIA,
    "caption": "He did not see it coming\n\n#funny #fail #chair",  # meta_support.metadata()
    "owner": {"id": IG_ID},
    "permalink": IG_PERMALINK,
    "timestamp": "2027-01-15T01:01:00+0000",
}


class Crash(BaseException):  # noqa: N818 - stands for the process dying, not an error
    """Raised from inside a Graph call: the worker dies there (the stage cannot catch it)."""


@dataclass
class Killer:
    """A respx side effect that kills the worker inside the call. respx records no call
    for a BaseException, so this keeps the requests it saw."""

    where: str
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise Crash(f"worker killed {self.where}")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(graph("debug_token")).mock(return_value=reply("debug_token_page"))
        router.get(graph(f"{IG_ID}/content_publishing_limit")).mock(
            return_value=reply("ig_publishing_limit")
        )
        yield router


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


def instagram(http: httpx.Client, clock: Clock) -> InstagramPublisher:
    """A new publisher, as a restarted worker builds it: nothing in memory."""
    return InstagramPublisher(
        GraphClient(http),
        guard(http, clock, Alerts()),
        credentials=credentials(),
        clock=clock,
        sleep=lambda _: None,
        poll_every=1.0,
        max_polls=3,
    )


def facebook(http: httpx.Client, clock: Clock) -> FacebookPublisher:
    return FacebookPublisher(
        GraphClient(http), guard(http, clock, Alerts()), credentials=credentials(), clock=clock
    )


def stage(
    env: Env,
    clock: Clock,
    platform: str,
    publisher: object,
    worker: str,
    conn: sqlite3.Connection | None = None,
) -> PostingStage:
    return PostingStage(
        conn=env.conn if conn is None else conn,
        publishers={platform: publisher},  # type: ignore[dict-item]
        worker_id=worker,
        media_root=env.media_root,
        clock=clock,
        guard=allow_all,
    )


def add_post(env: Env, platform: str, account: str, at: datetime = T0) -> Post:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    target = env.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * 2048)
    clip_id = env.clip(job, status="approved", video_path=rel)
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=at,
        video_path=rel,
        now=T0 - timedelta(hours=1),
        metadata=metadata(),
    )


def restarted(env: Env, clock: Clock, platform: str, publisher: object) -> None:
    """One pass of a new worker `w2`: its own DB connection, a new publisher."""
    with closing(connect(env.db_path)) as conn:
        stage(env, clock, platform, publisher, "w2", conn).run_once()


def stored(env: Env, post: Post) -> dict:
    found = repo.platform_ref(env.conn, post.idempotency_key)
    assert found is not None
    return json.loads(found.ref)


def current(env: Env, post: Post) -> Post:
    found = repo.get_post(env.conn, post.id)
    assert found is not None
    return found


def ig_routes(mock: respx.MockRouter, *statuses: str) -> dict[str, respx.Route]:
    routes = {
        "media": mock.post(graph(f"{IG_ID}/media")).mock(return_value=reply("ig_container")),
        "upload": mock.post(IG_UPLOAD).mock(return_value=reply("ig_upload_ok")),
        "status": mock.get(graph(CONTAINER)),
        "publish": mock.post(graph(f"{IG_ID}/media_publish")).mock(
            return_value=reply("ig_publish")
        ),
        "permalink": mock.get(graph(MEDIA)).mock(return_value=reply("ig_media_permalink")),
        "recent": mock.get(graph(f"{IG_ID}/media")).mock(return_value=reply("ig_recent_media")),
    }
    routes["status"].side_effect = [reply(f"ig_status_{s}") for s in statuses]
    return routes


def fb_routes(mock: respx.MockRouter, video_state: str = "fb_video_scheduled") -> dict:
    return {
        "reels": mock.post(REELS),
        "upload": mock.post(FB_UPLOAD).mock(return_value=reply("fb_upload_ok")),
        "video": mock.get(graph(VIDEO_ID)).mock(return_value=reply(video_state)),
    }


def finish_calls(route: respx.Route) -> int:
    return sum(b"upload_phase=finish" in call.request.content for call in route.calls)


def start_calls(route: respx.Route) -> int:
    return sum(b"upload_phase=start" in call.request.content for call in route.calls)


# --- the column and its repo helpers ------------------------------------------------------


def test_posts_have_a_nullable_platform_ref_column(env: Env) -> None:
    columns = {row["name"]: row for row in env.conn.execute("PRAGMA table_info(posts)")}
    assert "platform_ref" in columns
    assert columns["platform_ref"]["notnull"] == 0
    post = add_post(env, "instagram", "ig_main")
    assert repo.platform_ref(env.conn, post.idempotency_key) is None


def test_the_ref_is_written_only_for_the_workers_own_upload(env: Env, clock: Clock) -> None:
    post = add_post(env, "instagram", "ig_main")
    key = post.idempotency_key

    assert not repo.save_platform_ref(env.conn, key, "w1", "x")  # scheduled, not uploading
    claimed = repo.claim_due(env.conn, T0, "w1", platforms=["instagram"])
    assert [p.id for p in claimed] == [post.id]
    assert not repo.save_platform_ref(env.conn, key, "w2", "x")  # another worker's upload
    assert repo.platform_ref(env.conn, key) is None

    assert repo.save_platform_ref(env.conn, key, "w1", '{"container_id": "1"}')
    assert repo.platform_ref(env.conn, key) == StoredRef("ig_main", '{"container_id": "1"}')
    assert repo.platform_ref(env.conn, "never-seen") is None


@pytest.mark.parametrize("raw", ["not json", json.dumps({"pad": "x" * 4100})])
def test_the_column_refuses_anything_but_a_small_json_object(env: Env, raw: str) -> None:
    post = add_post(env, "instagram", "ig_main")
    repo.claim_due(env.conn, T0, "w1", platforms=["instagram"])

    with pytest.raises(sqlite3.IntegrityError):
        repo.save_platform_ref(env.conn, post.idempotency_key, "w1", raw)


def test_the_stage_store_refuses_a_post_that_is_not_its_upload(env: Env) -> None:
    post = add_post(env, "facebook", "fb_main")
    repo.claim_due(env.conn, T0, "w1", platforms=["facebook"])

    with pytest.raises(PostConflict):
        StoredRefs(env.conn, "w2").save(post.idempotency_key, "{}")
    StoredRefs(env.conn, "w1").save(post.idempotency_key, "{}")
    assert StoredRefs(env.conn, "w2").load(post.idempotency_key) == StoredRef("fb_main", "{}")


# --- the ref is stored before any step that can be interrupted -----------------------------


@dataclass
class Refs:
    """A recording `PlatformRefs` store for the post of account `account`. `refuse` plays
    a post that is no longer ours: always, or for the saves it returns True for."""

    account: str = "ig_main"
    saved: dict[str, str] = field(default_factory=dict)
    history: list[tuple[str, dict]] = field(default_factory=list)
    refuse: bool | Callable[[dict], bool] = False

    def save(self, idempotency_key: str, ref: str) -> None:
        value = json.loads(ref)
        if self.refuse is True or (callable(self.refuse) and self.refuse(value)):
            raise PostConflict("post is no longer this worker's upload")
        self.saved[idempotency_key] = ref
        self.history.append((idempotency_key, value))

    def load(self, idempotency_key: str) -> StoredRef | None:
        raw = self.saved.get(idempotency_key)
        return None if raw is None else StoredRef(self.account, raw)


def fb_refs(**kwargs: object) -> Refs:
    return Refs(account="fb_main", **kwargs)  # type: ignore[arg-type]


def test_instagram_stores_the_container_before_the_upload_and_marks_it_before_publishing(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    refs = Refs()
    routes = ig_routes(mock, "finished")
    seen_at_upload: list[dict] = []
    seen_at_publish: list[dict] = []

    def at_upload(_: httpx.Request) -> httpx.Response:
        seen_at_upload.append(refs.history[-1][1])
        return reply("ig_upload_ok")

    def at_publish(_: httpx.Request) -> httpx.Response:
        seen_at_publish.append(refs.history[-1][1])
        return reply("ig_publish")

    routes["upload"].mock(side_effect=at_upload)
    routes["publish"].mock(side_effect=at_publish)
    publisher = instagram(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)

    publisher.publish(post, video(tmp_path), metadata())

    assert seen_at_upload[0]["container_id"] == CONTAINER
    assert seen_at_upload[0]["publish_sent"] is False
    assert "account_id" not in seen_at_upload[0]  # the post's own account is the truth
    assert seen_at_publish[0]["container_id"] == CONTAINER
    assert seen_at_publish[0]["publish_sent"] is True
    assert refs.history[-1][1]["media_id"] == MEDIA  # kept once media_publish answered


def test_facebook_stores_the_video_id_before_the_upload_and_marks_it_before_finishing(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    refs = fb_refs()
    routes = fb_routes(mock)
    seen_at_upload: list[dict] = []
    seen_at_finish: list[dict] = []

    def reels(request: httpx.Request) -> httpx.Response:
        if b"upload_phase=start" in request.content:
            return reply("fb_reels_start")
        seen_at_finish.append(refs.history[-1][1])
        return reply("fb_reels_finish")

    def at_upload(_: httpx.Request) -> httpx.Response:
        seen_at_upload.append(refs.history[-1][1])
        return reply("fb_upload_ok")

    routes["reels"].mock(side_effect=reels)
    routes["upload"].mock(side_effect=at_upload)
    publisher = facebook(http, clock)
    publisher.use_platform_refs(refs)

    publisher.publish(meta_post(), video(tmp_path), metadata())

    assert seen_at_upload == [{"video_id": VIDEO_ID, "finish_sent": False}]
    assert seen_at_finish == [{"video_id": VIDEO_ID, "finish_sent": True}]


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_an_upload_that_cannot_store_its_ref_stops_before_sending_the_video(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path, platform: str
) -> None:
    refs = Refs(refuse=True) if platform == "instagram" else fb_refs(refuse=True)
    if platform == "instagram":
        upload = ig_routes(mock)["upload"]
        publisher: InstagramPublisher | FacebookPublisher = instagram(http, clock)
        post = meta_post(platform="instagram", account="ig_main", at=T0)
    else:
        routes = fb_routes(mock)
        routes["reels"].mock(return_value=reply("fb_reels_start"))
        upload = routes["upload"]
        publisher = facebook(http, clock)
        post = meta_post()
    publisher.use_platform_refs(refs)

    with pytest.raises(PostConflict):
        publisher.publish(post, video(tmp_path), metadata())

    assert upload.call_count == 0


# --- AC: a restart mid-publish is resolved by id, with no duplicate -----------------------


def test_instagram_restart_after_media_publish_is_resolved_by_the_stored_container(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = ig_routes(mock, "finished", "published")
    killer = Killer("mid media_publish")
    routes["publish"].mock(side_effect=killer)
    post = add_post(env, "instagram", "ig_main")

    with pytest.raises(Crash):
        stage(env, clock, "instagram", instagram(http, clock), "w1").run_once()
    assert current(env, post).status == PostStatus.UPLOADING

    container_id = stored(env, post)["container_id"]
    clock.now += LATER
    restarted(env, clock, "instagram", instagram(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert done.external_id == MEDIA
    assert done.url == RECENT_PERMALINK
    assert routes["media"].call_count == 1  # no second container
    assert len(killer.requests) == 1 and routes["publish"].call_count == 0  # one media_publish
    checked = routes["status"].calls.last.request.url
    assert str(checked.copy_with(query=None)) == graph(container_id)


def test_instagram_restart_after_a_lost_publish_reply_does_not_publish_twice(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = ig_routes(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("reply lost"))
    post = add_post(env, "instagram", "ig_main")

    stage(env, clock, "instagram", instagram(http, clock), "w1").run_once()
    retry = current(env, post)
    assert retry.status == PostStatus.SCHEDULED and retry.attempts == 1

    clock.now = retry.scheduled_at
    restarted(env, clock, "instagram", instagram(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert done.external_id == MEDIA
    assert routes["media"].call_count == 1
    assert routes["publish"].call_count == 1


def test_facebook_restart_after_the_finish_call_is_resolved_by_the_stored_video_id(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = fb_routes(mock)
    killer = Killer("mid finish")
    routes["reels"].side_effect = [reply("fb_reels_start"), killer]
    post = add_post(env, "facebook", "fb_main", at=T0 + timedelta(minutes=20))

    with pytest.raises(Crash):
        stage(env, clock, "facebook", facebook(http, clock), "w1").run_once()
    assert current(env, post).status == PostStatus.UPLOADING

    video_id = stored(env, post)["video_id"]
    clock.now += LATER
    restarted(env, clock, "facebook", facebook(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert done.external_id == video_id == VIDEO_ID
    assert start_calls(routes["reels"]) == 1  # no second Reel
    assert finish_calls(routes["reels"]) == 0 and len(killer.requests) == 1  # one finish
    assert b"upload_phase=finish" in killer.requests[0].content
    assert routes["video"].call_count == 1


def test_facebook_restart_after_a_lost_finish_reply_does_not_upload_twice(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = fb_routes(mock)
    routes["reels"].side_effect = [reply("fb_reels_start"), httpx.ReadTimeout("reply lost")]
    post = add_post(env, "facebook", "fb_main", at=T0 + timedelta(minutes=20))

    stage(env, clock, "facebook", facebook(http, clock), "w1").run_once()
    retry = current(env, post)
    assert retry.status == PostStatus.SCHEDULED and retry.attempts == 1

    clock.now = retry.scheduled_at
    restarted(env, clock, "facebook", facebook(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert done.external_id == VIDEO_ID
    assert start_calls(routes["reels"]) == 1


def test_facebook_restart_after_a_failed_upload_retries_with_a_new_reel(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    # The finish call was never sent, so the stored Reel can never go live: the retry
    # uploads again instead of handing the post to a human.
    routes = fb_routes(mock)
    routes["reels"].side_effect = [
        reply("fb_reels_start"),
        reply("fb_reels_start"),
        reply("fb_reels_finish"),
    ]
    routes["upload"].side_effect = [reply("error_transient", 500), reply("fb_upload_ok")]
    post = add_post(env, "facebook", "fb_main", at=T0 + timedelta(minutes=20))

    stage(env, clock, "facebook", facebook(http, clock), "w1").run_once()
    retry = current(env, post)
    assert retry.status == PostStatus.SCHEDULED and retry.attempts == 1

    clock.now = retry.scheduled_at
    restarted(env, clock, "facebook", facebook(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert start_calls(routes["reels"]) == 2
    assert finish_calls(routes["reels"]) == 1
    assert routes["video"].call_count == 0


def test_instagram_restart_before_media_publish_never_publishes_the_stored_container(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    # Killed during the upload: media_publish was never sent, so the container cannot be
    # live. The stale pass keeps its policy (a human checks; never re-published blindly).
    routes = ig_routes(mock)
    routes["upload"].mock(side_effect=Killer("mid upload"))
    post = add_post(env, "instagram", "ig_main")

    with pytest.raises(Crash):
        stage(env, clock, "instagram", instagram(http, clock), "w1").run_once()
    clock.now += LATER
    restarted(env, clock, "instagram", instagram(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POST_FAILED
    assert "not found on the platform" in (done.error or "")
    assert routes["media"].call_count == 1
    assert routes["publish"].call_count == 0
    assert routes["status"].call_count == 0


# --- no stored ref: today's behaviour -----------------------------------------------------


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_a_post_without_a_stored_ref_falls_back_to_the_in_memory_lookup(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock, platform: str
) -> None:
    # A post left uploading before CLP-122 (no platform_ref): not found, a human checks.
    routes = ig_routes(mock) if platform == "instagram" else fb_routes(mock)
    account = "ig_main" if platform == "instagram" else "fb_main"
    post = add_post(env, platform, account)
    repo.claim_due(env.conn, T0, "dead", platforms=[platform])
    publisher = instagram(http, clock) if platform == "instagram" else facebook(http, clock)

    clock.now += LATER
    stage(env, clock, platform, publisher, "w2").run_once()

    done = current(env, post)
    assert done.status == PostStatus.POST_FAILED
    assert "not found on the platform" in (done.error or "")
    assert all(route.call_count == 0 for route in routes.values())


def test_a_publisher_without_a_store_keeps_its_progress_in_memory(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished", "published")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    publisher = instagram(http, clock)
    post = meta_post(platform="instagram", account="ig_main", at=T0)

    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    found = publisher.lookup(post.idempotency_key)
    assert found == PublishResult(external_id=MEDIA, url=RECENT_PERMALINK)
    assert instagram(http, clock).lookup(post.idempotency_key) is None  # a restart forgets


IG_REF = {
    "container_id": CONTAINER,
    "caption_sha256": "ab",
    "started_at": "2027-01-15T01:00:00Z",
    "publish_sent": True,
}
FB_REF = {"video_id": VIDEO_ID, "finish_sent": True}


@pytest.mark.parametrize(
    ("platform", "raw"),
    [
        ("instagram", "not json"),
        ("facebook", "not json"),
        ("instagram", "[]"),
        ("facebook", "[" * 2000 + "]" * 2000),
        ("instagram", json.dumps({**IG_REF, "pad": "x" * 4100})),
        ("facebook", json.dumps({**FB_REF, "account_id": 7})),
        ("facebook", json.dumps({**FB_REF, "account_id": "fb_other"})),
        ("instagram", json.dumps({**IG_REF, "account_id": "ig_other"})),
        ("facebook", json.dumps({**FB_REF, "finish_sent": "yes"})),
        ("instagram", json.dumps({**IG_REF, "started_at": "yesterday"})),
        ("instagram", json.dumps({**IG_REF, "started_at": 1700000000})),
        ("instagram", json.dumps({**IG_REF, "caption_sha256": None})),
    ],
)
def test_an_unreadable_stored_ref_is_handed_to_a_human(
    http: httpx.Client, clock: Clock, platform: str, raw: str
) -> None:
    refs = (
        Refs(saved={"idem-0001": raw})
        if platform == "instagram"
        else fb_refs(saved={"idem-0001": raw})
    )
    publisher = instagram(http, clock) if platform == "instagram" else facebook(http, clock)
    publisher.use_platform_refs(refs)

    with pytest.raises(MetaError, match="stored upload reference is unreadable"):
        publisher.lookup("idem-0001")


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_a_stored_id_that_is_not_a_graph_id_is_never_put_in_a_url(
    http: httpx.Client, clock: Clock, platform: str
) -> None:
    ref = (
        {**IG_REF, "container_id": "../me"}
        if platform == "instagram"
        else {**FB_REF, "video_id": "x/y"}
    )
    publisher = instagram(http, clock) if platform == "instagram" else facebook(http, clock)
    saved = {"idem-0001": json.dumps(ref)}
    publisher.use_platform_refs(
        Refs(saved=saved) if platform == "instagram" else fb_refs(saved=saved)
    )

    with pytest.raises(MetaError, match="unexpected stored"):
        publisher.lookup("idem-0001")


# --- the stored id wins over this process's memory -----------------------------------------


def test_facebook_lookup_checks_the_latest_stored_reel_not_an_older_one_in_memory(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    # This worker's attempt 1 is in its memory; another worker's attempt 2 stored a newer
    # Reel on the post since. The lookup must check the newer one.
    routes = fb_routes(mock)
    routes["reels"].side_effect = [reply("fb_reels_start"), httpx.ReadTimeout("lost")]
    newer = mock.get(graph("4444444444")).mock(return_value=reply("fb_video_scheduled"))
    refs = fb_refs()
    publisher = facebook(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post()
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    refs.saved[post.idempotency_key] = json.dumps({**FB_REF, "video_id": "4444444444"})

    found = publisher.lookup(post.idempotency_key)

    assert found is not None and found.external_id == "4444444444"
    assert newer.call_count == 1 and routes["video"].call_count == 0


def test_instagram_lookup_checks_the_latest_stored_container_not_an_older_one_in_memory(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    newer = mock.get(graph("4444444444")).mock(return_value=reply("ig_status_published"))
    refs = Refs()
    publisher = instagram(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    stored = json.loads(refs.saved[post.idempotency_key])
    refs.saved[post.idempotency_key] = json.dumps({**stored, "container_id": "4444444444"})

    found = publisher.lookup(post.idempotency_key)

    assert found == PublishResult(external_id=MEDIA, url=RECENT_PERMALINK)
    assert newer.call_count == 1 and routes["status"].call_count == 1  # only the publish wait


def test_instagram_keeps_its_one_recheck_for_the_same_stored_container(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished", "finished", "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    publisher = instagram(http, clock)
    publisher.use_platform_refs(Refs())
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    with pytest.raises(PublishDeferred):
        publisher.lookup(post.idempotency_key)
    assert publisher.lookup(post.idempotency_key) is None


# --- review round 1 (CLP-122) ---------------------------------------------------------------


def test_lookup_uses_the_posts_own_account_never_one_named_in_the_ref(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    video_get = fb_routes(mock)["video"]
    publisher = facebook(http, clock)
    publisher.use_platform_refs(fb_refs(saved={"idem-0001": json.dumps(FB_REF)}))

    assert publisher.lookup("idem-0001") is not None
    assert video_get.calls.last.request.headers["Authorization"] == f"OAuth {FB_TOKEN}"


def test_instagram_does_not_repeat_its_recheck_after_a_restart(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished", "finished", "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    refs = Refs()
    first = instagram(http, clock)
    first.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        first.publish(post, video(tmp_path), metadata())
    with pytest.raises(PublishDeferred):
        first.lookup(post.idempotency_key)
    assert json.loads(refs.saved[post.idempotency_key])["rechecked"] is True

    second = instagram(http, clock)  # the restarted worker
    second.use_platform_refs(refs)

    assert second.lookup(post.idempotency_key) is None  # not deferred a second time
    assert routes["status"].call_count == 3  # the publish wait, then one check each


def test_a_recheck_that_cannot_be_stored_still_defers(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished", "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    refs = Refs(refuse=lambda ref: ref.get("rechecked") is True)
    publisher = instagram(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())

    with pytest.raises(PublishDeferred):
        publisher.lookup(post.idempotency_key)


def test_instagram_restart_after_media_publish_answered_is_resolved_by_the_media_id(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = ig_routes(mock, "finished")
    routes["permalink"].side_effect = [
        Killer("after media_publish"),
        httpx.Response(200, json=MEDIA_NODE),
    ]
    post = add_post(env, "instagram", "ig_main")

    with pytest.raises(Crash):
        stage(env, clock, "instagram", instagram(http, clock), "w1").run_once()
    assert stored(env, post)["media_id"] == MEDIA
    clock.now += LATER
    restarted(env, clock, "instagram", instagram(http, clock))

    done = current(env, post)
    assert done.status == PostStatus.POSTED
    assert done.external_id == MEDIA and done.url == IG_PERMALINK
    assert routes["publish"].call_count == 1
    assert routes["recent"].call_count == 0  # no caption and time matching
    assert routes["status"].call_count == 1  # only the publish wait


@pytest.mark.parametrize(
    "media_reply",
    [
        httpx.Response(400, json=recorded("error_invalid_parameter")),
        httpx.Response(200, json={**MEDIA_NODE, "id": "17920238422039999"}),
        httpx.Response(200, json={**MEDIA_NODE, "owner": {"id": "17841400000000999"}}),
        httpx.Response(200, json={**MEDIA_NODE, "timestamp": "2027-01-15T00:50:00+0000"}),
        httpx.Response(200, json={k: v for k, v in MEDIA_NODE.items() if k != "timestamp"}),
        httpx.Response(200, json={**MEDIA_NODE, "timestamp": "soon"}),
        httpx.Response(200, json={**MEDIA_NODE, "caption": "Another reel"}),
        httpx.Response(200, json={k: v for k, v in MEDIA_NODE.items() if k != "caption"}),
    ],
    ids=[
        "error",
        "other-id",
        "other-owner",
        "too-old",
        "no-timestamp",
        "bad-timestamp",
        "other-caption",
        "no-caption",
    ],
)
def test_a_stored_media_id_that_is_not_confirmed_falls_back_to_matching(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, media_reply: httpx.Response
) -> None:
    routes = ig_routes(mock, "published")
    routes["permalink"].mock(return_value=media_reply)
    digest = hashlib.sha256(metadata().caption.encode()).hexdigest()
    ref = {**IG_REF, "caption_sha256": digest, "media_id": MEDIA}
    publisher = instagram(http, clock)
    publisher.use_platform_refs(Refs(saved={"idem-0001": json.dumps(ref)}))

    found = publisher.lookup("idem-0001")

    assert found == PublishResult(external_id=MEDIA, url=RECENT_PERMALINK)
    assert routes["permalink"].call_count == 1 and routes["recent"].call_count == 1


@pytest.mark.parametrize(
    "node",
    [MEDIA_NODE, {k: v for k, v in MEDIA_NODE.items() if k != "owner"}],
    ids=["owner", "no-owner"],
)
def test_a_stored_media_id_is_confirmed_by_owner_and_time(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, node: dict
) -> None:
    routes = ig_routes(mock)
    routes["permalink"].mock(return_value=httpx.Response(200, json=node))
    digest = hashlib.sha256(metadata().caption.encode()).hexdigest()
    ref = {**IG_REF, "caption_sha256": digest, "media_id": MEDIA}
    publisher = instagram(http, clock)
    publisher.use_platform_refs(Refs(saved={"idem-0001": json.dumps(ref)}))

    assert publisher.lookup("idem-0001") == PublishResult(external_id=MEDIA, url=IG_PERMALINK)
    fields = routes["permalink"].calls.last.request.url.params["fields"].split(",")
    assert {"id", "caption", "owner", "timestamp"} <= set(fields)
    assert routes["status"].call_count == 0 and routes["recent"].call_count == 0


def test_memory_takes_the_stored_media_id_of_the_same_container(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    # This worker's memory knows the container; another worker since stored the media id
    # for the same container. It is used, not this worker's older copy.
    routes = ig_routes(mock, "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    routes["permalink"].mock(return_value=httpx.Response(200, json=MEDIA_NODE))
    refs = Refs()
    publisher = instagram(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    newer = {**json.loads(refs.saved[post.idempotency_key]), "media_id": MEDIA}
    refs.saved[post.idempotency_key] = json.dumps(newer)

    found = publisher.lookup(post.idempotency_key)

    assert found == PublishResult(external_id=MEDIA, url=IG_PERMALINK)
    assert routes["status"].call_count == 1  # only the publish wait


def test_memory_takes_the_stored_recheck_of_the_same_container(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path
) -> None:
    routes = ig_routes(mock, "finished", "finished")
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    refs = Refs()
    publisher = instagram(http, clock)
    publisher.use_platform_refs(refs)
    post = meta_post(platform="instagram", account="ig_main", at=T0)
    with pytest.raises(MetaError):
        publisher.publish(post, video(tmp_path), metadata())
    newer = {**json.loads(refs.saved[post.idempotency_key]), "rechecked": True}
    refs.saved[post.idempotency_key] = json.dumps(newer)

    assert publisher.lookup(post.idempotency_key) is None  # no second deferral


def test_a_media_id_that_cannot_be_stored_never_fails_the_live_post(
    mock: respx.MockRouter,
    http: httpx.Client,
    clock: Clock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ig_routes(mock, "finished")
    publisher = instagram(http, clock)
    publisher.use_platform_refs(Refs(refuse=lambda ref: ref.get("media_id") is not None))
    post = meta_post(platform="instagram", account="ig_main", at=T0)

    with caplog.at_level(logging.WARNING):
        result = publisher.publish(post, video(tmp_path), metadata())

    assert result == PublishResult(external_id=MEDIA, url=IG_PERMALINK)
    assert "could not store" in caplog.text


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_a_refused_sent_flag_save_stops_before_the_publishing_call(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock, tmp_path: Path, platform: str
) -> None:
    if platform == "instagram":
        routes = ig_routes(mock, "finished")
        publisher: InstagramPublisher | FacebookPublisher = instagram(http, clock)
        publisher.use_platform_refs(Refs(refuse=lambda ref: ref.get("publish_sent") is True))
        post = meta_post(platform="instagram", account="ig_main", at=T0)
    else:
        routes = fb_routes(mock)
        routes["reels"].side_effect = [reply("fb_reels_start"), reply("fb_reels_finish")]
        publisher = facebook(http, clock)
        publisher.use_platform_refs(fb_refs(refuse=lambda ref: ref.get("finish_sent") is True))
        post = meta_post()

    with pytest.raises(PostConflict):
        publisher.publish(post, video(tmp_path), metadata())

    assert routes["upload"].call_count == 1
    if platform == "instagram":
        assert routes["publish"].call_count == 0
    else:
        assert finish_calls(routes["reels"]) == 0


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_a_worker_that_loses_its_claim_mid_upload_never_publishes(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock, platform: str
) -> None:
    account = "ig_main" if platform == "instagram" else "fb_main"
    post = add_post(
        env, platform, account, at=T0 if platform == "instagram" else T0 + timedelta(minutes=20)
    )
    routes = ig_routes(mock, "finished") if platform == "instagram" else fb_routes(mock)
    if platform == "facebook":
        routes["reels"].side_effect = [reply("fb_reels_start"), reply("fb_reels_finish")]
    upload_reply = "ig_upload_ok" if platform == "instagram" else "fb_upload_ok"

    later = Clock(T0 + LATER)
    w2_passes: list[int] = []

    def taken_over(_: httpx.Request) -> httpx.Response:
        # Mid-upload, w1 looks dead to a second worker (no heartbeat here): w2's stage, on
        # its own connection with its own publisher and StoredRefs, takes the post over.
        with closing(connect(env.db_path)) as conn:
            w2 = instagram(http, later) if platform == "instagram" else facebook(http, later)
            w2_passes.append(stage(env, later, platform, w2, "w2", conn).run_once())
        assert current(env, post).worker_id != "w1"
        return reply(upload_reply)

    routes["upload"].mock(side_effect=taken_over)
    publisher = instagram(http, clock) if platform == "instagram" else facebook(http, clock)

    stage(env, clock, platform, publisher, "w1").run_once()

    assert w2_passes == [1]
    # w2 found the upload unsent (stage policy: a human checks); w1's sent-flag save is
    # refused, so the publishing call never goes out.
    assert current(env, post).status == PostStatus.POST_FAILED
    if platform == "instagram":
        assert routes["publish"].call_count == 0
        assert stored(env, post)["publish_sent"] is False
    else:
        assert finish_calls(routes["reels"]) == 0
        assert stored(env, post)["finish_sent"] is False


@dataclass
class DieAfterSentSave:
    """Wraps the stage's store: the worker dies right after the sent flag is saved, before
    the publishing call goes out."""

    inner: object
    flag: str

    def save(self, idempotency_key: str, ref: str) -> None:
        self.inner.save(idempotency_key, ref)  # type: ignore[attr-defined]
        if json.loads(ref).get(self.flag) is True:
            raise Crash("worker killed before the publishing call")

    def load(self, idempotency_key: str) -> StoredRef | None:
        return self.inner.load(idempotency_key)  # type: ignore[attr-defined]


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_a_crash_between_the_sent_flag_and_the_call_is_handed_to_a_human(
    env: Env, mock: respx.MockRouter, http: httpx.Client, clock: Clock, platform: str
) -> None:
    account = "ig_main" if platform == "instagram" else "fb_main"
    post = add_post(
        env, platform, account, at=T0 if platform == "instagram" else T0 + timedelta(minutes=20)
    )
    if platform == "instagram":
        routes = ig_routes(mock, "finished", "finished")
        publisher: InstagramPublisher | FacebookPublisher = instagram(http, clock)
        flag = "publish_sent"
    else:
        routes = fb_routes(mock, "fb_video_unpublished")
        routes["reels"].side_effect = [reply("fb_reels_start")]
        publisher = facebook(http, clock)
        flag = "finish_sent"
    first = stage(env, clock, platform, publisher, "w1")
    publisher.use_platform_refs(DieAfterSentSave(StoredRefs(env.conn, "w1"), flag))

    with pytest.raises(Crash):
        first.run_once()
    clock.now += LATER
    fresh = instagram(http, clock) if platform == "instagram" else facebook(http, clock)
    restarted(env, clock, platform, fresh)

    done = current(env, post)
    assert done.status == PostStatus.POST_FAILED
    assert "may not have reached Meta" in (done.error or "")
    if platform == "instagram":
        assert routes["publish"].call_count == 0
    else:
        assert finish_calls(routes["reels"]) == 0


def test_facebook_lookup_refuses_a_video_of_another_page(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    body = {**recorded("fb_video_scheduled"), "from": {"id": "9999999999", "name": "Other"}}
    video_get = mock.get(graph(VIDEO_ID)).mock(return_value=httpx.Response(200, json=body))
    publisher = facebook(http, clock)
    publisher.use_platform_refs(fb_refs(saved={"idem-0001": json.dumps(FB_REF)}))

    with pytest.raises(MetaError, match="not a video of this Page"):
        publisher.lookup("idem-0001")
    assert "from" in video_get.calls.last.request.url.params["fields"].split(",")


@pytest.mark.parametrize(
    "owner", [{"id": PAGE_ID, "name": "Clipper Test Page"}, None], ids=["this-page", "missing"]
)
def test_facebook_lookup_resolves_a_video_of_this_page_or_without_from(
    mock: respx.MockRouter,
    http: httpx.Client,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    owner: dict | None,
) -> None:
    # Unconfirmed whether Graph returns `from` for an unpublished or scheduled Reel: a
    # missing one is logged and the lookup goes on; only a different Page raises.
    body = recorded("fb_video_scheduled")
    if owner is not None:
        body["from"] = owner
    mock.get(graph(VIDEO_ID)).mock(return_value=httpx.Response(200, json=body))
    publisher = facebook(http, clock)
    publisher.use_platform_refs(fb_refs(saved={"idem-0001": json.dumps(FB_REF)}))

    with caplog.at_level(logging.INFO):
        found = publisher.lookup("idem-0001")

    assert found is not None and found.external_id == VIDEO_ID
    assert ("no `from`" in caplog.text) is (owner is None)


def test_a_token_error_while_confirming_the_media_id_pauses_the_account(
    mock: respx.MockRouter, http: httpx.Client, clock: Clock
) -> None:
    routes = ig_routes(mock, "published")
    routes["permalink"].mock(return_value=reply("error_token_expired", 400))
    digest = hashlib.sha256(metadata().caption.encode()).hexdigest()
    ref = {**IG_REF, "caption_sha256": digest, "media_id": MEDIA}
    publisher = instagram(http, clock)
    publisher.use_platform_refs(Refs(saved={"idem-0001": json.dumps(ref)}))

    with pytest.raises(PublishDeferred):
        publisher.lookup("idem-0001")
    assert routes["status"].call_count == 0 and routes["recent"].call_count == 0
