"""SQL for the posts table (CLP-92).

Every status change runs inside BEGIN IMMEDIATE, is conditional on the current status
(and, for a worker's own claim, on its worker id), and writes an audit row to
`clip_reviews` (action `post_scheduled`, `post_uploading`, `post_posted`, `post_failed`,
`post_cancelled`, `post_rescheduled`, `post_held`, `post_released`, `post_deferred`,
`post_slot_kept` or `post_repicked`), next to the clip's review history. Functions that
change state open their own transaction, except `insert_post`, which callers wrap (see
`scheduler.schedule_for_clip`).

CLP-115/CLP-120: `publish_at` is the post's slot, when it goes live; `scheduled_at` is
when it is next tried. Scheduling, a reschedule, a release, a retry and a claim-time cap
deferral (whose time the caps picked) set both; a publisher's deferral moves only
`scheduled_at`, so the slot survives a quota day or a paused account.
`settle_slot` decides, once a deferral is over, whether the slot still stands.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from clipper.clips import get_version
from clipper.db import transaction
from clipper.edl import EDL, has_ai_elements
from clipper.jobs.models import from_iso, to_iso, utcnow
from clipper.metadata import (
    PLATFORM_LIMITS,
    ai_flag,
    build_post_metadata,
    clean_caption,
    clean_hashtags,
    clean_title,
)
from clipper.posting.models import (
    InvalidPostTransition,
    NoFreeSlot,
    Post,
    PostConflict,
    PostMetadata,
    PostStatus,
    PublishResult,
    StoredRef,
    can_transition,
)

LIST_LIMIT = 500
UNRESOLVED_PREFIX = "uploading state unresolved"
"""How the posting stage's unresolved-upload errors start (`stage.UNRESOLVED`)."""
_COUNTED = "status != 'cancelled'"
"""Posts that hold their file on the platform (`posts_one_file_per_platform`)."""
_TAKES_SLOT = "status NOT IN ('cancelled', 'held')"
"""Posts that use one of the account's slots: a held post has none until it is released."""
_GO_LIVE = (
    "CASE WHEN status = 'posted' THEN MAX(posted_at, COALESCE(publish_at, posted_at))"
    " ELSE MAX(claimed_at, COALESCE(publish_at, claimed_at)) END"
)
"""When a posted or uploading post goes (or went) live: its upload, or its slot when the
platform scheduled it for later (YouTube `publishAt`, Facebook; CLP-115 review)."""
_UPLOAD_LEAD_MAX = timedelta(days=1)
"""Uploads never start this long before they go live (lead times are minutes), so the
go-live counts only look at posts changed since a day before the window."""
_REVIEWER_EDITABLE = frozenset({PostStatus.SCHEDULED, PostStatus.POST_FAILED})
_CANCELLABLE = _REVIEWER_EDITABLE | {PostStatus.HELD}


def _action(status: PostStatus) -> str:
    """Audit action for a status change: `post_scheduled`, ..., `post_failed`."""
    return str(status) if status.startswith("post_") else f"post_{status}"


def get_post(conn: sqlite3.Connection, post_id: int) -> Post | None:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return Post.from_row(row) if row else None


def _require(conn: sqlite3.Connection, post_id: int) -> Post:
    post = get_post(conn, post_id)
    if post is None:
        raise LookupError(post_id)
    return post


def list_posts(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[PostStatus] | None = None,
    clip_id: int | None = None,
    newest_first: bool = False,
    limit: int = LIST_LIMIT,
) -> list[Post]:
    clauses: list[str] = []
    params: list[Any] = []
    if statuses is not None:
        wanted = [str(s) for s in statuses]
        if not wanted:
            return []
        clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
        params.extend(wanted)
    if clip_id is not None:
        clauses.append("clip_id = ?")
        params.append(clip_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "DESC" if newest_first else "ASC"
    sql = f"SELECT * FROM posts{where} ORDER BY scheduled_at {order}, id {order} LIMIT ?"  # noqa: S608 - fixed fragments, values bound
    return [Post.from_row(r) for r in conn.execute(sql, [*params, limit])]


def account_post_times(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    exclude_post_id: int | None = None,
    since: datetime | None = None,
) -> list[datetime]:
    """The slots of the account's posts (`Post.slot`: when each goes, or went, live, not
    when a deferred one is next tried; CLP-120); cancelled and held posts take no slot.
    `exclude_post_id` leaves out the post being moved and `since` skips older history,
    which the slot picker never looks at (CLP-106). Every post that takes a slot has a
    `publish_at` (only held posts have none), so the range uses `posts_account_publish`."""
    rows = conn.execute(
        f"SELECT * FROM posts WHERE account_id = ? AND {_TAKES_SLOT} AND id IS NOT ?"  # noqa: S608 - constant
        " AND publish_at >= ?",
        (account_id, exclude_post_id, to_iso(since) if since is not None else ""),
    )
    return [Post.from_row(r).slot for r in rows]


def uploads_between(
    conn: sqlite3.Connection,
    account_id: str,
    start: datetime,
    end: datetime,
    *,
    exclude_post_id: int | None = None,
    per_attempt: bool = False,
) -> int:
    """Uploads of the account in [start, end), for the claim-time caps (CLP-106): posts
    posted or being uploaded, and failed attempts (or unresolved uploads) recorded then,
    from their audit rows: a failed attempt may have reached the platform, used quota or
    even gone live. Audit rows keep their time, so a later deferral, reschedule or retry
    neither moves an attempt into another window nor erases it.

    By default each other post counts once, by when it goes live (`_GO_LIVE`: an upload
    scheduled natively for its slot counts on the slot's day; CLP-115 review), for the
    daily limit. `per_attempt` counts API attempts instead, by when they were made (for the
    platform's API cap), including the excluded post's own failed attempts."""
    window = (to_iso(start), to_iso(end))
    if per_attempt:
        live = conn.execute(
            "SELECT id FROM posts WHERE account_id = ? AND id IS NOT ? AND updated_at >= ? AND ("
            " (status = 'posted' AND posted_at >= ? AND posted_at < ?)"
            " OR (status = 'uploading' AND claimed_at >= ? AND claimed_at < ?))",
            (account_id, exclude_post_id, window[0], *window, *window),
        ).fetchall()
    else:
        live = conn.execute(
            "SELECT id FROM posts WHERE account_id = ? AND id IS NOT ? AND updated_at >= ?"  # noqa: S608 - constant
            f" AND status IN ('posted', 'uploading') AND {_GO_LIVE} >= ? AND {_GO_LIVE} < ?",
            (account_id, exclude_post_id, to_iso(start - _UPLOAD_LEAD_MAX), *window),
        ).fetchall()
    failed = [
        row[0]
        for row in conn.execute(
            "SELECT json_extract(payload, '$.post_id') FROM clip_reviews"
            " WHERE action IN (?, ?) AND created_at >= ? AND created_at < ?"
            " AND json_extract(payload, '$.account_id') = ?"
            " AND (json_extract(payload, '$.attempts') IS NOT NULL"
            " OR json_extract(payload, '$.error') LIKE ?)",
            (
                _action(PostStatus.SCHEDULED),
                _action(PostStatus.POST_FAILED),
                *window,
                account_id,
                f"{UNRESOLVED_PREFIX}%",
            ),
        )
    ]
    if per_attempt:
        return len(live) + len(failed)
    return len(({row[0] for row in live} | set(failed)) - {exclude_post_id})


def had_failed_attempt(conn: sqlite3.Connection, post: Post) -> bool:
    """Whether an upload attempt of this post ever failed (or was left unresolved), even
    if a reviewer's retry or reschedule has since reset its attempt count."""
    row = conn.execute(
        "SELECT 1 FROM clip_reviews WHERE clip_id = ? AND action IN (?, ?)"
        " AND json_extract(payload, '$.post_id') = ?"
        " AND (json_extract(payload, '$.attempts') IS NOT NULL"
        " OR json_extract(payload, '$.error') LIKE ?) LIMIT 1",
        (
            post.clip_id,
            _action(PostStatus.SCHEDULED),
            _action(PostStatus.POST_FAILED),
            post.id,
            f"{UNRESOLVED_PREFIX}%",
        ),
    ).fetchone()
    return row is not None


def go_lives_near(
    conn: sqlite3.Connection,
    account_id: str,
    at: datetime,
    gap: timedelta,
    *,
    exclude_post_id: int | None = None,
) -> list[datetime]:
    """When the account's posted or in-flight posts go (or went) live within `gap` of
    `at`, on either side, latest first: an upload scheduled natively for later is seen at
    its slot, not at its upload (CLP-115 review)."""
    rows = conn.execute(
        f"SELECT {_GO_LIVE} AS live FROM posts WHERE account_id = ? AND id IS NOT ?"  # noqa: S608 - constant
        " AND status IN ('posted', 'uploading') AND updated_at >= ?"
        " AND live > ? AND live < ? ORDER BY live DESC",
        (
            account_id,
            exclude_post_id,
            to_iso(at - gap - _UPLOAD_LEAD_MAX),
            to_iso(at - gap),
            to_iso(at + gap),
        ),
    ).fetchall()
    return [moment for row in rows if (moment := from_iso(row["live"])) is not None]


def has_post(conn: sqlite3.Connection, clip_id: int, account_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM posts WHERE clip_id = ? AND account_id = ?", (clip_id, account_id)
    ).fetchone()
    return row is not None


def file_on_platform(conn: sqlite3.Connection, platform: str, video_path: str) -> bool:
    """Whether this rendered file is already scheduled or posted on the platform."""
    row = conn.execute(
        f"SELECT 1 FROM posts WHERE platform = ? AND video_path = ? AND {_COUNTED}",  # noqa: S608
        (platform, video_path),
    ).fetchone()
    return row is not None


def _audit(
    conn: sqlite3.Connection,
    post: Post,
    action: str,
    now: datetime,
    **detail: Any,
) -> None:
    payload = {"post_id": post.id, "account_id": post.account_id, "platform": post.platform}
    payload.update({k: v for k, v in detail.items() if v is not None})
    conn.execute(
        "INSERT INTO clip_reviews (clip_id, version, action, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (post.clip_id, post.clip_version, action, json.dumps(payload, sort_keys=True), to_iso(now)),
    )


def _metadata_json(metadata: PostMetadata | None) -> str | None:
    return None if metadata is None else json.dumps(asdict(metadata), ensure_ascii=False)


def insert_post(
    conn: sqlite3.Connection,
    *,
    clip_id: int,
    clip_version: int,
    account_id: str,
    platform: str,
    scheduled_at: datetime,
    video_path: str,
    now: datetime,
    campaign_id: str | None = None,
    metadata: PostMetadata | None = None,
    held: str | None = None,
) -> Post:
    """Create a `scheduled` post (and its audit row) with a fresh idempotency key; or a
    `held` one when `held` gives the reason (CLP-97). `metadata` is what it is published
    with. `scheduled_at` is the post's slot (`publish_at`); a held post has none."""
    stamp = to_iso(now)
    status = PostStatus.HELD if held else PostStatus.SCHEDULED
    cur = conn.execute(
        "INSERT INTO posts (clip_id, clip_version, account_id, platform, status, scheduled_at,"
        " publish_at, idempotency_key, video_path, created_at, updated_at, campaign_id,"
        " metadata, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            clip_version,
            account_id,
            platform,
            str(status),
            to_iso(scheduled_at),
            None if held else to_iso(scheduled_at),
            uuid.uuid4().hex,
            video_path,
            stamp,
            stamp,
            campaign_id,
            _metadata_json(metadata),
            held,
        ),
    )
    post = _require(conn, int(cur.lastrowid or 0))
    _audit(
        conn,
        post,
        _action(status),
        now,
        scheduled_at=None if held else to_iso(scheduled_at),
        campaign_id=campaign_id,
        reason=held,
    )
    return post


def held_posts(conn: sqlite3.Connection) -> list[Post]:
    """Every held post, oldest first (no list limit: recheck must see them all)."""
    rows = conn.execute(
        "SELECT * FROM posts WHERE status = ? ORDER BY id", (str(PostStatus.HELD),)
    ).fetchall()
    return [Post.from_row(r) for r in rows]


def release_held(
    conn: sqlite3.Connection,
    post: Post,
    at: datetime,
    now: datetime,
    *,
    metadata: PostMetadata,
    clip_version: int,
    video_path: str,
    campaign_id: str | None,
) -> Post:
    """A held post passed the pre-flight again: schedule it at `at` with what was checked
    (the clip's current version and file, the campaign that now applies). The caller
    holds the transaction, so the slot it picked cannot be taken meanwhile. For the same
    file, an AI flag stored on the post stays on, whatever the new metadata says; a new
    file (a re-edited, re-rendered clip) takes the new metadata's flag (CLP-121)."""
    if video_path == post.video_path:
        row = conn.execute("SELECT metadata FROM posts WHERE id = ?", (post.id,)).fetchone()
        stored = _stored_metadata(row["metadata"] if row else None)
        flag = ai_flag(post.platform)
        if stored is not None and stored.flags.get(flag):
            metadata = replace(metadata, flags={**metadata.flags, flag: True})
    _update(
        conn,
        post,
        PostStatus.SCHEDULED,
        now,
        scheduled_at=at,
        publish_at=at,
        error=None,
        attempts=0,
        metadata=_metadata_json(metadata),
        clip_version=clip_version,
        video_path=video_path,
        campaign_id=campaign_id,
    )
    released = _require(conn, post.id)
    _audit(
        conn,
        released,
        "post_released",
        now,
        previous=str(post.status),
        scheduled_at=to_iso(at),
        campaign_id=campaign_id,
    )
    return released


def update_hold(conn: sqlite3.Connection, post: Post, reason: str, now: datetime) -> Post:
    """A held post still cannot go out: record the current reason (audited only when it
    changed). The caller holds the transaction."""
    if post.error == reason:
        return post
    _update(conn, post, PostStatus.HELD, now, error=reason)
    updated = _require(conn, post.id)
    _audit(conn, updated, _action(PostStatus.HELD), now, reason=reason)
    return updated


def hold_claimed(
    conn: sqlite3.Connection, post: Post, worker_id: str, reason: str, now: datetime
) -> Post:
    """The publish-time pre-flight refused a post this worker claimed: hold it instead of
    publishing (CLP-97)."""
    with transaction(conn):
        _update(
            conn,
            post,
            PostStatus.HELD,
            now,
            owner=worker_id,
            error=reason,
            publish_at=None,  # a held post has no slot
            worker_id=None,
            claimed_at=None,
        )
        held = _require(conn, post.id)
        _audit(conn, held, _action(PostStatus.HELD), now, reason=reason, worker_id=worker_id)
    return held


def _change(
    conn: sqlite3.Connection,
    post_id: int,
    target: PostStatus,
    now: datetime,
    *,
    action: str | None = None,
    by: str | None = None,
    allowed_from: Collection[PostStatus] = _REVIEWER_EDITABLE,
    computed: Callable[[sqlite3.Connection, Post], dict[str, Any]] | None = None,
    **fields: Any,
) -> Post:
    """A reviewer/CLI change: move a post to `target` and set `fields`, with an audit row.
    Only posts in `allowed_from` change: never one a worker is uploading. `computed`
    adds fields worked out inside the transaction (CLP-106: the next free slot)."""
    with transaction(conn):
        post = _require(conn, post_id)
        if post.status not in allowed_from or not can_transition(post.status, target):
            raise InvalidPostTransition(post_id, post.status, target)
        if computed is not None:
            fields = {**fields, **computed(conn, post)}
        _update(conn, post, target, now, **fields)
        updated = _require(conn, post_id)
        _audit(
            conn,
            updated,
            action or _action(target),
            now,
            previous=str(post.status),
            scheduled_at=to_iso(updated.scheduled_at),
            by=by,
        )
    return updated


def _update(
    conn: sqlite3.Connection,
    post: Post,
    target: PostStatus,
    now: datetime,
    *,
    owner: str | None = None,
    **fields: Any,
) -> None:
    """UPDATE guarded by the status we read (and the claiming worker `owner`, if given)."""
    assignments = {"status": str(target), "updated_at": to_iso(now), **fields}
    for key, value in assignments.items():
        if isinstance(value, datetime):
            assignments[key] = to_iso(value)
    columns = ", ".join(f"{name} = ?" for name in assignments)
    guard = "id = ? AND status = ?"
    params: list[Any] = [*assignments.values(), post.id, str(post.status)]
    if owner is not None:
        guard += " AND worker_id = ?"
        params.append(owner)
    cur = conn.execute(f"UPDATE posts SET {columns} WHERE {guard}", params)  # noqa: S608 - column names are ours
    if cur.rowcount != 1:
        raise PostConflict(f"post {post.id} changed while {target} was being recorded")


# --- reviewer / CLI actions -----------------------------------------------------------


def cancel(conn: sqlite3.Connection, post_id: int, now: datetime, *, by: str | None = None) -> Post:
    return _change(conn, post_id, PostStatus.CANCELLED, now, by=by, allowed_from=_CANCELLABLE)


def reschedule(
    conn: sqlite3.Connection, post_id: int, at: datetime, now: datetime, *, by: str | None = None
) -> Post:
    """Move a scheduled post, or requeue a failed one, to `at` (attempts start over)."""
    return _change(
        conn,
        post_id,
        PostStatus.SCHEDULED,
        now,
        action="post_rescheduled",
        by=by,
        scheduled_at=at,
        publish_at=at,
        attempts=0,
        error=None,
    )


def retry(
    conn: sqlite3.Connection,
    post_id: int,
    now: datetime,
    *,
    at: datetime | None = None,
    pick: Callable[[sqlite3.Connection, Post], datetime | None] | None = None,
    by: str | None = None,
) -> Post:
    """Requeue a `post_failed` post with a fresh attempt count: at `at`, else at the slot
    `pick` chooses inside the write transaction (None raises NoFreeSlot), else now."""

    def slot(c: sqlite3.Connection, post: Post) -> dict[str, Any]:
        if at is not None or pick is None:
            return {"scheduled_at": at or now, "publish_at": at or now}
        chosen = pick(c, post)
        if chosen is None:
            raise NoFreeSlot(post.account_id)
        return {"scheduled_at": chosen, "publish_at": chosen}

    return _change(
        conn,
        post_id,
        PostStatus.SCHEDULED,
        now,
        by=by,
        allowed_from=frozenset({PostStatus.POST_FAILED}),
        computed=slot,
        attempts=0,
        error=None,
    )


# --- posting stage -------------------------------------------------------------------


def due_platforms(conn: sqlite3.Connection, now: datetime) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT platform FROM posts WHERE status = ? AND scheduled_at <= ?",
        (str(PostStatus.SCHEDULED), to_iso(now)),
    )
    return {r[0] for r in rows}


def _claim(conn: sqlite3.Connection, post: Post, worker_id: str, now: datetime) -> Post:
    _update(
        conn,
        post,
        PostStatus.UPLOADING,
        now,
        worker_id=worker_id,
        claimed_at=now,
    )
    claimed = _require(conn, post.id)
    _audit(conn, claimed, _action(PostStatus.UPLOADING), now, worker_id=worker_id)
    return claimed


def claim_due(
    conn: sqlite3.Connection,
    now: datetime,
    worker_id: str,
    *,
    platforms: Collection[str],
    limit: int = 10,
    early: Mapping[str, timedelta] | None = None,
    exclude: Collection[int] = (),
) -> list[Post]:
    """Atomically move due `scheduled` posts on `platforms` to `uploading` for this worker.
    Posts in `exclude` (already handled in this pass) are skipped.

    `early` maps a platform to a lead time (CLP-94, CLP-115: Facebook and YouTube publish
    natively at the slot). A post is due once its `scheduled_at` has come and its slot is
    no further ahead than the lead time (none: the slot has come too), so a platform
    without a lead time never publishes before the slot. A first attempt at its slot (no
    attempt, no deferral: `error` is unset, since every deferral records its reason) is
    due the lead time before it. A retry or a deferral is never claimed before its own
    time, whatever the lead time.

    BEGIN IMMEDIATE holds the write lock from the read to the update, so two workers can
    never claim the same post.
    """
    wanted = sorted(platforms)
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    due: list[str] = []
    params: list[Any] = []
    for platform in wanted:
        lead = max((early or {}).get(platform, timedelta(0)), timedelta(0))
        ahead = to_iso(now + lead)
        if lead:
            due.append(
                "(platform = ? AND COALESCE(publish_at, scheduled_at) <= ? AND (scheduled_at <= ?"
                " OR (attempts = 0 AND error IS NULL AND scheduled_at <= ?)))"
            )
            params += [platform, ahead, to_iso(now), ahead]
        else:
            due.append(
                "(platform = ? AND scheduled_at <= ? AND COALESCE(publish_at, scheduled_at) <= ?)"
            )
            params += [platform, ahead, ahead]
    skipped = sorted(exclude)
    skip = f" AND id NOT IN ({', '.join('?' for _ in skipped)})" if skipped else ""
    with transaction(conn):
        rows = conn.execute(
            f"SELECT * FROM posts WHERE status = ? AND platform IN ({marks})"  # noqa: S608 - placeholders only
            f" AND ({' OR '.join(due)}){skip} ORDER BY scheduled_at, id LIMIT ?",
            (str(PostStatus.SCHEDULED), *wanted, *params, *skipped, limit),
        ).fetchall()
        return [_claim(conn, Post.from_row(r), worker_id, now) for r in rows]


def claim_stale(
    conn: sqlite3.Connection,
    now: datetime,
    worker_id: str,
    *,
    older_than: timedelta,
    platforms: Collection[str],
    limit: int | None = None,
) -> list[Post]:
    """Take over posts on `platforms` left `uploading` longer than `older_than` (a crashed
    worker). Only platforms this worker publishes to: it must be able to `lookup` them.
    `limit` takes over at most that many, so the rest do not sit claimed meanwhile."""
    wanted = sorted(platforms)
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    with transaction(conn):
        where = f"status = ? AND (claimed_at IS NULL OR claimed_at <= ?) AND platform IN ({marks})"
        sql = f"SELECT * FROM posts WHERE {where} ORDER BY claimed_at, id"  # noqa: S608
        rows = conn.execute(
            sql, (str(PostStatus.UPLOADING), to_iso(now - older_than), *wanted)
        ).fetchall()
        taken: list[Post] = []
        for row in rows:
            if limit is not None and len(taken) >= limit:
                break
            post = Post.from_row(row)
            cur = conn.execute(
                "UPDATE posts SET worker_id = ?, claimed_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ? AND claimed_at IS ?",
                (
                    worker_id,
                    to_iso(now),
                    to_iso(now),
                    post.id,
                    str(PostStatus.UPLOADING),
                    row["claimed_at"],
                ),
            )
            if cur.rowcount == 1:
                recovered = _require(conn, post.id)
                _audit(
                    conn,
                    recovered,
                    "post_recovered",
                    now,
                    worker_id=worker_id,
                    previous_worker=post.worker_id,
                )
                taken.append(recovered)
        return taken


def touch_claim(conn: sqlite3.Connection, post_id: int, worker_id: str, now: datetime) -> bool:
    """Heartbeat (CLP-105): re-stamp `claimed_at` of a post this worker is uploading, so the
    stale pass never takes over a live upload. False once the post is no longer this
    worker's upload (finished, or taken over). No audit row: it runs every minute."""
    cur = conn.execute(
        "UPDATE posts SET claimed_at = ?, updated_at = ?"
        " WHERE id = ? AND status = ? AND worker_id = ?",
        (to_iso(now), to_iso(now), post_id, str(PostStatus.UPLOADING), worker_id),
    )
    return cur.rowcount == 1


def save_platform_ref(
    conn: sqlite3.Connection, idempotency_key: str, worker_id: str, ref: str
) -> bool:
    """Store the platform's id for the upload in progress (CLP-122) on the post with this
    key. Only while it is this worker's upload, like `touch_claim`: False when it is not
    (finished, or taken over), and nothing is written. No audit row; `updated_at` is
    left alone, since the claim-time caps count recent uploads by it."""
    cur = conn.execute(
        "UPDATE posts SET platform_ref = ?"
        " WHERE idempotency_key = ? AND status = ? AND worker_id = ?",
        (ref, idempotency_key, str(PostStatus.UPLOADING), worker_id),
    )
    return cur.rowcount == 1


def platform_ref(conn: sqlite3.Connection, idempotency_key: str) -> StoredRef | None:
    """The stored platform id of the post with this key (`save_platform_ref`) with the
    post's own account, or None when there is no such post or nothing is stored."""
    row = conn.execute(
        "SELECT account_id, platform_ref FROM posts WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None or row["platform_ref"] is None:
        return None
    return StoredRef(row["account_id"], row["platform_ref"])


def release_uploads(
    conn: sqlite3.Connection,
    dead_worker_id: str,
    now: datetime,
    *,
    quiet_for: timedelta,
    stale_after: timedelta,
) -> tuple[int, int]:
    """Operator recovery of a known-dead worker (`run-worker --recover-id`). Returns
    (released, shortened).

    Claims not re-stamped for `quiet_for` are released (`claimed_at` cleared), so the next
    stale pass resolves them through `lookup` at once. A claim re-stamped more recently may
    belong to a live worker named by mistake, so it is only shortened: set to go stale
    `quiet_for` from `now` instead of `stale_after` from its last beat. A live worker's
    next heartbeat re-stamps it, so its upload is never handed over; a dead one's goes
    stale minutes after the restart. Each change is compare-and-swap on `claimed_at`.
    The protection relies on the heartbeat: a worker run without one looks quiet."""
    quiet_since = to_iso(now - quiet_for)
    shortened_to = to_iso(now - stale_after + quiet_for)
    released = shortened = 0
    with transaction(conn):
        rows = conn.execute(
            "SELECT * FROM posts WHERE status = ? AND worker_id = ? AND claimed_at IS NOT NULL",
            (str(PostStatus.UPLOADING), dead_worker_id),
        ).fetchall()
        for row in rows:
            quiet = row["claimed_at"] <= quiet_since
            cur = conn.execute(
                "UPDATE posts SET claimed_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ? AND worker_id = ? AND claimed_at IS ?",
                (
                    None if quiet else shortened_to,
                    to_iso(now),
                    row["id"],
                    str(PostStatus.UPLOADING),
                    dead_worker_id,
                    row["claimed_at"],
                ),
            )
            if cur.rowcount != 1:
                continue
            post = Post.from_row(row)
            if quiet:
                released += 1
                _audit(conn, post, "post_released", now, worker_id=dead_worker_id)
            else:
                shortened += 1
                _audit(
                    conn,
                    post,
                    "post_release_deferred",
                    now,
                    worker_id=dead_worker_id,
                    claimed_at=shortened_to,
                )
    return released, shortened


def mark_posted(
    conn: sqlite3.Connection,
    post: Post,
    worker_id: str,
    result: PublishResult,
    now: datetime,
    *,
    live_at: datetime | None = None,
) -> Post:
    """Record the upload. `live_at`, when known, is when it goes (or went) live and becomes
    `publish_at`: the slot for a natively scheduled upload, else the upload itself."""
    fields = {} if live_at is None else {"publish_at": live_at}
    with transaction(conn):
        _update(
            conn,
            post,
            PostStatus.POSTED,
            now,
            owner=worker_id,
            posted_at=now,
            external_id=result.external_id,
            url=result.url,
            error=None,
            **fields,
        )
        done = _require(conn, post.id)
        _audit(conn, done, _action(PostStatus.POSTED), now, external_id=result.external_id)
    return done


def record_failure(
    conn: sqlite3.Connection,
    post: Post,
    worker_id: str,
    error: str,
    now: datetime,
    *,
    max_attempts: int,
    backoff: timedelta,
    retry_at: Callable[[sqlite3.Connection, datetime], datetime | None] | None = None,
    no_slot_note: str = "",
) -> Post:
    """Count a failed attempt: retry at `now + backoff * 2**attempts`, or at the slot
    `retry_at(conn, that time)` picks inside this transaction (CLP-106: the account's next
    free slot at or after it). At `max_attempts`, or when `retry_at` finds no slot (then
    `no_slot_note` is added to the error), give up with `post_failed`."""
    with transaction(conn):
        current = _require(conn, post.id)
        attempts = current.attempts + 1
        target, fields = PostStatus.POST_FAILED, {}
        if attempts < max_attempts:
            # Never before the slot: a first attempt claimed early (a publisher lead time)
            # must not be retried, and so published, ahead of the time it was given.
            when: datetime | None = max(now + backoff * 2**attempts, current.slot)
            if retry_at is not None:
                when = retry_at(conn, when)
            if when is None:
                error = f"{error}; {no_slot_note}" if no_slot_note else error
            else:
                # The retry goes live when it is uploaded: that is its slot now (CLP-115).
                target, fields = PostStatus.SCHEDULED, {"scheduled_at": when, "publish_at": when}
        _update(
            conn,
            current,
            target,
            now,
            owner=worker_id,
            attempts=attempts,
            error=error,
            worker_id=None,
            claimed_at=None,
            **fields,
        )
        updated = _require(conn, post.id)
        _audit(
            conn,
            updated,
            _action(target),
            now,
            attempts=attempts,
            error=error,
            scheduled_at=to_iso(updated.scheduled_at),
        )
    return updated


def defer(
    conn: sqlite3.Connection,
    post: Post,
    worker_id: str,
    reason: str,
    until: datetime,
    now: datetime,
) -> Post:
    """Put a claimed post back to `scheduled` at `until` without counting an attempt
    (CLP-93: quota, upload limit or a paused account). Audited as `post_deferred`. The
    slot is kept (CLP-115): `settle_slot` checks it once the deferral is over."""
    with transaction(conn):
        _update(
            conn,
            post,
            PostStatus.SCHEDULED,
            now,
            owner=worker_id,
            scheduled_at=until,
            publish_at=post.slot,
            error=reason,
            worker_id=None,
            claimed_at=None,
        )
        deferred = _require(conn, post.id)
        _audit(conn, deferred, "post_deferred", now, error=reason, scheduled_at=to_iso(until))
    return deferred


@dataclass(frozen=True)
class Deferral:
    """Why a claimed post may not upload now, and when it may (None: no free slot, so the
    post is given up for a human)."""

    reason: str
    until: datetime | None


def defer_claimed(
    conn: sqlite3.Connection,
    post: Post,
    worker_id: str,
    now: datetime,
    decide: Callable[[sqlite3.Connection], Deferral | None],
) -> Deferral | None:
    """Claim-time caps (CLP-106): run `decide` and act on it in one write transaction, so
    the count and the slot it picks are not stale. A deferral puts the claimed post back
    to `scheduled` at `until` without counting an attempt (audit `post_deferred`, as for
    CLP-93's `PublishDeferred`); with no `until` it is `post_failed`. None: go ahead.
    `until` is a time the caps picked for the post to go live, so it becomes the post's
    slot and is reserved for the slot picker (CLP-115 review)."""
    with transaction(conn):
        decision = decide(conn)
        if decision is None:
            return None
        if decision.until is None:
            _update(
                conn,
                post,
                PostStatus.POST_FAILED,
                now,
                owner=worker_id,
                error=decision.reason,
                worker_id=None,
                claimed_at=None,
            )
            failed = _require(conn, post.id)
            _audit(conn, failed, _action(PostStatus.POST_FAILED), now, error=decision.reason)
            return decision
        _update(
            conn,
            post,
            PostStatus.SCHEDULED,
            now,
            owner=worker_id,
            scheduled_at=decision.until,
            publish_at=decision.until,
            error=decision.reason,
            worker_id=None,
            claimed_at=None,
        )
        deferred = _require(conn, post.id)
        _audit(
            conn,
            deferred,
            "post_deferred",
            now,
            error=decision.reason,
            scheduled_at=to_iso(decision.until),
        )
    return decision


def settle_slot(
    conn: sqlite3.Connection,
    post: Post,
    worker_id: str,
    now: datetime,
    *,
    pick: Callable[[sqlite3.Connection], datetime | None] | None,
    go_by: datetime,
    grace: timedelta = timedelta(0),
    no_slot: str = "no free slot",
) -> Post:
    """A claimed post whose deferral is over (CLP-115, CLP-120): put `scheduled_at` back
    on the slot it goes live at, in one write transaction. Returns the post as it is now:
    `uploading` to upload it, `scheduled` for a later slot, or `post_failed`.

    * Not deferred (`scheduled_at` is the slot) or no slot: unchanged.
    * The slot is still ahead, or passed by at most `grace` (measured at the later of the
      deferral's end and now, so a worker back from an outage does not count as on
      time): the slot is kept (audit `post_slot_kept`); one that passed goes out at once.
    * Otherwise the slot is re-picked (audit `post_repicked`): `pick` chooses the
      account's next free slot inside this transaction. Without `pick` (no account rules)
      the post goes out now; when `pick` finds none it is `post_failed` with `no_slot`.
      A slot no later than `go_by` is uploaded at once (one already behind now records
      now, when it really goes live); a later one goes back to `scheduled` at that slot
      with its error cleared, so a lead-time platform claims it early, as a first attempt."""
    slot = post.publish_at
    if slot is None or slot == post.scheduled_at:
        return post
    with transaction(conn):
        if max(post.scheduled_at, now) - slot <= grace:
            _update(conn, post, PostStatus.UPLOADING, now, owner=worker_id, scheduled_at=slot)
            kept = _require(conn, post.id)
            previous = to_iso(post.scheduled_at)
            _audit(conn, kept, "post_slot_kept", now, previous=previous, scheduled_at=to_iso(slot))
            return kept
        chosen = now if pick is None else pick(conn)
        if chosen is None:
            _update(
                conn,
                post,
                PostStatus.POST_FAILED,
                now,
                owner=worker_id,
                error=no_slot,
                worker_id=None,
                claimed_at=None,
            )
            failed = _require(conn, post.id)
            _audit(conn, failed, _action(PostStatus.POST_FAILED), now, error=no_slot)
            return failed
        if chosen <= go_by:
            at = max(chosen, now)
            _update(
                conn,
                post,
                PostStatus.UPLOADING,
                now,
                owner=worker_id,
                scheduled_at=at,
                publish_at=at,
                error=None,
            )
        else:
            at = chosen
            _update(
                conn,
                post,
                PostStatus.SCHEDULED,
                now,
                owner=worker_id,
                scheduled_at=at,
                publish_at=at,
                error=None,
                worker_id=None,
                claimed_at=None,
            )
        settled = _require(conn, post.id)
        _audit(conn, settled, "post_repicked", now, previous=to_iso(slot), scheduled_at=to_iso(at))
    return settled


def mark_failed(
    conn: sqlite3.Connection, post: Post, worker_id: str, error: str, now: datetime
) -> Post:
    """Give up on a post without another attempt (e.g. an unresolvable crashed upload)."""
    with transaction(conn):
        _update(
            conn,
            post,
            PostStatus.POST_FAILED,
            now,
            owner=worker_id,
            error=error,
            worker_id=None,
            claimed_at=None,
        )
        failed = _require(conn, post.id)
        _audit(conn, failed, _action(PostStatus.POST_FAILED), now, error=error)
    return failed


def metadata_for(
    conn: sqlite3.Connection,
    post: Post,
    *,
    edl: EDL | None = None,
    now: datetime | None = None,
) -> PostMetadata:
    """What the post is published with: the metadata stored at scheduling (the clip's
    platform variant, campaign tags and flags, as the pre-flight checked them: the AI flag
    from the render sidecar's EDL, the paid-partnership flag from the campaign brief).
    Posts scheduled before CLP-97 fall back to the clip version's platform variant (CLP-96,
    without campaign tags or the paid flag), else its single title and caption.

    `edl` is the clip's EDL read now (the render sidecar, None when unknown). When it has
    an AI element the AI flag is set, whatever was stored (CLP-121): a clip whose sidecar
    gained an AI element after scheduling, or a post from before CLP-97, still goes out
    disclosed. A stored flag is never cleared. A flag turned on here is written to the
    post's stored metadata (for a post from before CLP-97, the metadata built here), so
    the row records what was sent, and audited as `post_ai_flag_set` at `now`. The write is
    guarded by the status and worker read with `post`: a post that changed meanwhile is
    left as it is (the metadata returned still has the flag). The write opens its own
    transaction, so call this with `edl` outside any transaction."""
    metadata = _metadata_for(conn, post)
    if edl is None or not has_ai_elements(edl):
        return metadata
    flag = ai_flag(post.platform)
    if metadata.flags.get(flag):
        return metadata
    upgraded = replace(metadata, flags={**metadata.flags, flag: True})
    with transaction(conn):
        cur = conn.execute(
            "UPDATE posts SET metadata = ? WHERE id = ? AND status = ? AND worker_id IS ?",
            (_metadata_json(upgraded), post.id, str(post.status), post.worker_id),
        )
        if cur.rowcount == 1:
            _audit(conn, post, "post_ai_flag_set", now or utcnow(), flag=flag)
    return upgraded


def _metadata_for(conn: sqlite3.Connection, post: Post) -> PostMetadata:
    row = conn.execute("SELECT metadata FROM posts WHERE id = ?", (post.id,)).fetchone()
    stored = _stored_metadata(row["metadata"] if row else None)
    if stored is not None:
        return stored
    version = get_version(conn, post.clip_id, post.clip_version)
    if version is None:
        raise LookupError(f"clip {post.clip_id} v{post.clip_version}")
    if post.platform not in version.variants:
        # That copy was stored unchecked (CLP-111).
        limit = PLATFORM_LIMITS[post.platform].post_hashtags_max
        return PostMetadata(
            title=clean_title(version.title or ""),
            caption=clean_caption(version.caption or ""),
            hashtags=clean_hashtags(version.hashtags, limit),
        )
    built = build_post_metadata(version.variants, post.platform)
    return PostMetadata(
        title=built.title, caption=built.caption, hashtags=built.hashtags, flags=built.flags
    )


def _stored_metadata(raw: str | None) -> PostMetadata | None:
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("stored post metadata is not an object")
    title, caption = data.get("title"), data.get("caption")
    hashtags, flags = data.get("hashtags"), data.get("flags")
    if not (
        isinstance(title, str)
        and isinstance(caption, str)
        and isinstance(hashtags, list)
        and all(isinstance(t, str) for t in hashtags)
        and isinstance(flags, dict)
        and all(isinstance(k, str) and isinstance(v, bool) for k, v in flags.items())
    ):
        raise ValueError("stored post metadata is malformed")
    return PostMetadata(title=title, caption=caption, hashtags=hashtags, flags=flags)
