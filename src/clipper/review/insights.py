"""The insights pages (CLP-154): `/insights` lists the commentary channels and
`/insights/{account_id}` shows one channel's monetisation tracker, 28-day trend, Shorts
table (every Short on the channel, pipeline or hand upload) and per-tag breakdown.
Read-only. Same auth as the other review routes: the app-wide gate plus `require_user`
on every route.
"""

from __future__ import annotations

from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from clipper.accounts import ACCOUNT_ID_PATTERN, token_env_name
from clipper.insights import repo, report
from clipper.insights.tags import TAG_KEYS
from clipper.review.app import render
from clipper.review.routes import Conn, User, _now

router = APIRouter()

INSIGHTS_PAGE = "/insights"
AccountId = Annotated[str, Path(pattern=ACCOUNT_ID_PATTERN, max_length=64)]


def _channels(request: Request) -> list[Any]:
    return [
        a
        for a in request.app.state.accounts
        if getattr(a, "platform", None) == "youtube"
        and getattr(a, "content", "clips") == "commentary"
        and getattr(a, "enabled", True)
    ]


@router.get(INSIGHTS_PAGE, response_class=HTMLResponse)
def insights_index(request: Request, _user: User) -> Response:
    return render(request, "insights_index.html", {"channels": _channels(request)})


@router.get(INSIGHTS_PAGE + "/{account_id}", response_class=HTMLResponse)
def insights_page(request: Request, account_id: AccountId, _user: User, conn: Conn) -> Response:
    account = next((a for a in _channels(request) if a.id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="channel not found")
    now = _now(request)
    # Channel rows are keyed by the account's local day; Short publish times are UTC.
    local_today = now.astimezone(ZoneInfo(account.timezone)).date()
    rows = report.shorts_table(conn, account_id, now)
    breakdown = report.tag_breakdown(rows, now.date())
    return render(
        request,
        "insights.html",
        {
            "account": account,
            "tiers": report.tracker(conn, account_id, local_today),
            "trend": report.trend(conn, account_id, local_today),
            "rows": rows,
            "breakdown": [(key, breakdown[key]) for key in TAG_KEYS if key in breakdown],
            "state": repo.analytics_state(conn, account_id),
            "states": repo.AnalyticsState,
            "token_var": token_env_name(account_id, "refresh_token"),
            "analytics_only": report.ANALYTICS_ONLY,
            "table_days": report.TABLE_DAYS,
            "settled_days": report.FINDINGS_MIN_AGE_DAYS,
        },
    )
