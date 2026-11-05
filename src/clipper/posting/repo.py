"""SQL for the posts table (CLP-92).

Every status change runs inside BEGIN IMMEDIATE, is conditional on the current status
(and, for a worker's own claim, on its worker id), and writes an audit row to
`clip_reviews` (action `post_scheduled`, `post_uploading`, `post_posted`, `post_failed`,
`post_cancelled` or `post_rescheduled`), next to the clip's review
history. Functions that change state open their own transaction, except `insert_post`,
which callers wrap (see `scheduler.schedule_for_clip`).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from clipper.clips import get_version
from clipper.db import transaction
from clipper.jobs.models import from_iso, to_iso
from clipper.posting.models import (
    InvalidPostTransition,
    NoFreeSlot,
    Post,
    PostConflict,
    PostMetadata,
    PostStatus,
    PublishResult,
    can_transition,
)

LIST_LIMIT = 500
UNRESOLVED_PREFIX = "uploading state unresolved"
"""How the posting stage's unresolved-upload errors start (`stage.UNRESOLVED`)."""
_COUNTED = "status != 'cancelled'"
_REVIEWER_EDITABLE = frozenset({PostStatus.SCHEDULED, PostStatus.POST_FAILED})


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
    """When the account's posts are (or were) scheduled; cancelled posts free their slot.
    `exclude_post_id` leaves out the post being moved and `since` skips older history,
    which the slot picker never looks at (CLP-106; the `(account_id, scheduled_at)` index
    keeps it cheap)."""
    rows = conn.execute(
        f"SELECT * FROM posts WHERE account_id = ? AND {_COUNTED} AND id IS NOT ?"  # noqa: S608 - constant
        " AND scheduled_at >= ?",
        (account_id, exclude_post_id, to_iso(since) if since is not None else ""),
    )
    return [Post.from_row(r).scheduled_at for r in rows]


def uploads_between(
    conn: sqlite3.Connection,
    account_id: str,
    start: datetime,
    end: datetime,
    *,
    exclude_post_id: int | None = None,
    per_attempt: bool = False,
) -> int:
    """Uploads the account made or started in [start, end), for the claim-time caps
    (CLP-106). Counted: posts posted then (`posted_at`), posts claimed for upload then
    (`claimed_at`), and posts whose failed attempt (or unresolved upload) was recorded then
    (`updated_at`, with attempts or an "uploading state unresolved" error): a failed attempt
    may have reached the platform, used quota or even gone live.

    `per_attempt` counts API attempts instead of posts (each earlier failed attempt of a
    post adds one), for the platform's API cap. A post's attempts are only seen in the
    window of its last change, and a reschedule resets them; the count errs high."""
    weight = "1 + attempts" if per_attempt else "1"
    failed_weight = (
        f"attempts + (CASE WHEN error LIKE '{UNRESOLVED_PREFIX}%' THEN 1 ELSE 0 END)"
        if per_attempt
        else "1"
    )
    row = conn.execute(
        f"SELECT COALESCE(SUM(CASE"  # noqa: S608 - fixed fragments, values bound
        f" WHEN status = 'posted' AND posted_at >= ? AND posted_at < ? THEN {weight}"
        f" WHEN status = 'uploading' AND claimed_at >= ? AND claimed_at < ? THEN {weight}"
        " WHEN status IN ('scheduled', 'post_failed') AND updated_at >= ? AND updated_at < ?"
        f" AND (attempts > 0 OR error LIKE '{UNRESOLVED_PREFIX}%') THEN {failed_weight}"
        " ELSE 0 END), 0) FROM posts WHERE account_id = ? AND id IS NOT ?",
        (*[to_iso(start), to_iso(end)] * 3, account_id, exclude_post_id),
    ).fetchone()
    return int(row[0])


def last_upload(
    conn: sqlite3.Connection,
    account_id: str,
    since: datetime,
    *,
    exclude_post_id: int | None = None,
) -> datetime | None:
    """When the account's latest upload since `since` started (posted or in flight)."""
    row = conn.execute(
        "SELECT MAX(CASE WHEN status = 'posted' THEN posted_at ELSE claimed_at END)"
        " FROM posts WHERE account_id = ? AND id IS NOT ? AND ("
        " (status = 'posted' AND posted_at >= ?) OR (status = 'uploading' AND claimed_at >= ?))",
        (account_id, exclude_post_id, to_iso(since), to_iso(since)),
    ).fetchone()
    return from_iso(row[0]) if row and row[0] else None


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
) -> Post:
    """Create a `scheduled` post (and its audit row) with a fresh idempotency key."""
    stamp = to_iso(now)
    cur = conn.execute(
        "INSERT INTO posts (clip_id, clip_version, account_id, platform, status, scheduled_at,"
        " idempotency_key, video_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            clip_version,
            account_id,
            platform,
            str(PostStatus.SCHEDULED),
            to_iso(scheduled_at),
            uuid.uuid4().hex,
            video_path,
            stamp,
            stamp,
        ),
    )
    post = _require(conn, int(cur.lastrowid or 0))
    _audit(conn, post, _action(PostStatus.SCHEDULED), now, scheduled_at=to_iso(scheduled_at))
    return post


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
    return _change(conn, post_id, PostStatus.CANCELLED, now, by=by)


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
            return {"scheduled_at": at or now}
        chosen = pick(c, post)
        if chosen is None:
            raise NoFreeSlot(post.account_id)
        return {"scheduled_at": chosen}

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
) -> list[Post]:
    """Atomically move due `scheduled` posts on `platforms` to `uploading` for this worker.

    BEGIN IMMEDIATE holds the write lock from the read to the update, so two workers can
    never claim the same post.
    """
    wanted = sorted(platforms)
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    with transaction(conn):
        rows = conn.execute(
            f"SELECT * FROM posts WHERE status = ? AND scheduled_at <= ? AND platform IN ({marks})"  # noqa: S608 - placeholders only
            " ORDER BY scheduled_at, id LIMIT ?",
            (str(PostStatus.SCHEDULED), to_iso(now), *wanted, limit),
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
    conn: sqlite3.Connection, post: Post, worker_id: str, result: PublishResult, now: datetime
) -> Post:
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
            when: datetime | None = now + backoff * 2**attempts
            if retry_at is not None:
                when = retry_at(conn, when)
            if when is None:
                error = f"{error}; {no_slot_note}" if no_slot_note else error
            else:
                target, fields = PostStatus.SCHEDULED, {"scheduled_at": when}
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
    CLP-93's `PublishDeferred`); with no `until` it is `post_failed`. None: go ahead."""
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


def metadata_for(conn: sqlite3.Connection, post: Post) -> PostMetadata:
    """Title, caption and hashtags of the clip version the post was scheduled with."""
    version = get_version(conn, post.clip_id, post.clip_version)
    if version is None:
        raise LookupError(f"clip {post.clip_id} v{post.clip_version}")
    return PostMetadata(
        title=version.title or "",
        caption=version.caption or "",
        hashtags=list(version.hashtags),
    )
