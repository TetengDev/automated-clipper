-- Campaign link submissions (CLP-97). One row per posted campaign post with a URL: the
-- link to hand to the campaign program, the deadline (posted_at + the brief's
-- submit_within_minutes; NULL when the brief has none), when it was submitted and by
-- whom, whether that was late, and the earnings the program approved.
-- `status` is validated in Python (`clipper.submissions.SubmissionStatus`): pending,
-- submitted. `payout` is a decimal string (summed in Python, never as REAL).
-- notified_at / reminded_at / overdue_alerted_at record the operator pings so each is
-- sent once.

CREATE TABLE campaign_submissions (
    id                  INTEGER PRIMARY KEY,
    post_id             INTEGER NOT NULL UNIQUE REFERENCES posts (id),
    campaign_id         TEXT NOT NULL,
    program             TEXT,
    account_id          TEXT NOT NULL,
    platform            TEXT NOT NULL,
    url                 TEXT NOT NULL,
    posted_at           TEXT NOT NULL,
    deadline_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    submitted_at        TEXT,
    submitted_by        TEXT,
    late                INTEGER NOT NULL DEFAULT 0,
    notified_at         TEXT,
    reminded_at         TEXT,
    overdue_alerted_at  TEXT,
    approved_views      INTEGER,
    payout              TEXT,
    earnings_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX campaign_submissions_status ON campaign_submissions (status, deadline_at);
CREATE INDEX campaign_submissions_campaign ON campaign_submissions (campaign_id);
