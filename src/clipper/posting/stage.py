"""Worker stage `posting` (CLP-92): publish due posts through the platform publishers.

Each pass (the worker runs one per poll, see `clipper.jobs.worker.Worker.posting`):

1. Posts left `uploading` for longer than `STALE_UPLOAD` (a worker crashed mid-upload) on
   platforms this worker publishes to are taken over, never re-published. If the
   publisher's `lookup(idempotency_key)` finds the upload, the post is `posted`; in every
   other case (not found, lookup error, no `lookup`) the upload may still be in flight,
   so the post becomes `post_failed` ("uploading state unresolved") and alerts a human.
2. Due `scheduled` posts on platforms with a registered publisher are claimed one at a
   time (atomically moved to `uploading` under this worker's id) and published. A retry
   first asks `lookup` whether the earlier attempt went through. Success stores the
   external id and URL (`posted`). A failure counts an attempt and backs off
   (`now + BACKOFF_BASE * 2**attempts`); the `MAX_ATTEMPTS`th failure is `post_failed`
   and raises an alert. A successful upload whose result cannot be recorded because the
   post changed meanwhile alerts too (a possible duplicate).
   Before publishing, the `guard` (the campaign pre-flight, CLP-97) runs again with the
   current config: a post it refuses, or a guard that fails, is `held` instead.
   A publisher that raises `PublishDeferred` (a quota or upload limit, a paused account;
   CLP-93) puts the post back to `scheduled` at the given time without counting an
   attempt, and its alert, if any, goes to a human.
3. Due posts on a platform with no publisher stay `scheduled`; that is logged once per
   platform per worker run.

A publisher that raises `PublishRejected` (CLP-94) gets the post `post_failed` at once,
with an alert. A publisher with a `lead_time` gets its posts' first attempts that long
before their slot (Facebook and YouTube schedule natively; CLP-115).

CLP-115/CLP-120: a post keeps its slot (`publish_at`) apart from its next attempt
(`scheduled_at`). Retries and deferrals are claimed at their own time, never earlier for
a lead time. Once a deferral is over (`repo.settle_slot`, before the claim-time caps), a
slot still ahead is kept, and so is one passed by at most `REPICK_GRACE` at the later of
the deferral's end and now (the post goes out at once). A slot passed by more is replaced
by the account's next free slot (`pick_slot`, with the other posts' slots, searched from
the deferral's end but never earlier than `REPICK_SLACK` before now), so a backlog
released by a paused account is spread over its windows, gap and posts per day instead
of going out at once. A fresh slot within the lead time (plus `REPICK_SLACK`, on a
platform with a lead time only) is uploaded at once; a later one waits for it, claimed
like a first attempt. With no account rules (or broken ones) the post goes now; an
account whose rules leave no free slot within `MAX_DAYS_AHEAD` days gets the post
`post_failed` with an alert. `publish_at` ends as the real go-live time: the slot when
the platform scheduled the upload natively, else the upload.

If recording a successful upload fails (DB error), the post stays `uploading` and step 1
resolves it later through `lookup`; it is never re-published blindly.

CLP-105:

* Heartbeat. While `publish` runs, a background thread re-stamps the post's `claimed_at`
  every `heartbeat_seconds` (on its own connection from `connect`), so a slow but live
  upload is never taken for a crashed one. It stops when the upload ends, when the post is
  no longer this worker's, or after `MAX_UPLOAD`, so a publish that hangs for ever still
  goes stale and reaches a human. Without `connect` there is no heartbeat.
* `recover(dead_worker_id)` (from `run-worker --recover-id`) hands the dead worker's
  `uploading` posts to the next stale pass at once.
* The worker runs this stage on a thread of its own (`Worker.run_forever`), so it needs
  a connection of its own too.

CLP-106 (with the accounts config, `accounts`):

* A failed attempt retries in the account's next free slot at or after the backoff time
  (`pick_slot`), so retries keep to its windows, gap, posts per day, warm-up and platform
  cap. With no free slot within `MAX_DAYS_AHEAD` days the post is `post_failed` and alerts.
* At claim time (after the lookup of an earlier attempt), in one write transaction:
  a post that would take its account over the `daily_limit` of the local day it publishes
  on (the platform cap on the UTC day for an account not in the config), over the
  platform cap in a rolling 24 hours, or within the minimum gap of another post's go-live
  time on either side, is deferred to the account's next free slot (audit
  `post_deferred`, no attempt counted; that time becomes its slot), or, with no free slot,
  is `post_failed` and alerts. Posts posted, in flight, or with failed attempts in the
  window count (`repo.uploads_between`). The daily limit and the gap use when posts go
  live (an upload scheduled natively counts at its slot; CLP-115 review); the rolling
  API cap counts attempts when they were made. A post claimed before its slot publishes
  at its slot, so the day and the gap are those of `max(now, scheduled_at)`, and a
  deferral is never earlier than the slot nor sooner than `MIN_DEFER` from now.
  One pass handles each post at most once, so it never defers the same post twice.
* Posts of an account missing from the config keep the plain backoff.

CLP-122: a publisher with `use_platform_refs` (Facebook, Instagram) gets a store on
`posts.platform_ref` (`StoredRefs`, this stage's connection and worker id). It keeps the
platform's id for the upload there before each step that can be interrupted, so the
`lookup` of a later worker (the stale pass, or a retry after a restart) checks the upload
by id instead of finding nothing. The save is fail-closed: if it cannot be written (the
post was taken over, or the database stays locked past its busy timeout), the publisher
raises before the next platform call: the upload is aborted (fail-closed), never sent on
without its id on record. The attempt then fails as usual, or, if even that cannot be
recorded, the post stays `uploading` for the stale pass.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clipper.clips import resolve_media_path
from clipper.jobs.models import Clock, utcnow
from clipper.jobs.worker import DEFAULT_MEDIA_ROOT
from clipper.pipeline.common import strip_control
from clipper.posting import repo
from clipper.posting.models import (
    MAX_ATTEMPTS,
    Post,
    PostConflict,
    PostStatus,
    PublishDeferred,
    Publisher,
    PublishRejected,
    PublishResult,
    StoredRef,
)
from clipper.posting.scheduler import (
    HISTORY,
    MAX_DAYS_AHEAD,
    PLATFORM_DAILY_CAPS,
    AccountLike,
    daily_limit,
    day_bounds,
    pick_slot,
)
from clipper.redact import redact

STALE_UPLOAD = timedelta(minutes=30)
HEARTBEAT = timedelta(minutes=1)
"""How often a running upload re-stamps its claim; well inside `STALE_UPLOAD`."""
RECOVER_QUIET = HEARTBEAT * 5
"""`recover` only releases uploads whose claim has not been re-stamped for this long, so
naming a live worker by mistake never hands its upload over. Five beats leave room for a
few minutes of clock skew between hosts (NTP is assumed)."""
HEARTBEAT_BUSY_MS = 2_000
"""The heartbeat's busy timeout: a missed beat is harmless, a stalled result write is not."""
MAX_UPLOAD = timedelta(hours=4)
"""The heartbeat stops after this long, so a publish that never returns goes stale."""
BACKOFF_BASE = timedelta(minutes=5)
MIN_DEFER = timedelta(minutes=5)
"""A deferral is never sooner than this, so one pass cannot spin on a post."""
UNRESOLVED = repo.UNRESOLVED_PREFIX
DEFAULT_BATCH = 10
MAX_STALE_PER_PASS = DEFAULT_BATCH
"""Stale posts resolved per pass at most; the rest wait for the next pass."""
ROLLING_DAY = timedelta(hours=24)
ROLLING_RETRY = timedelta(hours=1)
"""A post held by the rolling 24-hour platform cap is tried again this much later."""
REPICK_SLACK = timedelta(minutes=1)
"""`pick_slot` rounds up to the minute: a re-picked slot this close is uploaded at once."""
REPICK_GRACE = timedelta(minutes=30)
"""A deferral that ends at most this long after the post's slot keeps the slot: the post
goes out at once (the claim-time caps still apply), so a short deferral (Instagram's
5-minute lookup recheck, `MIN_DEFER`) never moves a post by hours. Only a longer one, a
real pause, re-picks the slot (CLP-115/CLP-120)."""

logger = logging.getLogger(__name__)

Alert = Callable[[str], None]
Guard = Callable[[Post], "str | None"]


def log_alert(message: str) -> None:
    """Default alert: an ERROR log line. Discord alerting is CLP-30."""
    logger.error("ALERT: %s", message)


@dataclass
class PostingStage:
    conn: sqlite3.Connection
    publishers: Mapping[str, Publisher]
    worker_id: str
    media_root: Path = DEFAULT_MEDIA_ROOT
    clock: Clock = utcnow
    alert: Alert = log_alert
    guard: Guard | None = None
    """The publish-time campaign pre-flight (CLP-97, `scheduler.publish_guard`): a reason
    holds the claimed post instead of publishing it. Required whenever there are
    publishers (pass `allow_all` to opt out explicitly), so leaving it out never
    publishes without the check."""
    batch: int = DEFAULT_BATCH
    connect: Callable[[], sqlite3.Connection] | None = None
    """Opens a DB connection for the upload heartbeat thread (None: no heartbeat)."""
    heartbeat_seconds: float = HEARTBEAT.total_seconds()
    stop: threading.Event | None = None
    """Set on shutdown: the pass finishes the upload in hand but claims no new post."""
    accounts: Mapping[str, AccountLike] = field(default_factory=dict)
    """Posting accounts by id (CLP-91's config): their slot rules shape retries and caps."""
    _warned: set[str] = field(default_factory=set, init=False, repr=False)

    def recover(self, dead_worker_id: str) -> int:
        """Hand a known-dead worker's `uploading` posts to the next stale pass now. Posts
        whose claim was re-stamped within `RECOVER_QUIET` are left alone: their worker is
        alive (its heartbeat still runs), whatever the operator believes."""
        released, shortened = repo.release_uploads(
            self.conn,
            dead_worker_id,
            self.clock(),
            quiet_for=RECOVER_QUIET,
            stale_after=STALE_UPLOAD,
        )
        logger.info("released %d uploading post(s) claimed by %s", released, dead_worker_id)
        if shortened:
            logger.warning(
                "%d uploading post(s) of %s beat within %s: not released; they go stale in %s"
                " unless that worker is alive",
                shortened,
                dead_worker_id,
                RECOVER_QUIET,
                RECOVER_QUIET,
            )
        return released

    def __post_init__(self) -> None:
        if self.publishers and self.guard is None:
            raise ValueError(
                "PostingStage with publishers needs a guard (scheduler.publish_guard, or"
                " allow_all to publish without the campaign pre-flight)"
            )
        # CLP-122: publishers that track an upload by the platform's id keep it on the
        # post, through this stage's connection (publish and lookup run on its thread).
        refs = StoredRefs(self.conn, self.worker_id)
        for publisher in self.publishers.values():
            use = getattr(publisher, "use_platform_refs", None)
            if callable(use):
                use(refs)

    def run_once(self) -> int:
        """One posting pass; returns how many posts it acted on."""
        handled = self._recover_stale()
        self._warn_unpublishable()
        early = {p: lead for p, pub in self.publishers.items() if (lead := _lead_time(pub))}
        seen: set[int] = set()
        # One post at a time (claim, publish, record), so a post never sits claimed while
        # others upload and cannot go stale before it is even tried.
        for _ in range(self.batch):
            if self.stop is not None and self.stop.is_set():
                break
            due = repo.claim_due(
                self.conn,
                self.clock(),
                self.worker_id,
                platforms=self.publishers.keys(),
                limit=1,
                early=early,
                exclude=seen,
            )
            if not due:
                break
            seen.add(due[0].id)
            self._publish(due[0])
            handled += 1
        return handled

    # --- steps -------------------------------------------------------------------------

    def _warn_unpublishable(self) -> None:
        missing = repo.due_platforms(self.conn, self.clock()) - self.publishers.keys()
        for platform in sorted(missing - self._warned):
            logger.warning("no publisher registered for %s; its due posts stay scheduled", platform)
            self._warned.add(platform)

    def _publish(self, post: Post) -> None:
        publisher = self.publishers[post.platform]
        # Lookup first: an earlier attempt that went live is recorded whatever the caps.
        # After a human's retry (attempts reset, an earlier failure in the audit) a broken
        # lookup does not block the upload: the retry is the override the alert asks for.
        requeued = post.attempts == 0 and repo.had_failed_attempt(self.conn, post)
        if (post.attempts > 0 or requeued) and self._already_live(
            post, publisher, strict=not requeued
        ):
            return
        settled = self._settle_slot(post, publisher)
        if settled is None:
            return
        post = settled
        if self._held_by_caps(post):
            return
        if self._held(post):
            return
        try:
            path = resolve_media_path(self.media_root, post.video_path)
            if path is None:
                raise FileNotFoundError("rendered video is missing")
            metadata = repo.metadata_for(self.conn, post)
            with self._heartbeat(post):
                result = publisher.publish(post, path, metadata)
        except PublishDeferred as exc:
            self._deferred(post, exc)
            return
        except PublishRejected as exc:
            self._give_up(post, f"rejected: {_describe(exc)}")
            return
        except Exception as exc:
            self._failed_attempt(post, _describe(exc))
            return
        # Outside the try: if this write fails the post stays `uploading` and the stale
        # pass resolves it through `lookup` instead of uploading it again.
        self._posted(post, result, uploaded=True)

    @contextmanager
    def _heartbeat(self, post: Post) -> Iterator[None]:
        """Keep `post`'s claim fresh while the body (an upload or a lookup) runs."""
        if self.connect is None:
            yield
            return
        done = threading.Event()
        beat = threading.Thread(
            target=self._beat, args=(post, done), name=f"heartbeat-post-{post.id}", daemon=True
        )
        beat.start()
        try:
            yield
        finally:
            done.set()
            beat.join()

    def _beat(self, post: Post, done: threading.Event) -> None:
        assert self.connect is not None
        started = self.clock()
        conn: sqlite3.Connection | None = None
        try:
            while not done.wait(self.heartbeat_seconds):
                now = self.clock()
                if now - started > MAX_UPLOAD:
                    logger.error(
                        "%s: upload still running after %s; heartbeat stopped",
                        _label(post),
                        MAX_UPLOAD,
                    )
                    return
                try:
                    if conn is None:
                        conn = self.connect()
                        # The factory may use the default 30s; a beat may simply be missed.
                        conn.execute(f"PRAGMA busy_timeout={HEARTBEAT_BUSY_MS:d}")
                    if not repo.touch_claim(conn, post.id, self.worker_id, now):
                        logger.warning("%s: no longer ours; heartbeat stopped", _label(post))
                        return
                except (sqlite3.Error, OSError) as exc:
                    # Retried next beat; the upload itself carries on regardless.
                    logger.warning("%s: heartbeat failed: %s", _label(post), exc)
        finally:
            if conn is not None:
                conn.close()

    def _held(self, post: Post) -> bool:
        """Run the guard; hold the post when it refuses (or fails: fail closed)."""
        if self.guard is None:  # no publishers, so nothing is ever claimed
            return False
        try:
            reason = self.guard(post)
        except Exception as exc:
            reason = f"publish pre-flight failed: {_describe(exc)}"
        if reason is None:
            return False
        try:
            repo.hold_claimed(self.conn, post, self.worker_id, reason, self.clock())
        except PostConflict as exc:
            logger.warning("%s: hold not recorded: %s", _label(post), exc)
            return True
        logger.warning("%s: held by pre-flight: %s", _label(post), strip_control(reason))
        return True

    def _already_live(self, post: Post, publisher: Publisher, *, strict: bool = True) -> bool:
        """Before re-publishing after a failed attempt, ask `lookup` (when there is one)
        whether the earlier attempt went through after all, e.g. it timed out reading the
        response. True when the post was recorded as posted, deferred (the lookup cannot
        check right now) or handed to a human. A lookup that fails hands the post to a
        human, unless not `strict` (a human already requeued it): then it is logged and the
        upload goes ahead."""
        lookup = getattr(publisher, "lookup", None)
        if not callable(lookup):
            return False
        try:
            with self._heartbeat(post):
                found = lookup(post.idempotency_key)
        except PublishDeferred as exc:
            self._deferred(post, exc)
            return True
        except Exception as exc:
            if not strict:
                logger.warning(
                    "%s: lookup failed: %s; uploading as requeued", _label(post), _describe(exc)
                )
                return False
            self._give_up(post, f"{UNRESOLVED}: lookup failed: {_describe(exc)}")
            return True
        if found is None:
            return False
        logger.info("%s: an earlier attempt went through; recorded", _label(post))
        self._posted(post, found)
        return True

    def _recover_stale(self) -> int:
        """Resolve posts a crashed (or very slow) worker left `uploading`. Never re-publish:
        only a `lookup` that finds the upload records it as posted; anything else is
        `post_failed` for a human to check, since the upload may still be in flight."""
        handled = 0
        # One at a time, like due posts: one waiting behind a slow lookup is not claimed.
        for _ in range(MAX_STALE_PER_PASS):
            if self.stop is not None and self.stop.is_set():
                break
            stale = repo.claim_stale(
                self.conn,
                self.clock(),
                self.worker_id,
                older_than=STALE_UPLOAD,
                platforms=self.publishers.keys(),
                limit=1,
            )
            if not stale:
                break
            self._resolve_stale(stale[0])
            handled += 1
        return handled

    def _resolve_stale(self, post: Post) -> None:
        lookup = getattr(self.publishers[post.platform], "lookup", None)
        if not callable(lookup):
            logger.error("%s: crashed mid-upload and cannot be checked", _label(post))
            self._give_up(post, UNRESOLVED)
            return
        try:
            with self._heartbeat(post):  # a slow lookup must not let the claim go stale
                found = lookup(post.idempotency_key)
        except Exception as exc:
            # Never deferred: a deferred post with no attempts would be re-published
            # without a lookup. A human checks it; the publisher's alert still goes out.
            alert = exc.alert if isinstance(exc, PublishDeferred) else None
            if alert:
                self._send(redact(alert), post)
            self._give_up(post, f"{UNRESOLVED}: lookup failed: {_describe(exc)}")
            return
        if found is not None:
            logger.info("%s: upload from a crashed worker found; recorded", _label(post))
            self._posted(post, found)
        else:
            self._give_up(post, f"{UNRESOLVED}: not found on the platform")

    # --- outcomes ----------------------------------------------------------------------

    def _posted(self, post: Post, result: PublishResult, *, uploaded: bool = False) -> None:
        """Record a live post. `uploaded`: this worker just uploaded it, so it goes live at
        its slot when the platform scheduled it, else now (`publish_at`, CLP-115 review);
        an upload found by `lookup` keeps its slot, its go-live time being unknown."""
        now = self.clock()
        live_at = max(post.slot, now) if uploaded else None
        try:
            repo.mark_posted(self.conn, post, self.worker_id, result, now, live_at=live_at)
        except PostConflict as exc:
            # The video is live but the row changed under us: a possible duplicate.
            message = (
                f"{_label(post)} of clip {post.clip_id} was uploaded (external id"
                f" {result.external_id}, url {result.url or '-'}) but the result could not be"
                f" recorded ({exc}); check the platform for a duplicate."
            )
            logger.error("%s: result dropped: %s", _label(post), exc)
            self._send(message, post)
            return
        logger.info("%s: posted (%s)", _label(post), result.external_id)

    def _settle_slot(self, post: Post, publisher: Publisher) -> Post | None:
        """After a deferral: keep the slot, or re-pick one that passed (CLP-115, CLP-120).
        The post to upload, or None when it went back to `scheduled`, was given up (with
        an alert) or changed meanwhile."""
        now = self.clock()
        account = self.accounts.get(post.account_id)

        def pick(conn: sqlite3.Connection) -> datetime | None:
            assert account is not None
            try:
                taken = repo.account_post_times(
                    conn, account.id, exclude_post_id=post.id, since=now - HISTORY
                )
                # From the deferral's end, so a claim a little late keeps the slot a cap
                # deferral chose (pick_slot rounds up to the minute), but never from hours
                # ago after a worker outage.
                return pick_slot(account, taken, max(post.scheduled_at, now - REPICK_SLACK))
            except (ValueError, ZoneInfoNotFoundError) as exc:
                logger.error("account %s: invalid schedule config: %s", account.id, exc)
                return now  # no usable rules: as for an account not in the config

        lead = _lead_time(publisher)
        # A slot a minute away is near enough for a platform that schedules natively; one
        # without a lead time never posts before its slot.
        go_by = now + lead + REPICK_SLACK if lead else now
        no_slot = f"slot passed while deferred; no free slot in the next {MAX_DAYS_AHEAD} days"
        try:
            settled = repo.settle_slot(
                self.conn,
                post,
                self.worker_id,
                now,
                pick=None if account is None else pick,
                go_by=go_by,
                grace=REPICK_GRACE,
                no_slot=no_slot,
            )
        except PostConflict as exc:
            logger.warning("%s: slot not settled: %s", _label(post), exc)
            return None
        except Exception as exc:
            # Never leave the post claimed: the stale pass would take it for a crashed upload.
            reason = f"slot check failed: {_describe(exc)}"
            logger.error("%s: %s; retrying later", _label(post), reason)
            try:
                repo.defer(self.conn, post, self.worker_id, reason, now + BACKOFF_BASE, now)
            except Exception:
                logger.exception("%s: could not release the claim", _label(post))
            return None
        if settled.status == PostStatus.POST_FAILED:
            self._alert(settled, no_slot)
            return None
        if settled.status != PostStatus.UPLOADING:
            logger.info(
                "%s: slot passed while deferred; re-picked %s",
                _label(post),
                settled.slot.isoformat(),
            )
            return None
        return settled

    def _held_by_caps(self, post: Post) -> bool:
        """Claim-time caps (CLP-106). True when the claimed post must not upload now: it
        was deferred (no attempt counted) or, with no free slot, given up with an alert."""
        now = self.clock()
        try:
            decision = repo.defer_claimed(
                self.conn, post, self.worker_id, now, lambda c: self._cap_decision(c, post, now)
            )
        except PostConflict as exc:
            logger.warning("%s: deferral not recorded: %s", _label(post), exc)
            return True
        except Exception as exc:
            # Never upload unchecked, and never leave the post claimed (the stale pass
            # would hand it to a human as an unresolved upload).
            logger.error("%s: cap check failed: %s; retrying later", _label(post), _describe(exc))
            until = max(now + BACKOFF_BASE, post.scheduled_at)
            reason = f"cap check failed: {_describe(exc)}"
            try:
                # A plain deferral: the slot is kept, this time was not picked as a slot.
                repo.defer(self.conn, post, self.worker_id, reason, until, now)
            except Exception:
                logger.exception("%s: could not release the claim", _label(post))
            return True
        if decision is None:
            return False
        if decision.until is None:
            failed = repo.get_post(self.conn, post.id) or post
            self._alert(failed, decision.reason)
        else:
            logger.warning(
                "%s: %s; deferred to %s", _label(post), decision.reason, decision.until.isoformat()
            )
        return True

    def _cap_decision(
        self, conn: sqlite3.Connection, post: Post, now: datetime
    ) -> repo.Deferral | None:
        """Runs inside the claim-check transaction. In order: the account's daily limit
        for the local day the post publishes on (`daily_limit`; the platform cap on the
        UTC day for an account not in the config or with a broken one), the platform cap
        over a rolling 24 hours (counted per API attempt, so from now), and the account's
        minimum gap after its last upload. The claim-time count is the authority on caps;
        the slot picker only plans.

        A post claimed before its slot (a publisher that schedules natively) publishes at
        its slot: the day and the gap are measured at `max(now, scheduled_at)`, and a
        deferral searches from no earlier than the slot and `MIN_DEFER` from now, so it
        never moves the post earlier or makes it due again at once."""
        account = self.accounts.get(post.account_id)
        cap = PLATFORM_DAILY_CAPS.get(post.platform)
        limit = cap
        at = max(now, post.scheduled_at)
        start, end = day_bounds("UTC", at)
        if account is not None:
            try:
                start, end = day_bounds(account.timezone, at)
                limit = daily_limit(account, start.astimezone(ZoneInfo(account.timezone)).date())
            except (ValueError, ZoneInfoNotFoundError) as exc:
                logger.error("account %s: invalid schedule config: %s", account.id, exc)
                account, limit = None, cap
                start, end = day_bounds("UTC", at)
        others = post.id
        floor = max(post.scheduled_at, now + MIN_DEFER)

        def after(moment: datetime, *extra: datetime) -> datetime | None:
            moment = max(moment, floor)
            if account is None:
                return moment
            try:
                taken = repo.account_post_times(
                    conn, account.id, exclude_post_id=others, since=now - HISTORY
                )
                return pick_slot(account, [*taken, *extra], moment)
            except (ValueError, ZoneInfoNotFoundError):
                return moment

        def held(reason: str, until: datetime | None) -> repo.Deferral:
            if until is None:
                reason = f"{reason}; no free slot in the next {MAX_DAYS_AHEAD} days"
            return repo.Deferral(reason, until)

        if limit is not None:
            done = repo.uploads_between(conn, post.account_id, start, end, exclude_post_id=others)
            if done >= limit:
                return held(f"daily limit reached ({done} of {limit} upload(s) today)", after(end))
        if cap is not None:
            recent = repo.uploads_between(
                conn,
                post.account_id,
                now - ROLLING_DAY,
                now + timedelta(seconds=1),
                exclude_post_id=others,
                per_attempt=True,
            )
            if recent >= cap:
                return held(
                    f"{post.platform} cap reached ({recent} of {cap} upload(s) in 24 hours)",
                    after(now + ROLLING_RETRY),
                )
        if account is not None and account.min_gap_minutes > 0:
            gap = timedelta(minutes=account.min_gap_minutes)
            near = repo.go_lives_near(conn, account.id, at, gap, exclude_post_id=others)
            if near:
                return held(
                    f"within the {account.min_gap_minutes}-minute gap of another post"
                    f" going live at {near[0].isoformat()}",
                    after(near[0] + gap, *near),
                )
        return None

    def _retry_at(self, post: Post) -> Callable[[sqlite3.Connection, datetime], datetime | None]:
        """For `record_failure`: inside its transaction, the account's next free slot at or
        after the backoff time (the backoff time itself for an account not in the config
        or with a broken one)."""
        account = self.accounts.get(post.account_id)
        now = self.clock()

        def slot(conn: sqlite3.Connection, earliest: datetime) -> datetime | None:
            if account is None:
                return earliest
            try:
                taken = repo.account_post_times(
                    conn, account.id, exclude_post_id=post.id, since=now - HISTORY
                )
                return pick_slot(account, taken, earliest)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                logger.error("account %s: invalid schedule config: %s", account.id, exc)
                return earliest

        return slot

    def _failed_attempt(self, post: Post, error: str) -> None:
        try:
            updated = repo.record_failure(
                self.conn,
                post,
                self.worker_id,
                error,
                self.clock(),
                max_attempts=MAX_ATTEMPTS,
                backoff=BACKOFF_BASE,
                retry_at=self._retry_at(post),
                no_slot_note=f"no free slot for a retry in the next {MAX_DAYS_AHEAD} days",
            )
        except PostConflict as exc:
            logger.warning("%s: failure not recorded: %s", _label(post), exc)
            return
        if updated.status == PostStatus.POST_FAILED:
            self._alert(updated, updated.error or error)
        else:
            logger.warning(
                "%s: attempt %d failed: %s; retry at %s",
                _label(post),
                updated.attempts,
                error,
                updated.scheduled_at.isoformat(),
            )

    def _deferred(self, post: Post, deferral: PublishDeferred) -> None:
        """Reschedule without counting an attempt (CLP-93). Never earlier than
        `MIN_DEFER` from now, so a bad `until` cannot make a pass spin on one post."""
        now = self.clock()
        # A deferred post is claimed at `until` (CLP-115): the lead time never shortens it.
        until = max(deferral.until, now + MIN_DEFER)
        reason = redact(deferral.reason)
        try:
            repo.defer(self.conn, post, self.worker_id, reason, until, now)
        except PostConflict as exc:
            logger.warning("%s: deferral not recorded: %s", _label(post), exc)
        else:
            logger.warning("%s: deferred to %s: %s", _label(post), until.isoformat(), reason)
        # The publisher sends each alert once, so it goes out even if the row changed.
        if deferral.alert:
            self._send(redact(deferral.alert), post)

    def _give_up(self, post: Post, error: str) -> None:
        try:
            failed = repo.mark_failed(self.conn, post, self.worker_id, error, self.clock())
        except PostConflict as exc:
            logger.warning("%s: failure not recorded: %s", _label(post), exc)
            return
        self._alert(failed, error)

    def _alert(self, post: Post, error: str) -> None:
        if error.startswith(UNRESOLVED):
            # The upload may still be in flight or live: a blind requeue could post twice.
            advice = (
                "The upload may have gone through: check the platform before you requeue"
                f" with `clipper posts retry {post.id}`."
            )
        else:
            advice = f"Requeue with `clipper posts retry {post.id}`."
        message = (
            f"{_label(post)} of clip {post.clip_id} is post_failed after"
            f" {post.attempts} attempt(s): {error}. {advice}"
        )
        self._send(message, post)

    def _send(self, message: str, post: Post) -> None:
        try:
            self.alert(message)
        except Exception:
            logger.exception("%s: alert failed", _label(post))


@dataclass(frozen=True)
class StoredRefs:
    """`PlatformRefs` on `posts.platform_ref` (CLP-122). A save is refused (`PostConflict`)
    once the post is no longer this worker's upload, so a worker that lost its claim never
    overwrites the new owner's id nor carries on uploading."""

    conn: sqlite3.Connection
    worker_id: str

    def save(self, idempotency_key: str, ref: str) -> None:
        if not repo.save_platform_ref(self.conn, idempotency_key, self.worker_id, ref):
            raise PostConflict("the post is no longer this worker's upload")

    def load(self, idempotency_key: str) -> StoredRef | None:
        return repo.platform_ref(self.conn, idempotency_key)


def _lead_time(publisher: Publisher) -> timedelta | None:
    lead = getattr(publisher, "lead_time", None)
    return lead if isinstance(lead, timedelta) and lead > timedelta(0) else None


def allow_all(_post: Post) -> str | None:
    """A guard that never holds: only for stages that knowingly skip the pre-flight."""
    return None


def _describe(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}")


def _label(post: Post) -> str:
    return f"post {post.id} ({post.platform}/{post.account_id})"
