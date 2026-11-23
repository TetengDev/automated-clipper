"""The approved slot is kept apart from the retry/deferral time (CLP-115, CLP-120).

`publish_at` is the slot a post goes live at; `scheduled_at` is when the stage next tries
it. A YouTube post is uploaded up to its publisher's lead time before the slot with
`publishAt` = the slot, so YouTube's own scheduler publishes it. Backoff and deferrals
keep their own times and are never pulled earlier by the lead time. When a deferral ends,
a post whose slot is still ahead keeps it; one whose slot passed meanwhile gets a fresh
slot from the slot picker, so a backlog released after a pause is spread by the account's
windows, gap and posts per day instead of going out in one burst.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from clipper.db import bundled_migrations, connect, migrate
from clipper.jobs.models import to_iso
from clipper.posting import repo
from clipper.posting.models import Post, PostMetadata, PostStatus, PublishDeferred, PublishResult
from clipper.posting.scheduler import parse_windows
from clipper.posting.stage import BACKOFF_BASE, REPICK_GRACE, PostingStage, allow_all
from clipper.posting.youtube import LEAD_TIME, video_resource
from posting_support import FakeAccount, FakePublisher, LookupPublisher, write_rendered
from review_support import Env, make_env

MANILA = ZoneInfo("Asia/Manila")  # UTC+8, no DST
YT = "yt_main"
RECHECK = timedelta(minutes=5)  # Instagram's `RECHECK_AFTER`


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A Manila wall-clock time in January 2027, as an aware UTC datetime."""
    return datetime(2027, 1, day, hour, minute, tzinfo=MANILA).astimezone(UTC)


T0 = local(15, 6)  # before the 09:00 window opens


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def youtube_account(**overrides: Any) -> FakeAccount:
    return FakeAccount(id=YT, platform="youtube", **overrides)


def make_post(
    env: Env, *, at: datetime, account: str = YT, platform: str = "youtube", held: str | None = None
) -> Post:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    write_rendered(env.media_root / rel)
    clip_id = env.clip(job, status="approved", video_path=rel, title="Chair falls", caption="c")
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id=account,
        platform=platform,
        scheduled_at=at,
        video_path=rel,
        now=T0 - timedelta(days=1),
        held=held,
    )


def get(env: Env, post: Post) -> Post:
    found = repo.get_post(env.conn, post.id)
    assert found is not None
    return found


def stage(
    env: Env,
    clock: Clock,
    publisher: object,
    *accounts: FakeAccount,
    platform: str = "youtube",
    **kw: Any,
) -> PostingStage:
    return PostingStage(
        conn=env.conn,
        publishers={platform: publisher},  # type: ignore[dict-item]
        media_root=env.media_root,
        clock=clock,
        accounts={a.id: a for a in accounts},
        **{"guard": allow_all, "worker_id": "w1", **kw},
    )


@dataclass
class YouTubeLike(FakePublisher):
    """Builds the real `videos.insert` body for every upload; lead time as YouTube's."""

    clock: Clock = field(default_factory=Clock)
    lead_time: timedelta = LEAD_TIME
    bodies: list[dict[str, Any]] = field(default_factory=list)
    at: list[datetime] = field(default_factory=list)

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        self.at.append(self.clock())
        self.bodies.append(video_resource(post, metadata, now=self.clock()))
        return super().publish(post, video_path, metadata)


@dataclass
class DeferOnce(YouTubeLike):
    """Defers its first upload to `until` (a quota or a paused account), then uploads."""

    until: datetime = T0
    deferred: int = 0

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        if not self.deferred:
            self.deferred += 1
            raise PublishDeferred("YouTube quotaExceeded: deferred", until=self.until)
        return super().publish(post, video_path, metadata)


# --- the slot column -------------------------------------------------------------------------


def test_migration_0013_adds_publish_at_and_backfills_it(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    try:
        before = [m for m in bundled_migrations() if m.version < 13]
        migrate(conn, before)
        stamp = "2027-01-15T01:00:00Z"
        conn.execute(
            "INSERT INTO jobs (id, platform, item_id, creator, source_url, created_at,"
            " updated_at) VALUES (1, 'youtube', 'x', 'MrBeast', '', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO clips (id, job_id, rank, created_at, updated_at) VALUES (1, 1, 1, ?, ?)",
            (stamp, stamp),
        )
        for n, state in enumerate(("scheduled", "posted", "held"), start=1):
            conn.execute(
                "INSERT INTO posts (clip_id, clip_version, account_id, platform, status,"
                " scheduled_at, idempotency_key, video_path, created_at, updated_at)"
                " VALUES (1, 1, ?, 'youtube', ?, ?, ?, ?, ?, ?)",
                (f"a{n}", state, f"2027-01-1{n}T01:00:00Z", f"k{n}", f"v{n}.mp4", stamp, stamp),
            )

        assert 13 in migrate(conn)

        assert (13, "post_publish_at") in [(m.version, m.name) for m in bundled_migrations()]
        rows = dict(conn.execute("SELECT status, publish_at FROM posts").fetchall())
        assert rows == {
            "scheduled": "2027-01-11T01:00:00Z",
            "posted": "2027-01-12T01:00:00Z",
            "held": None,  # a held post has no slot; its time is only when it was held
        }
    finally:
        conn.close()


def test_a_new_post_stores_its_slot_and_a_held_one_has_none(env: Env) -> None:
    slot = local(15, 9)

    assert make_post(env, at=slot).publish_at == slot
    assert make_post(env, at=slot, held="breaks the brief").publish_at is None


def test_reschedule_retry_and_release_set_the_slot(env: Env) -> None:
    moved = make_post(env, at=local(15, 9))
    assert repo.reschedule(env.conn, moved.id, local(15, 11), T0).publish_at == local(15, 11)

    failed = make_post(env, at=local(15, 9))
    env.conn.execute("UPDATE posts SET status = 'post_failed' WHERE id = ?", (failed.id,))
    requeued = repo.retry(env.conn, failed.id, T0, at=local(15, 18))
    assert requeued.publish_at == requeued.scheduled_at == local(15, 18)

    held = make_post(env, at=T0, held="breaks the brief")
    released = repo.release_held(
        env.conn,
        held,
        local(15, 20),
        T0,
        metadata=PostMetadata(title="t", caption="c"),
        clip_version=1,
        video_path=held.video_path,
        campaign_id=None,
    )
    assert released.publish_at == local(15, 20)


def test_the_slot_picker_sees_slots_not_deferral_times(env: Env) -> None:
    # A post deferred to just after now still holds its slot (12:00) for the picker.
    post = make_post(env, at=local(15, 12))
    env.conn.execute(
        "UPDATE posts SET scheduled_at = ?, error = 'paused' WHERE id = ?",
        (to_iso(local(15, 7)), post.id),
    )

    assert repo.account_post_times(env.conn, YT) == [local(15, 12)]


# --- CLP-115 AC1: uploaded before the slot with publishAt = the slot ---------------------------


def test_a_youtube_post_is_uploaded_ahead_of_its_slot_with_publish_at_the_slot(
    env: Env, clock: Clock
) -> None:
    slot = local(15, 9)
    post = make_post(env, at=slot)
    publisher = YouTubeLike(clock=clock)

    clock.now = slot - LEAD_TIME - timedelta(minutes=1)
    stage(env, clock, publisher, youtube_account()).run_once()
    assert publisher.calls == []

    clock.now = slot - LEAD_TIME
    stage(env, clock, publisher, youtube_account()).run_once()

    assert publisher.at == [slot - LEAD_TIME]
    [body] = publisher.bodies
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2027-01-15T01:00:00Z"  # 09:00 Manila
    done = get(env, post)
    assert done.status == PostStatus.POSTED
    assert done.publish_at == done.scheduled_at == slot


def test_the_youtube_publisher_has_a_thirty_minute_lead_time() -> None:
    # Review round 1: 30 minutes still uses YouTube's scheduler and keeps the window in
    # which a pause, a disable or a lapsed campaign cannot stop an uploaded post short.
    assert timedelta(minutes=30) == LEAD_TIME


# --- CLP-115 AC2: backoff and quota deferral are not shortened by the lead time --------------


def test_a_retry_backoff_is_not_pulled_earlier_by_the_lead_time(env: Env, clock: Clock) -> None:
    slot = local(15, 9)
    post = make_post(env, at=slot)
    publisher = YouTubeLike(clock=clock, fail=1)
    clock.now = slot  # a late first attempt fails
    stage(env, clock, publisher).run_once()
    retry_at = slot + BACKOFF_BASE * 2
    failed = get(env, post)
    assert failed.attempts == 1 and failed.scheduled_at == retry_at

    clock.now = retry_at - timedelta(minutes=1)  # well inside the lead time
    stage(env, clock, publisher).run_once()
    assert len(publisher.calls) == 1

    clock.now = retry_at
    stage(env, clock, publisher).run_once()
    assert len(publisher.calls) == 2 and get(env, post).status == PostStatus.POSTED


def test_a_quota_deferral_is_not_shortened_by_the_lead_time(env: Env, clock: Clock) -> None:
    slot = local(15, 12)
    post = make_post(env, at=slot)
    quota_reset = local(15, 11, 50)  # before the slot, less than the lead time before it
    publisher = DeferOnce(clock=clock, until=quota_reset)
    clock.now = slot - LEAD_TIME
    stage(env, clock, publisher, youtube_account()).run_once()

    deferred = get(env, post)
    assert deferred.status == PostStatus.SCHEDULED and deferred.attempts == 0
    assert deferred.scheduled_at == quota_reset
    assert deferred.publish_at == slot  # never overwritten by a deferral

    clock.now = quota_reset - timedelta(minutes=1)
    stage(env, clock, publisher, youtube_account()).run_once()
    assert publisher.bodies == []

    clock.now = quota_reset
    stage(env, clock, publisher, youtube_account()).run_once()
    [body] = publisher.bodies
    assert body["status"]["publishAt"] == "2027-01-15T04:00:00Z"  # the kept 12:00 slot
    assert get(env, post).scheduled_at == slot


def test_a_deferral_far_from_the_slot_waits_for_the_lead_time_too(env: Env, clock: Clock) -> None:
    # Deferred to 06:30 for a 12:00 slot: not uploaded before 10:00 (slot - lead).
    slot = local(15, 12)
    post = make_post(env, at=slot)
    claimed = repo.claim_due(
        env.conn, T0, "w1", platforms={"youtube"}, early={"youtube": slot - T0}
    )
    assert [p.id for p in claimed] == [post.id]
    repo.defer(env.conn, claimed[0], "w1", "paused", local(15, 6, 30), T0)

    for now, due in ((local(15, 6, 30), False), (slot - LEAD_TIME, True)):
        found = repo.claim_due(
            env.conn, now, "w1", platforms={"youtube"}, early={"youtube": LEAD_TIME}
        )
        assert bool(found) is due


def test_a_platform_without_lead_never_goes_before_its_slot_after_a_deferral(env: Env) -> None:
    slot = local(15, 12)
    post = make_post(env, at=slot, account="tt_main", platform="tiktok")
    [claimed] = repo.claim_due(env.conn, slot, "w1", platforms={"tiktok"})
    assert claimed.id == post.id
    # A deferral back to before the slot (a publisher clock skew): still not before it.
    env.conn.execute(
        "UPDATE posts SET status = 'scheduled', scheduled_at = ?, publish_at = ?,"
        " error = 'paused', worker_id = NULL WHERE id = ?",
        (to_iso(local(15, 10)), to_iso(slot), post.id),
    )

    assert repo.claim_due(env.conn, local(15, 11), "w1", platforms={"tiktok"}) == []
    assert len(repo.claim_due(env.conn, slot, "w1", platforms={"tiktok"})) == 1


# --- CLP-115 AC3 (with CLP-120): a post deferred past its slot ---------------------------------


def test_a_post_deferred_past_its_slot_goes_out_at_a_repicked_slot(env: Env, clock: Clock) -> None:
    account = youtube_account()
    slot = local(15, 9)
    post = make_post(env, at=slot)
    quota_reset = local(15, 15)  # the slot (and the 09:00-12:00 window) passed meanwhile
    publisher = DeferOnce(clock=clock, until=quota_reset)
    clock.now = slot - LEAD_TIME
    stage(env, clock, publisher, account).run_once()

    clock.now = quota_reset
    stage(env, clock, publisher, account).run_once()

    # The next free slot is 18:00, more than the lead time away: it waits for it.
    repicked = get(env, post)
    assert repicked.status == PostStatus.SCHEDULED and repicked.attempts == 0
    assert repicked.publish_at == repicked.scheduled_at == local(15, 18)
    assert repicked.error is None
    assert env.reviews(post.clip_id)[-1]["action"] == "post_repicked"
    assert publisher.bodies == []

    clock.now = local(15, 18) - LEAD_TIME
    stage(env, clock, publisher, account).run_once()
    [body] = publisher.bodies
    assert body["status"]["publishAt"] == "2027-01-15T10:00:00Z"  # 18:00 Manila


def test_a_repicked_slot_within_the_lead_time_is_uploaded_at_once(env: Env, clock: Clock) -> None:
    account = youtube_account()
    slot = local(15, 9)
    post = make_post(env, at=slot)
    publisher = DeferOnce(clock=clock, until=local(15, 17, 45))
    clock.now = slot - LEAD_TIME
    stage(env, clock, publisher, account).run_once()

    clock.now = local(15, 17, 45)
    stage(env, clock, publisher, account).run_once()

    assert publisher.at == [local(15, 17, 45)]  # no extra pass, no extra delay
    [body] = publisher.bodies
    assert body["status"]["publishAt"] == "2027-01-15T10:00:00Z"
    assert get(env, post).publish_at == local(15, 18)


def test_a_post_with_no_future_slot_is_published_at_once(env: Env, clock: Clock) -> None:
    # The account is missing from the config, so there is no slot to pick.
    slot = local(15, 9)
    post = make_post(env, at=slot)
    publisher = DeferOnce(clock=clock, until=local(15, 16, 5))
    clock.now = slot - LEAD_TIME
    stage(env, clock, publisher).run_once()

    clock.now = local(15, 16, 5)
    stage(env, clock, publisher).run_once()

    [body] = publisher.bodies
    assert body["status"]["privacyStatus"] == "public"
    assert "publishAt" not in body["status"]
    assert get(env, post).status == PostStatus.POSTED


# --- CLP-120: a backlog after a pause is spread ----------------------------------------------


def test_a_backlog_released_at_once_gets_one_slot_each(env: Env, clock: Clock) -> None:
    # Three posts deferred to the same moment (a paused account resuming) are re-slotted
    # by the windows, the gap and posts per day instead of going out together.
    account = FakeAccount()  # tiktok: 3 a day, 09:00-12:00 and 18:00-22:00, 120 min gap
    backlog = [
        make_post(env, at=at, account="tt_main", platform="tiktok")
        for at in (local(15, 9), local(15, 11), local(15, 18))
    ]
    resume = local(16, 8)
    for post in backlog:
        [claimed] = repo.claim_due(env.conn, post.scheduled_at, "w1", platforms={"tiktok"})
        repo.defer(env.conn, claimed, "w1", "account paused", resume, post.scheduled_at)
    publisher = FakePublisher()
    clock.now = resume

    stage(env, clock, publisher, account, platform="tiktok").run_once()

    assert publisher.calls == []
    slots = sorted(get(env, post).publish_at for post in backlog)  # type: ignore[type-var]
    assert slots == [local(16, 9), local(16, 11), local(16, 18)]


@dataclass
class Paused(FakePublisher):
    """Defers every upload by an hour until `resume` (an expired token, like YouTube's
    `PAUSE_RECHECK`), then records when each upload goes live."""

    clock: Clock = field(default_factory=Clock)
    resume: datetime = T0
    lead_time: timedelta = timedelta(0)
    live: list[datetime] = field(default_factory=list)

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        now = self.clock()
        if now < self.resume:
            raise PublishDeferred("account paused: invalid_grant", until=now + timedelta(hours=1))
        slot = post.publish_at
        self.live.append(slot if slot is not None and slot > now else now)
        return super().publish(post, video_path, metadata)


@pytest.mark.parametrize(
    ("platform", "lead"), [("tiktok", timedelta(0)), ("youtube", LEAD_TIME)], ids=["no-lead", "yt"]
)
def test_a_one_day_pause_backlog_is_spread_by_windows_and_posts_per_day(
    env: Env, clock: Clock, platform: str, lead: timedelta
) -> None:
    account = FakeAccount(id=f"{platform}_main", platform=platform)
    day_one = [local(15, 9), local(15, 11), local(15, 18)]
    day_two = [local(16, 9), local(16, 11)]  # approved before the pause
    posts = [
        make_post(env, at=at, account=account.id, platform=platform) for at in day_one + day_two
    ]
    resume = local(16, 7)  # a 1-day pause, from 07:00 on the 15th
    publisher = Paused(clock=clock, resume=resume, lead_time=lead)
    posting = stage(env, clock, publisher, account, platform=platform)
    clock.now = local(15, 7)

    while clock.now < local(18, 0):
        posting.run_once()
        clock.now += timedelta(minutes=5)

    assert all(get(env, p).status == PostStatus.POSTED for p in posts)
    # The posts approved for the 16th were never deferred: they keep their slots.
    assert [get(env, p).publish_at for p in posts[3:]] == day_two
    live = sorted(publisher.live)
    assert len(live) == len(posts)
    assert all(t >= resume for t in live)
    windows = parse_windows(account.windows)
    per_day: dict[object, list[datetime]] = {}
    for t in live:
        at = t.astimezone(MANILA)
        assert any(start <= at.time() <= end for start, end in windows), at
        per_day.setdefault(at.date(), []).append(t)
    for times in per_day.values():
        assert len(times) <= account.posts_per_day
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert all(gap >= timedelta(minutes=account.min_gap_minutes) for gap in gaps)
    assert len(per_day) == 2  # 5 posts at 3 a day: the 16th and the 17th


def test_the_slot_is_chosen_inside_the_write(env: Env, clock: Clock, monkeypatch) -> None:
    # Like CLP-106's retries: the re-pick reads the other posts' slots in its transaction.
    seen: list[bool] = []
    real = repo.account_post_times

    def spy(conn: sqlite3.Connection, *args: object, **kwargs: object) -> list[datetime]:
        seen.append(conn.in_transaction)
        return real(conn, *args, **kwargs)  # type: ignore[arg-type]

    post = make_post(env, at=local(15, 9))
    publisher = DeferOnce(clock=clock, until=local(15, 16, 5))
    clock.now = local(15, 9) - LEAD_TIME
    stage(env, clock, publisher, youtube_account()).run_once()
    monkeypatch.setattr(repo, "account_post_times", spy)

    clock.now = local(15, 16, 5)
    stage(env, clock, publisher, youtube_account()).run_once()

    assert get(env, post).publish_at == local(15, 18)
    assert seen and all(seen)


def _deferred_past_its_slot(env: Env, clock: Clock, account: FakeAccount) -> tuple[Post, DeferOnce]:
    post = make_post(env, at=local(15, 9))
    publisher = DeferOnce(clock=clock, until=local(15, 15))
    clock.now = local(15, 9) - LEAD_TIME
    stage(env, clock, publisher, account).run_once()
    clock.now = local(15, 15)
    return post, publisher


def test_a_broken_account_rule_publishes_at_once(
    env: Env, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    post, publisher = _deferred_past_its_slot(env, clock, youtube_account())
    broken = youtube_account(windows=["9am-noon"])

    stage(env, clock, publisher, broken).run_once()

    [body] = publisher.bodies
    assert body["status"]["privacyStatus"] == "public"
    assert get(env, post).status == PostStatus.POSTED
    assert "invalid schedule config" in caplog.text


def test_an_error_while_settling_the_slot_releases_the_claim(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post, publisher = _deferred_past_its_slot(env, clock, youtube_account())

    def broken(*_: object, **__: object) -> list[datetime]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "account_post_times", broken)
    stage(env, clock, publisher, youtube_account()).run_once()

    after = get(env, post)
    assert publisher.bodies == []
    assert after.status == PostStatus.SCHEDULED and after.attempts == 0
    assert after.scheduled_at == local(15, 15) + BACKOFF_BASE
    assert after.publish_at == local(15, 9)
    assert after.error is not None and "slot check failed" in after.error


def test_a_post_changed_while_settling_is_left_alone(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post, publisher = _deferred_past_its_slot(env, clock, youtube_account())
    real = repo.settle_slot

    def taken_over(conn: sqlite3.Connection, claimed: Post, *args: object, **kw: object) -> object:
        conn.execute("UPDATE posts SET worker_id = 'w2' WHERE id = ?", (claimed.id,))
        return real(conn, claimed, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "settle_slot", taken_over)
    stage(env, clock, publisher, youtube_account()).run_once()

    assert publisher.bodies == []
    assert get(env, post).worker_id == "w2"


def test_a_late_claim_still_gets_the_slot_its_deferral_ended_on(env: Env, clock: Clock) -> None:
    # A CLP-106 cap deferral sent the post to the free 18:00 slot; the worker claims it a
    # few seconds late. The re-pick searches from the deferral's end, so it is not pushed
    # behind the 20:00 post (exactly one gap later) by the minute rounding.
    post = make_post(env, at=local(15, 9), account="tt_main", platform="tiktok")
    make_post(env, at=local(15, 20), account="tt_main", platform="tiktok")
    env.conn.execute(
        "UPDATE posts SET scheduled_at = ?, error = 'daily limit reached' WHERE id = ?",
        (to_iso(local(15, 18)), post.id),
    )
    publisher = FakePublisher()
    clock.now = local(15, 18) + timedelta(seconds=7)

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    assert [sent.id for sent, _, _ in publisher.calls] == [post.id]
    assert get(env, post).publish_at == clock.now  # it went live now: the real time


@dataclass
class Rechecking(LookupPublisher):
    """Instagram's lookup: a container still FINISHED is checked again `RECHECK_AFTER`
    later (a 5-minute deferral), then reported not found, so the stage publishes."""

    clock: Clock = field(default_factory=Clock)
    at: list[datetime] = field(default_factory=list)

    def lookup(self, idempotency_key: str) -> PublishResult | None:
        self.lookups.append(idempotency_key)
        if len(self.lookups) == 1:
            raise PublishDeferred("instagram: not published yet", until=self.clock() + RECHECK)
        return None

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        self.at.append(self.clock())
        return super().publish(post, video_path, metadata)


def test_a_five_minute_recheck_past_the_slot_posts_at_once(env: Env, clock: Clock) -> None:
    # The lead's grace window: a retry at 09:00 deferred 5 minutes by Instagram's lookup
    # recheck posts at 09:05, not at 18:00 behind the 11:00 post one gap later.
    account = FakeAccount(id="ig_main", platform="instagram")
    post = make_post(env, at=local(15, 9), account="ig_main", platform="instagram")
    make_post(env, at=local(15, 11), account="ig_main", platform="instagram")
    env.conn.execute("UPDATE posts SET attempts = 1 WHERE id = ?", (post.id,))
    publisher = Rechecking(clock=clock)
    posting = stage(env, clock, publisher, account, platform="instagram")
    clock.now = local(15, 9)
    posting.run_once()
    assert get(env, post).scheduled_at == local(15, 9) + RECHECK

    clock.now = local(15, 9) + RECHECK
    posting.run_once()

    assert publisher.at == [local(15, 9, 5)]
    done = get(env, post)
    assert done.status == PostStatus.POSTED
    assert done.scheduled_at == local(15, 9)  # kept, not re-picked
    assert done.publish_at == local(15, 9, 5)  # when it really went live
    actions = [r["action"] for r in env.reviews(post.clip_id)]
    assert "post_slot_kept" in actions and "post_repicked" not in actions


def test_a_youtube_slot_kept_in_the_grace_window_uploads_public_at_once(
    env: Env, clock: Clock
) -> None:
    post = make_post(env, at=local(15, 9))
    publisher = DeferOnce(clock=clock, until=local(15, 9, 20))
    clock.now = local(15, 9) - LEAD_TIME
    stage(env, clock, publisher, youtube_account()).run_once()

    clock.now = local(15, 9, 20)
    stage(env, clock, publisher, youtube_account()).run_once()

    [body] = publisher.bodies
    assert body["status"]["privacyStatus"] == "public" and "publishAt" not in body["status"]
    assert get(env, post).publish_at == local(15, 9, 20)


@pytest.mark.parametrize(
    ("late", "slot"),
    [(REPICK_GRACE, local(15, 9, 30)), (REPICK_GRACE + timedelta(minutes=1), local(15, 18))],
    ids=["30-min-kept", "31-min-repicked"],
)
def test_only_a_slot_passed_by_more_than_the_grace_is_repicked(
    env: Env, clock: Clock, late: timedelta, slot: datetime
) -> None:
    post = make_post(env, at=local(15, 9), account="tt_main", platform="tiktok")
    make_post(env, at=local(15, 11), account="tt_main", platform="tiktok")
    [claimed] = repo.claim_due(env.conn, local(15, 9), "w1", platforms={"tiktok"})
    repo.defer(env.conn, claimed, "w1", "paused", local(15, 9) + late, local(15, 9))
    publisher = FakePublisher()
    clock.now = local(15, 9) + late

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    assert get(env, post).publish_at == slot
    assert len(publisher.calls) == (1 if slot == local(15, 9, 30) else 0)


def test_the_grace_window_is_thirty_minutes() -> None:
    assert timedelta(minutes=30) == REPICK_GRACE


# --- review round 1 ----------------------------------------------------------------------------

ALL_DAY = ["00:00-23:59"]


def posted(env: Env, post: Post, *, uploaded: datetime) -> None:
    """As the stage leaves a post it uploaded at `uploaded` (live at its slot)."""
    env.conn.execute(
        "UPDATE posts SET status = 'posted', posted_at = ?, updated_at = ? WHERE id = ?",
        (to_iso(uploaded), to_iso(uploaded), post.id),
    )


def deferred(env: Env, post: Post, until: datetime, *, platform: str = "youtube") -> None:
    """Claim `post` at its slot and defer it to `until` (a paused account)."""
    [claimed] = repo.claim_due(env.conn, post.scheduled_at, "w0", platforms={platform})
    assert claimed.id == post.id
    repo.defer(env.conn, claimed, "w0", "account paused", until, post.scheduled_at)


def test_an_early_uploaded_neighbour_going_live_later_holds_off_a_grace_kept_post(
    env: Env, clock: Clock
) -> None:
    # Trace (a): A (slot 10:30) went up early, at 08:00. B's 10:00 slot was deferred to 10:25
    # and kept by the grace window: going live at 10:25 is 5 minutes before A, inside the
    # gap, though A's upload is more than a gap before B.
    a = make_post(env, at=local(15, 10, 30))
    posted(env, a, uploaded=local(15, 8))
    b = make_post(env, at=local(15, 10))
    deferred(env, b, local(15, 10, 25))
    publisher = YouTubeLike(clock=clock)
    clock.now = local(15, 10, 25)

    stage(env, clock, publisher, youtube_account()).run_once()

    assert publisher.bodies == []
    after = get(env, b)
    assert after.status == PostStatus.SCHEDULED and after.scheduled_at >= local(15, 12, 30)
    assert after.error is not None and "gap" in after.error


def test_a_neighbour_uploaded_hours_early_still_counts_for_the_gap(env: Env, clock: Clock) -> None:
    # Trace (b): A goes live at 13:00 but was uploaded at 11:00; B is rescheduled to 13:30
    # with a 120-minute gap, so B is deferred rather than going live 30 minutes after A.
    a = make_post(env, at=local(15, 13))
    posted(env, a, uploaded=local(15, 11))
    b = make_post(env, at=local(15, 9))
    repo.reschedule(env.conn, b.id, local(15, 13, 30), T0)
    publisher = YouTubeLike(clock=clock)
    clock.now = local(15, 13, 30) - LEAD_TIME

    stage(env, clock, publisher, youtube_account(windows=ALL_DAY)).run_once()

    assert publisher.bodies == []
    assert get(env, b).scheduled_at >= local(15, 15)


@pytest.mark.parametrize(
    ("slot", "held_off"),
    [(local(16, 9), True), (local(15, 23, 30), False)],
    ids=["slot-day-is-full", "upload-day-is-free"],
)
def test_an_upload_before_midnight_counts_on_its_slots_day(
    env: Env, clock: Clock, slot: datetime, held_off: bool
) -> None:
    # Trace (c): a 23:00 upload for a 00:30 slot fills the 16th (one post a day), not the 15th.
    account = youtube_account(posts_per_day=1, min_gap_minutes=0, windows=ALL_DAY)
    early = make_post(env, at=local(16, 0, 30))
    posted(env, early, uploaded=local(15, 23))
    make_post(env, at=slot)
    publisher = YouTubeLike(clock=clock)
    clock.now = slot

    stage(env, clock, publisher, account).run_once()

    assert (publisher.bodies == []) is held_off


def test_a_worker_back_after_the_last_window_moves_the_backlog_to_the_next_windows(
    env: Env, clock: Clock
) -> None:
    # Two posts deferred to 15:00 by a pause; the worker is down until 23:00, after the
    # 18:00-22:00 window closed. They take the next day's windows instead of 23:00.
    posts = [
        make_post(env, at=at, account="tt_main", platform="tiktok")
        for at in (local(15, 9), local(15, 11))
    ]
    for post in posts:
        deferred(env, post, local(15, 15), platform="tiktok")
    publisher = FakePublisher()
    clock.now = local(15, 23)

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    assert publisher.calls == []
    assert sorted(get(env, p).publish_at for p in posts) == [local(16, 9), local(16, 11)]  # type: ignore[type-var]


def test_a_late_claim_past_the_grace_is_repicked_though_the_deferral_ended_inside_it(
    env: Env, clock: Clock
) -> None:
    post = make_post(env, at=local(15, 9), account="tt_main", platform="tiktok")
    make_post(env, at=local(15, 11), account="tt_main", platform="tiktok")
    deferred(env, post, local(15, 9, 10), platform="tiktok")  # inside the grace window
    publisher = FakePublisher()
    clock.now = local(15, 9, 45)  # but the worker only gets to it 45 minutes after the slot

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    assert publisher.calls == []
    assert get(env, post).publish_at == local(15, 18)


def test_a_repicked_slot_already_behind_now_records_the_real_go_live_time(
    env: Env, clock: Clock
) -> None:
    post = make_post(env, at=local(15, 9), account="tt_main", platform="tiktok")
    deferred(env, post, local(15, 9, 10), platform="tiktok")
    publisher = FakePublisher()
    clock.now = local(15, 9, 45) + timedelta(seconds=30)

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    done = get(env, post)
    assert done.status == PostStatus.POSTED
    assert done.publish_at == clock.now


def test_no_free_slot_for_an_account_with_rules_gives_up_with_an_alert(
    env: Env, clock: Clock
) -> None:
    post, publisher = _deferred_past_its_slot(env, clock, youtube_account())
    no_room = youtube_account(warmup_start=date(2027, 3, 1))  # no allowance for weeks
    alerts: list[str] = []

    stage(env, clock, publisher, no_room, alert=alerts.append).run_once()

    assert publisher.bodies == []
    failed = get(env, post)
    assert failed.status == PostStatus.POST_FAILED
    assert failed.error is not None and "no free slot" in failed.error
    assert len(alerts) == 1 and f"post {post.id}" in alerts[0]


def test_a_post_held_at_publish_time_gives_up_its_slot(env: Env, clock: Clock) -> None:
    post = make_post(env, at=local(15, 9))
    clock.now = local(15, 9)

    stage(env, clock, YouTubeLike(clock=clock), guard=lambda _: "campaign ended").run_once()

    held = get(env, post)
    assert held.status == PostStatus.HELD and held.publish_at is None


def test_a_cap_deferral_reserves_the_time_it_picked(env: Env, clock: Clock) -> None:
    account = FakeAccount(posts_per_day=1, min_gap_minutes=0)
    done = make_post(env, at=local(15, 8), account="tt_main", platform="tiktok")
    posted(env, done, uploaded=local(15, 8))
    due = make_post(env, at=local(15, 9), account="tt_main", platform="tiktok")
    clock.now = local(15, 9)

    stage(env, clock, FakePublisher(), account, platform="tiktok").run_once()

    after = get(env, due)
    assert after.scheduled_at == after.publish_at == local(16, 9)
    assert local(16, 9) in repo.account_post_times(env.conn, "tt_main")


def test_a_platform_without_lead_is_never_early_after_a_repick(env: Env, clock: Clock) -> None:
    # The re-picked slot is the 09:00 window opening, 30 s away: a TikTok post waits for it.
    post = make_post(env, at=local(14, 18), account="tt_main", platform="tiktok")
    almost = local(15, 9) - timedelta(seconds=30)
    deferred(env, post, almost, platform="tiktok")
    publisher = FakePublisher()
    clock.now = almost

    stage(env, clock, publisher, FakeAccount(), platform="tiktok").run_once()

    assert publisher.calls == []
    after = get(env, post)
    assert after.status == PostStatus.SCHEDULED and after.publish_at == local(15, 9)


def test_the_slot_lookup_uses_the_publish_at_index(env: Env) -> None:
    seen: list[str] = []
    env.conn.set_trace_callback(seen.append)
    repo.account_post_times(env.conn, YT, since=T0)
    env.conn.set_trace_callback(None)
    [sql] = [q for q in seen if "FROM posts" in q]

    plan = env.conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()

    assert any("posts_account_publish" in str(tuple(row)) for row in plan)


def _pre_migration_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "old.db")
    migrate(conn, [m for m in bundled_migrations() if m.version < 13])
    stamp = "2027-01-14T01:00:00Z"
    conn.execute(
        "INSERT INTO jobs (id, platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES (1, 'youtube', 'x', 'MrBeast', '', ?, ?)",
        (stamp, stamp),
    )
    conn.execute(
        "INSERT INTO clips (id, job_id, rank, created_at, updated_at) VALUES (1, 1, 1, ?, ?)",
        (stamp, stamp),
    )
    return conn


def test_a_post_deferred_before_the_migration_is_not_claimed_early(tmp_path: Path) -> None:
    conn = _pre_migration_db(tmp_path)
    try:
        until = local(15, 16, 5)  # a quota reset recorded before this migration
        conn.execute(
            "INSERT INTO posts (clip_id, clip_version, account_id, platform, status,"
            " scheduled_at, error, idempotency_key, video_path, created_at, updated_at)"
            " VALUES (1, 1, ?, 'youtube', 'scheduled', ?, 'quotaExceeded', 'k1', 'v.mp4', ?, ?)",
            (YT, to_iso(until), to_iso(T0), to_iso(T0)),
        )
        migrate(conn)
        early = {"youtube": LEAD_TIME}

        assert (
            repo.claim_due(
                conn, until - timedelta(minutes=1), "w1", platforms={"youtube"}, early=early
            )
            == []
        )
        [post] = repo.claim_due(conn, until, "w1", platforms={"youtube"}, early=early)
        assert post.publish_at == post.scheduled_at == until
    finally:
        conn.close()


def test_a_released_post_is_claimed_a_lead_time_ahead_of_its_new_slot(env: Env) -> None:
    held = make_post(env, at=T0, held="breaks the brief")
    slot = local(15, 12)
    repo.release_held(
        env.conn,
        held,
        slot,
        T0,
        metadata=PostMetadata(title="t", caption="c"),
        clip_version=1,
        video_path=held.video_path,
        campaign_id=None,
    )
    early = {"youtube": LEAD_TIME}

    before = slot - LEAD_TIME - timedelta(minutes=1)
    assert repo.claim_due(env.conn, before, "w1", platforms={"youtube"}, early=early) == []
    [claimed] = repo.claim_due(env.conn, slot - LEAD_TIME, "w1", platforms={"youtube"}, early=early)
    assert claimed.id == held.id and claimed.publish_at == slot


def test_two_workers_settling_one_accounts_backlog_pick_different_slots(
    env: Env, clock: Clock
) -> None:
    backlog = [
        make_post(env, at=at, account="tt_main", platform="tiktok")
        for at in (local(15, 9), local(15, 11), local(15, 18))
    ]
    for post in backlog:
        deferred(env, post, local(16, 8), platform="tiktok")
    second = connect(env.db_path)
    try:
        workers = [
            PostingStage(
                conn=conn,
                publishers={"tiktok": FakePublisher()},
                worker_id=name,
                media_root=env.media_root,
                clock=clock,
                accounts={"tt_main": FakeAccount()},
                guard=allow_all,
                batch=1,
            )
            for conn, name in ((env.conn, "w1"), (second, "w2"))
        ]
        clock.now = local(16, 8)
        for worker in (*workers, workers[0]):
            worker.run_once()
    finally:
        second.close()

    slots = sorted(get(env, p).publish_at for p in backlog)  # type: ignore[type-var]
    assert slots == [local(16, 9), local(16, 11), local(16, 18)]
