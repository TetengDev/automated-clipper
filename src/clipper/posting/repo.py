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
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from typing import Any

from clipper.clips import get_version
from clipper.db import transaction
from clipper.jobs.models import to_iso
from clipper.posting.models import (
    InvalidPostTransition,
    Post,
    PostConflict,
    PostMetadata,
    PostStatus,
    PublishResult,
    can_transition,
)

LIST_LIMIT = 500
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


def account_post_times(conn: sqlite3.Connection, account_id: str) -> list[datetime]:
    """When the account's posts are (or were) scheduled; cancelled posts free their slot."""
    rows = conn.execute(
        f"SELECT * FROM posts WHERE account_id = ? AND {_COUNTED}",  # noqa: S608 - constant
        (account_id,),
    )
    return [Post.from_row(r).scheduled_at for r in rows]


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
    **fields: Any,
) -> Post:
    """A reviewer/CLI change: move a post to `target` and set `fields`, with an audit row.
    Only posts in `allowed_from` change: never one a worker is uploading."""
    with transaction(conn):
        post = _require(conn, post_id)
        if post.status not in allowed_from or not can_transition(post.status, target):
            raise InvalidPostTransition(post_id, post.status, target)
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


def retry(conn: sqlite3.Connection, post_id: int, now: datetime, *, by: str | None = None) -> Post:
    """Requeue a `post_failed` post now, with a fresh attempt count."""
    return _change(
        conn,
        post_id,
        PostStatus.SCHEDULED,
        now,
        by=by,
        allowed_from=frozenset({PostStatus.POST_FAILED}),
        scheduled_at=now,
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
) -> list[Post]:
    """Take over posts on `platforms` left `uploading` longer than `older_than` (a crashed
    worker). Only platforms this worker publishes to: it must be able to `lookup` them."""
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
        taken = []
        for row in rows:
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
) -> Post:
    """Count a failed attempt: back off (`now + backoff * 2**attempts`) or, at
    `max_attempts`, give up with `post_failed`."""
    with transaction(conn):
        current = _require(conn, post.id)
        attempts = current.attempts + 1
        if attempts >= max_attempts:
            target, fields = PostStatus.POST_FAILED, {}
        else:
            target = PostStatus.SCHEDULED
            fields = {"scheduled_at": now + backoff * 2**attempts}
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
