"""Review pages: queue (CLP-19), clip detail + approve/reject (CLP-20), media, export (CLP-21).

Auth and CSRF come from the app (`_AuthGate`, app-wide `csrf_protect`); routes still
declare `require_user`. Media is served only from paths stored in the DB, confined to
`app.state.media_root`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import State

from clipper import clips
from clipper.clips import (
    GATE_REASON_LABELS,
    REJECT_REASONS,
    REVIEWABLE,
    ClipStatus,
    InvalidTransition,
    QueueFilter,
)
from clipper.db import connect
from clipper.export import export_clip, sweep_staging
from clipper.review.app import flash, get_db, render, require_user

router = APIRouter()
logger = logging.getLogger(__name__)

User = Annotated[str, Depends(require_user)]
Conn = Annotated[sqlite3.Connection, Depends(get_db)]
MAX_ID = 2**63 - 1  # SQLite INTEGER range; larger ids are 422, not an OverflowError 500
ClipId = Annotated[int, Path(ge=1, le=MAX_ID)]
VersionNo = Annotated[int, Path(ge=1, le=MAX_ID)]
FormVersion = Annotated[int | None, Form(ge=1, le=MAX_ID)]
STATUS_ALL = "all"
_THUMB_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
MEDIA_CACHE_CONTROL = "private, max-age=300"


def _now(request: Request) -> datetime:
    return datetime.fromtimestamp(request.app.state.wall_clock(), UTC)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse_filter(
    status: str, creator: str | None, min_score: str | None, sort: str
) -> QueueFilter:
    if status == STATUS_ALL:
        status_value = None
    else:
        try:
            status_value = ClipStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail="unknown status") from None
    score: float | None = None
    if min_score not in (None, ""):
        try:
            score = float(min_score)
        except ValueError:
            raise HTTPException(status_code=422, detail="min_score must be a number") from None
        if score != score:  # NaN
            raise HTTPException(status_code=422, detail="min_score must be a number")
    return QueueFilter(
        status=status_value,
        creator=creator or None,
        min_score=score,
        sort="score" if sort == "score" else "newest",
    )


def _query(flt: QueueFilter, **extra: object) -> str:
    params: dict[str, object] = {
        "status": str(flt.status) if flt.status else STATUS_ALL,
        "sort": flt.sort,
    }
    if flt.creator:
        params["creator"] = flt.creator
    if flt.min_score is not None:
        params["min_score"] = f"{flt.min_score:g}"
    params.update(extra)
    return urlencode(params)


@router.get("/", response_class=HTMLResponse)
@router.get("/queue", response_class=HTMLResponse)
def queue(
    request: Request,
    _user: User,
    conn: Conn,
    status: str = ClipStatus.PENDING_REVIEW.value,
    creator: Annotated[str | None, Query(max_length=200)] = None,
    min_score: Annotated[str | None, Query(max_length=32)] = None,
    sort: str = "newest",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> Response:
    flt = _parse_filter(status, creator, min_score, sort)
    cards = clips.list_queue(conn, flt, page=page)
    total = clips.count_queue(conn, flt)
    has_more = page * clips.PAGE_SIZE < total
    ctx = {
        "cards": cards,
        "total": total,
        "flt": flt,
        "page": page,
        "next_page_url": f"/queue?{_query(flt, page=page + 1)}" if has_more else None,
    }
    if _is_htmx(request) and page > 1:
        return render(request, "_queue_cards.html", ctx)
    counts = clips.status_counts(conn, flt)
    status_tabs = [
        (s.value, counts.get(s.value, 0), f"/queue?{_query(_with_status(flt, s))}")
        for s in ClipStatus
    ]
    ctx.update(
        creators=clips.list_creators(conn),
        statuses=[s.value for s in ClipStatus],
        status_tabs=status_tabs,
        all_count=sum(counts.values()),
        all_url=f"/queue?{_query(_with_status(flt, None))}",
        status_value=str(flt.status) if flt.status else STATUS_ALL,
    )
    return render(request, "queue.html", ctx)


def _with_status(flt: QueueFilter, status: ClipStatus | None) -> QueueFilter:
    return QueueFilter(status=status, creator=flt.creator, min_score=flt.min_score, sort=flt.sort)


@router.get("/clip/{clip_id}", response_class=HTMLResponse)
def clip_detail(request: Request, clip_id: ClipId, _user: User, conn: Conn) -> Response:
    clip = clips.get_clip(conn, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    reviews = clips.reviews_for(conn, clip_id)
    last_export_error = None
    if clip.status == ClipStatus.APPROVED:
        last = next((r for r in reviews if r["action"].startswith("export")), None)
        if last is not None and last["action"] == "export_failed" and last["payload"]:
            # Error class only; the full (redacted) detail stays in the server logs.
            error = str(json.loads(last["payload"]).get("error") or "")
            last_export_error = error.partition(":")[0].strip() or "unknown error"
    return render(
        request,
        "clip.html",
        {
            "clip": clip,
            "reviews": reviews,
            "last_export_error": last_export_error,
            "next_id": clips.neighbour_pending(conn, clip_id, older=True),
            "prev_id": clips.neighbour_pending(conn, clip_id, older=False),
            "reasons": REJECT_REASONS,
            "can_review": clip.status in REVIEWABLE,
            "gate_labels": GATE_REASON_LABELS,
            "can_retry_export": clip.status == ClipStatus.APPROVED,
        },
    )


def _action_error(request: Request, message: str, status_code: int) -> Response:
    return render(request, "_action_error.html", {"message": message}, status_code=status_code)


def _move_on(request: Request, conn: sqlite3.Connection, clip_id: int) -> Response:
    nxt = clips.next_pending_after(conn, clip_id)
    location = f"/clip/{nxt}" if nxt is not None else "/queue"
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)


@router.post("/clip/{clip_id}/approve")
def approve_clip(
    request: Request,
    clip_id: ClipId,
    _user: User,
    conn: Conn,
    background: BackgroundTasks,
    version: FormVersion = None,
) -> Response:
    """Commit the approval (+ audit row) and return; the export runs after the response.
    Approving an already-approved clip (the "Retry export" button) re-schedules the export."""
    try:
        result = clips.approve(conn, clip_id, _now(request), expected_version=version)
    except LookupError:
        raise HTTPException(status_code=404, detail="clip not found") from None
    except InvalidTransition as exc:
        return _action_error(request, f"Cannot approve: clip is {exc.current}.", 409)
    if result.status == ClipStatus.APPROVED:
        if request.app.state.export_locks.busy(clip_id):
            flash(request, f"Export already in progress for clip {clip_id}.")
            return _move_on(request, conn, clip_id)
        background.add_task(export_in_background, request.app.state, clip_id)
        flash(
            request,
            f"Clip {clip_id} approved; exporting."
            if result.changed
            else f"Retrying export of clip {clip_id}.",
        )
    return _move_on(request, conn, clip_id)


def export_in_background(state: State, clip_id: int) -> None:
    """Export one clip on its own connection; skipped if that clip is already exporting."""
    locks = state.export_locks
    if not locks.try_acquire(clip_id):
        logger.info("export of clip %d already running; skipped", clip_id)
        return
    try:
        now = datetime.fromtimestamp(state.wall_clock(), UTC)
        sweep_staging(state.output_dir, now)
        conn = connect(state.db_path)
        try:
            export_clip(
                conn,
                clip_id,
                output_dir=state.output_dir,
                media_root=state.media_root,
                now=now,
                rclone_remote=state.rclone_remote,
                runner=state.export_runner,
            )
        finally:
            conn.close()
    except Exception:
        logger.exception("background export of clip %d crashed", clip_id)
    finally:
        locks.release(clip_id)


@router.post("/clip/{clip_id}/reject")
def reject_clip(
    request: Request,
    clip_id: ClipId,
    _user: User,
    conn: Conn,
    reason_tag: Annotated[str, Form(max_length=32)] = "",
    version: FormVersion = None,
) -> Response:
    if reason_tag not in REJECT_REASONS:
        return _action_error(request, "Pick a reject reason.", 422)
    try:
        result = clips.reject(conn, clip_id, reason_tag, _now(request), expected_version=version)
    except LookupError:
        raise HTTPException(status_code=404, detail="clip not found") from None
    except InvalidTransition as exc:
        return _action_error(request, f"Cannot reject: clip is {exc.current}.", 409)
    if result.changed:
        flash(request, f"Clip {clip_id} rejected ({reason_tag}).")
    return _move_on(request, conn, clip_id)


@router.get("/media/clip/{clip_id}/{version}/{kind}")
def clip_media(
    request: Request, clip_id: ClipId, version: VersionNo, kind: str, _user: User, conn: Conn
) -> Response:
    if kind not in ("video", "thumb"):
        raise HTTPException(status_code=404)
    ver = clips.get_version(conn, clip_id, version)
    if ver is None:
        raise HTTPException(status_code=404)
    stored = ver.video_path if kind == "video" else ver.thumb_path
    path = clips.resolve_media_path(request.app.state.media_root, stored)
    if path is None:
        raise HTTPException(status_code=404)
    suffix = path.suffix.lower()
    if kind == "video":
        if suffix != ".mp4":
            raise HTTPException(status_code=404)
        media_type = "video/mp4"
    else:
        media_type = _THUMB_TYPES.get(suffix, "")
        if not media_type:
            raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": MEDIA_CACHE_CONTROL},
    )
