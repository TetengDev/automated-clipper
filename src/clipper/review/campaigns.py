"""Review pages for campaign submissions (CLP-97): the Campaigns page lists the links to
submit with their deadlines, lets the operator mark one submitted, and shows RPM per
account and campaign.

Same auth and CSRF as the other review routes: the app-wide gate and `csrf_protect`, plus
`require_user` on every route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from clipper import submissions
from clipper.review.app import flash, render
from clipper.review.routes import MAX_ID, Conn, User, _action_error, _is_htmx, _now

router = APIRouter()

CAMPAIGNS_PAGE = "/campaigns"
RECENT_SUBMITTED = 50
SubmissionId = Annotated[int, Path(ge=1, le=MAX_ID)]


@router.get(CAMPAIGNS_PAGE, response_class=HTMLResponse)
def campaigns_page(request: Request, _user: User, conn: Conn) -> Response:
    rows = submissions.list_submissions(conn, limit=5_000)
    pending = [s for s in rows if s.status == submissions.SubmissionStatus.PENDING]
    done = [s for s in rows if s.status == submissions.SubmissionStatus.SUBMITTED]
    done = sorted(done, key=lambda s: s.submitted_at or s.posted_at, reverse=True)
    return render(
        request,
        "campaigns.html",
        {
            "pending": pending,
            "submitted": done[:RECENT_SUBMITTED],
            "rollup": submissions.rpm_rollup(conn),
            "now": _now(request),
        },
    )


@router.post("/campaigns/submissions/{submission_id}/submitted")
def mark_submitted(
    request: Request, submission_id: SubmissionId, _user: User, conn: Conn
) -> Response:
    try:
        done = submissions.mark_submitted(conn, submission_id, _now(request), by="review")
    except LookupError:
        raise HTTPException(status_code=404, detail="submission not found") from None
    except submissions.AlreadySubmitted:
        return _action_error(request, f"Submission {submission_id} is already submitted.", 409)
    late = " (late)" if done.late else ""
    flash(request, f"Submission {submission_id} marked submitted{late}.")
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": CAMPAIGNS_PAGE})
    return RedirectResponse(CAMPAIGNS_PAGE, status_code=303)
