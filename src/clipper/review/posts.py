"""Review pages for posts (CLP-92): the Scheduled page, cancel and reschedule, and the
Posts section of the clip page (loaded by HTMX from `/clip/{id}/posts`).

Same auth and CSRF as the other review routes: the app-wide gate and `csrf_protect`, plus
`require_user` on every route.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from clipper import clips
from clipper.posting import repo
from clipper.posting.models import InvalidPostTransition, Post, PostStatus
from clipper.review.app import flash, render, safe_next_path
from clipper.review.routes import MAX_ID, ClipId, Conn, User, _action_error, _is_htmx, _now

router = APIRouter()

PostId = Annotated[int, Path(ge=1, le=MAX_ID)]
NextPath = Annotated[str, Form(max_length=2048)]
RESCHEDULE_HORIZON = timedelta(days=366)
SCHEDULED_PAGE = "/scheduled"
RECENT_POSTED = 20


@router.get(SCHEDULED_PAGE, response_class=HTMLResponse)
def scheduled_page(request: Request, _user: User, conn: Conn) -> Response:
    upcoming = repo.list_posts(conn, statuses=(PostStatus.SCHEDULED, PostStatus.UPLOADING))
    failed = repo.list_posts(conn, statuses=(PostStatus.POST_FAILED,), newest_first=True)
    posted = repo.list_posts(
        conn, statuses=(PostStatus.POSTED,), newest_first=True, limit=RECENT_POSTED
    )
    return render(
        request,
        "scheduled.html",
        {"upcoming": upcoming, "failed": failed, "posted": posted, "next": SCHEDULED_PAGE},
    )


@router.get("/clip/{clip_id}/posts", response_class=HTMLResponse)
def clip_posts(request: Request, clip_id: ClipId, _user: User, conn: Conn) -> Response:
    if clips.get_clip(conn, clip_id) is None:
        raise HTTPException(status_code=404, detail="clip not found")
    posts = repo.list_posts(conn, clip_id=clip_id)
    return render(request, "_clip_posts_rows.html", {"posts": posts, "next": f"/clip/{clip_id}"})


def _done(request: Request, next_path: str, message: str) -> Response:
    flash(request, message)
    location = safe_next_path(next_path)
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)


def _apply(
    request: Request, post_id: int, verb: str, change: Callable[[], Post]
) -> Post | Response:
    try:
        return change()
    except LookupError:
        raise HTTPException(status_code=404, detail="post not found") from None
    except InvalidPostTransition as exc:
        return _action_error(request, f"Cannot {verb}: post {post_id} is {exc.current}.", 409)


@router.post("/posts/{post_id}/cancel")
def cancel_post(
    request: Request,
    post_id: PostId,
    _user: User,
    conn: Conn,
    next: NextPath = SCHEDULED_PAGE,
) -> Response:
    result = _apply(
        request, post_id, "cancel", lambda: repo.cancel(conn, post_id, _now(request), by="review")
    )
    if isinstance(result, Response):
        return result
    return _done(request, next, f"Post {post_id} cancelled.")


def _parse_when(value: str) -> datetime | None:
    """A `datetime-local` value (or ISO 8601); without an offset it is UTC."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@router.post("/posts/{post_id}/reschedule")
def reschedule_post(
    request: Request,
    post_id: PostId,
    _user: User,
    conn: Conn,
    scheduled_at: Annotated[str, Form(max_length=40)] = "",
    next: NextPath = SCHEDULED_PAGE,
) -> Response:
    now = _now(request)
    when = _parse_when(scheduled_at)
    if when is None:
        return _action_error(request, "Enter a date and time (UTC).", 422)
    if when < now - timedelta(minutes=1):
        return _action_error(request, "Pick a time in the future.", 422)
    if when > now + RESCHEDULE_HORIZON:
        return _action_error(request, "Pick a time within a year.", 422)
    result = _apply(
        request,
        post_id,
        "reschedule",
        lambda: repo.reschedule(conn, post_id, when, now, by="review"),
    )
    if isinstance(result, Response):
        return result
    return _done(
        request, next, f"Post {post_id} rescheduled to {when.strftime('%Y-%m-%d %H:%M')} UTC."
    )
