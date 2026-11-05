"""Campaign link submissions (CLP-97): the queue between "posted" and "paid".

When a campaign post is `posted` with an https URL, `SubmissionPass` (run by the worker
once per poll, next to the posting stage) records a `campaign_submissions` row with the
deadline (`posted_at + submit_within_minutes` from the brief) and pings the operator with
the exact link and the time left. Campaign programs have no documented free submission
API we can use, so the submit itself is a human step: the operator submits the link on
the program's site and marks it submitted in the review app (`/campaigns`) or with
`clipper campaigns submit <id>`. Nothing here scrapes a program's site or logs in to it.
A program that does get a documented API registers a `Submitter`; the pass then submits
for it and records `submitted_by = "api:<program>"`.

A reminder goes out `REMIND_BEFORE` the deadline, and one alert when it is missed.
`mark_submitted` records the time and flags a late submission. `record_earnings` stores
the approved views and payout per submission, and `rpm_rollup` turns them into RPM
(payout per 1,000 approved views) per account and per campaign.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from clipper.campaigns import Campaign
from clipper.db import transaction
from clipper.jobs.models import Clock, from_iso, to_iso, utcnow
from clipper.notify import Notify
from clipper.posting.models import Post, PostStatus
from clipper.redact import redact

REMIND_BEFORE = timedelta(minutes=10)
RETRY_FIRST = timedelta(seconds=30)
RETRY_MAX = timedelta(minutes=10)
RPM_PLACES = Decimal("0.01")

logger = logging.getLogger(__name__)


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"


class AlreadySubmitted(Exception):
    def __init__(self, submission_id: int) -> None:
        super().__init__(f"submission {submission_id} is already submitted")
        self.submission_id = submission_id


@dataclass(frozen=True)
class Submission:
    id: int
    post_id: int
    campaign_id: str
    program: str | None
    account_id: str
    platform: str
    url: str
    posted_at: datetime
    deadline_at: datetime | None
    status: SubmissionStatus
    submitted_at: datetime | None
    submitted_by: str | None
    late: bool
    notified_at: datetime | None
    reminded_at: datetime | None
    overdue_alerted_at: datetime | None
    approved_views: int | None
    payout: Decimal | None
    earnings_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Submission:
        posted = from_iso(row["posted_at"])
        assert posted is not None  # NOT NULL column
        return cls(
            id=row["id"],
            post_id=row["post_id"],
            campaign_id=row["campaign_id"],
            program=row["program"],
            account_id=row["account_id"],
            platform=row["platform"],
            url=row["url"],
            posted_at=posted,
            deadline_at=from_iso(row["deadline_at"]),
            status=SubmissionStatus(row["status"]),
            submitted_at=from_iso(row["submitted_at"]),
            submitted_by=row["submitted_by"],
            late=bool(row["late"]),
            notified_at=from_iso(row["notified_at"]),
            reminded_at=from_iso(row["reminded_at"]),
            overdue_alerted_at=from_iso(row["overdue_alerted_at"]),
            approved_views=row["approved_views"],
            payout=Decimal(row["payout"]) if row["payout"] is not None else None,
            earnings_at=from_iso(row["earnings_at"]),
        )

    def overdue(self, now: datetime) -> bool:
        """Still pending past the deadline."""
        return (
            self.status == SubmissionStatus.PENDING
            and self.deadline_at is not None
            and now > self.deadline_at
        )

    def minutes_left(self, now: datetime) -> int | None:
        if self.deadline_at is None:
            return None
        return math.ceil((self.deadline_at - now).total_seconds() / 60)

    @property
    def safe_url(self) -> str | None:
        """The link if it is plain https (checked again here: templates link it)."""
        parts = urlsplit(self.url)
        return self.url if parts.scheme == "https" and parts.netloc else None

    @property
    def rpm(self) -> Decimal | None:
        return _rpm(self.payout or Decimal(0), self.approved_views or 0)


# --- storage ------------------------------------------------------------------------------


def get_submission(conn: sqlite3.Connection, submission_id: int) -> Submission | None:
    row = conn.execute(
        "SELECT * FROM campaign_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    return Submission.from_row(row) if row else None


def list_submissions(
    conn: sqlite3.Connection, *, pending_only: bool = False, limit: int = 500
) -> list[Submission]:
    where = " WHERE status = 'pending'" if pending_only else ""
    rows = conn.execute(
        f"SELECT * FROM campaign_submissions{where} ORDER BY posted_at, id LIMIT ?",  # noqa: S608 - constant
        (limit,),
    )
    return [Submission.from_row(r) for r in rows]


def _require(conn: sqlite3.Connection, submission_id: int) -> Submission:
    found = get_submission(conn, submission_id)
    if found is None:
        raise LookupError(submission_id)
    return found


def _audit(
    conn: sqlite3.Connection, submission: Submission, action: str, now: datetime, **detail: Any
) -> None:
    """An audit row next to the clip's review history (as the posts table does)."""
    row = conn.execute(
        "SELECT clip_id, clip_version FROM posts WHERE id = ?", (submission.post_id,)
    ).fetchone()
    if row is None:
        return
    payload = {
        "submission_id": submission.id,
        "post_id": submission.post_id,
        "campaign_id": submission.campaign_id,
        **detail,
    }
    conn.execute(
        "INSERT INTO clip_reviews (clip_id, version, action, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (row[0], row[1], action, json.dumps(payload, sort_keys=True), to_iso(now)),
    )


def _new_posts(conn: sqlite3.Connection) -> list[Post]:
    rows = conn.execute(
        "SELECT p.* FROM posts p WHERE p.status = ? AND p.campaign_id IS NOT NULL"
        " AND p.url IS NOT NULL AND p.posted_at IS NOT NULL AND NOT EXISTS"
        " (SELECT 1 FROM campaign_submissions s WHERE s.post_id = p.id) ORDER BY p.id",
        (str(PostStatus.POSTED),),
    )
    return [Post.from_row(r) for r in rows]


def create_due(
    conn: sqlite3.Connection, campaigns: Mapping[str, Campaign], now: datetime
) -> list[Submission]:
    """A pending submission for every posted campaign post with a plain https URL that
    has none yet. The deadline counts from the post time, not from now."""
    created: list[Submission] = []
    for post in _new_posts(conn):
        url = post.safe_url
        if url is None or post.posted_at is None or post.campaign_id is None:
            continue  # only a link we would hand out; logged by the pass
        campaign = campaigns.get(post.campaign_id)
        minutes = campaign.submit_within_minutes if campaign else None
        deadline = post.posted_at + timedelta(minutes=minutes) if minutes else None
        stamp = to_iso(now)
        with transaction(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO campaign_submissions (post_id, campaign_id, program,"
                " account_id, platform, url, posted_at, deadline_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    post.id,
                    post.campaign_id,
                    campaign.program if campaign else None,
                    post.account_id,
                    post.platform,
                    url,
                    to_iso(post.posted_at),
                    to_iso(deadline) if deadline else None,
                    stamp,
                    stamp,
                ),
            )
            if cur.rowcount != 1:
                continue
            submission = _require(conn, int(cur.lastrowid or 0))
            _audit(conn, submission, "campaign_submission_created", now)
        created.append(submission)
    return created


def _set(conn: sqlite3.Connection, submission_id: int, now: datetime, **fields: Any) -> None:
    values = {k: to_iso(v) if isinstance(v, datetime) else v for k, v in fields.items()}
    values["updated_at"] = to_iso(now)
    columns = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE campaign_submissions SET {columns} WHERE id = ?",  # noqa: S608 - our column names
        (*values.values(), submission_id),
    )


def mark_submitted(
    conn: sqlite3.Connection, submission_id: int, at: datetime, *, by: str
) -> Submission:
    """Record that the link was submitted at `at` (by `review`, `cli` or `api:<program>`).
    Late when after the deadline. Submitting twice is refused."""
    with transaction(conn):
        current = _require(conn, submission_id)
        if current.status != SubmissionStatus.PENDING:
            raise AlreadySubmitted(submission_id)
        late = current.deadline_at is not None and at > current.deadline_at
        cur = conn.execute(
            "UPDATE campaign_submissions SET status = ?, submitted_at = ?, submitted_by = ?,"
            " late = ?, updated_at = ? WHERE id = ? AND status = ?",
            (
                str(SubmissionStatus.SUBMITTED),
                to_iso(at),
                by,
                int(late),
                to_iso(at),
                submission_id,
                str(SubmissionStatus.PENDING),
            ),
        )
        if cur.rowcount != 1:
            raise AlreadySubmitted(submission_id)
        done = _require(conn, submission_id)
        _audit(conn, done, "campaign_submitted", at, late=late, by=by)
    if late:
        logger.warning("submission %d was submitted late", submission_id)
    return done


def record_earnings(
    conn: sqlite3.Connection, submission_id: int, *, views: int, payout: Decimal, now: datetime
) -> Submission:
    """Store the approved views and payout the program reported for one submission
    (replacing earlier figures)."""
    if isinstance(views, bool) or not isinstance(views, int) or views < 0:
        raise ValueError("views must be a whole number of at least 0")
    if not isinstance(payout, Decimal) or not payout.is_finite() or payout < 0:
        raise ValueError("payout must be an amount of at least 0")
    with transaction(conn):
        current = _require(conn, submission_id)
        _set(
            conn,
            submission_id,
            now,
            approved_views=views,
            payout=str(payout),
            earnings_at=now,
        )
        _audit(conn, current, "campaign_earnings", now, views=views, payout=str(payout))
        return _require(conn, submission_id)


def parse_payout(text: str) -> Decimal:
    """A payout typed by the operator, e.g. `12.50`. ValueError for anything else."""
    try:
        amount = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"invalid payout {text!r}; use a number like 12.50") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"invalid payout {text!r}; use a number like 12.50")
    return amount


# --- RPM ----------------------------------------------------------------------------------


def _rpm(payout: Decimal, views: int) -> Decimal | None:
    if views <= 0:
        return None
    return (payout * 1000 / views).quantize(RPM_PLACES)


@dataclass
class RpmRow:
    key: str
    submissions: int = 0
    with_earnings: int = 0
    views: int = 0
    payout: Decimal = field(default_factory=lambda: Decimal(0))

    @property
    def rpm(self) -> Decimal | None:
        return _rpm(self.payout, self.views)


@dataclass(frozen=True)
class Rollup:
    by_account: list[RpmRow]
    by_campaign: list[RpmRow]


def rpm_rollup(conn: sqlite3.Connection) -> Rollup:
    """RPM per account and per campaign over every submission. Views and payout count
    only submissions with earnings recorded, so one without figures does not skew RPM."""
    accounts: dict[str, RpmRow] = {}
    campaigns: dict[str, RpmRow] = {}
    rows = conn.execute(
        "SELECT account_id, campaign_id, approved_views, payout FROM campaign_submissions"
    )
    for account_id, campaign_id, views, payout in rows:
        for table, key in ((accounts, account_id), (campaigns, campaign_id)):
            row = table.setdefault(key, RpmRow(key))
            row.submissions += 1
            if views is not None and payout is not None:
                row.with_earnings += 1
                row.views += views
                row.payout += Decimal(payout)
    return Rollup(
        by_account=sorted(accounts.values(), key=lambda r: r.key),
        by_campaign=sorted(campaigns.values(), key=lambda r: r.key),
    )


# --- worker pass ----------------------------------------------------------------------------

Submitter = Callable[[Submission], None]
"""Submits one link to a program's documented API; raises on failure."""


def _when(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "-"


def submission_message(submission: Submission, now: datetime, clip_id: int | None) -> str:
    clip = f", clip {clip_id}" if clip_id is not None else ""
    head = (
        f"Campaign post live: {submission.campaign_id} ({submission.program or 'not configured'})"
        f" on {submission.account_id} ({submission.platform}){clip}."
    )
    if submission.status == SubmissionStatus.SUBMITTED:
        by = submission.submitted_by
        return f"{head}\nLink: <{submission.url}>\nSubmitted automatically ({by})."
    if submission.program is None:
        deadline = "campaign not configured: check its brief for the deadline"
    elif submission.deadline_at is None:
        deadline = "no deadline in the brief"
    else:
        left = submission.minutes_left(now)
        deadline = f"submit by {_when(submission.deadline_at)} ({left} min left)"
    return (
        f"{head}\nLink: <{submission.url}>\n{deadline}.\n"
        f"Submit it on the program's site, then mark it: `clipper campaigns submit"
        f" {submission.id}` or the review app's Campaigns page."
    )


@dataclass
class _Retry:
    at: datetime
    delay: timedelta


@dataclass
class SubmissionPass:
    """One pass per worker poll: record new submissions, ping, remind, alert."""

    conn: sqlite3.Connection
    campaigns: Mapping[str, Campaign]
    notify: Notify
    alert: Notify
    clock: Clock = utcnow
    submitters: Mapping[str, Submitter] = field(default_factory=dict)
    _retry: dict[tuple[int, str], _Retry] = field(default_factory=dict, init=False, repr=False)
    _auto_tried: set[int] = field(default_factory=set, init=False, repr=False)

    def run_once(self) -> int:
        now = self.clock()
        created = create_due(self.conn, self.campaigns, now)
        for submission in created:
            self._auto_submit(submission, now)
        handled = len(created)
        rows = self.conn.execute(
            "SELECT * FROM campaign_submissions WHERE notified_at IS NULL OR (status = ?"
            " AND deadline_at IS NOT NULL AND (reminded_at IS NULL OR"
            " overdue_alerted_at IS NULL)) ORDER BY id",
            (str(SubmissionStatus.PENDING),),
        ).fetchall()
        for row in rows:
            handled += self._ping(Submission.from_row(row), now)
        return handled

    def _auto_submit(self, submission: Submission, now: datetime) -> None:
        submitter = self.submitters.get(submission.program or "")
        if submitter is None or submission.id in self._auto_tried:
            return
        self._auto_tried.add(submission.id)
        try:
            submitter(submission)
        except Exception as exc:
            logger.error(
                "submission %d: automatic submit failed: %s",
                submission.id,
                redact(f"{type(exc).__name__}: {exc}"),
            )
            return
        mark_submitted(self.conn, submission.id, now, by=f"api:{submission.program}")

    def _ping(self, submission: Submission, now: datetime) -> int:
        current = get_submission(self.conn, submission.id) or submission
        if current.notified_at is None:
            return self._send(current, "notified_at", self.notify, now, self._first(current, now))
        if current.status != SubmissionStatus.PENDING or current.deadline_at is None:
            return 0
        if current.overdue(now):
            if current.overdue_alerted_at is None:
                text = (
                    f"Campaign submission missed: {current.campaign_id} on {current.account_id}"
                    f" ({current.platform}), deadline {_when(current.deadline_at)}. Link:"
                    f" <{current.url}>. If you still submit it, record it with"
                    f" `clipper campaigns submit {current.id}` (it will be flagged late)."
                )
                return self._send(current, "overdue_alerted_at", self.alert, now, text)
            return 0
        if current.reminded_at is None and now >= current.deadline_at - REMIND_BEFORE:
            text = (
                f"Reminder: {current.minutes_left(now)} min left to submit"
                f" {current.campaign_id} on {current.account_id}: <{current.url}>"
                f" (`clipper campaigns submit {current.id}`)."
            )
            return self._send(current, "reminded_at", self.notify, now, text)
        return 0

    def _first(self, submission: Submission, now: datetime) -> str:
        row = self.conn.execute(
            "SELECT clip_id FROM posts WHERE id = ?", (submission.post_id,)
        ).fetchone()
        return submission_message(submission, now, row[0] if row else None)

    def _send(
        self, submission: Submission, column: str, send: Notify, now: datetime, text: str
    ) -> int:
        key = (submission.id, column)
        retry = self._retry.get(key)
        if retry is not None and now < retry.at:
            return 0
        try:
            send(text)
        except Exception as exc:
            delay = min(retry.delay * 2, RETRY_MAX) if retry else RETRY_FIRST
            self._retry[key] = _Retry(at=now + delay, delay=delay)
            logger.warning(
                "submission %d: %s ping failed (%s); retry in %ds",
                submission.id,
                column,
                redact(str(exc)),
                int(delay.total_seconds()),
            )
            return 0
        self._retry.pop(key, None)
        with transaction(self.conn):
            _set(self.conn, submission.id, now, **{column: now})
        return 1
