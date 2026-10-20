-- Initial schema. Status values are validated in Python (enums), not CHECK constraints,
-- so new states never require a table rebuild. Timestamps: ISO-8601 UTC text.

CREATE TABLE seen_items (
    platform        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    creator         TEXT NOT NULL,
    status          TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT,
    PRIMARY KEY (platform, item_id)
);

CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    platform         TEXT NOT NULL,
    item_id          TEXT NOT NULL,
    creator          TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    title            TEXT,
    duration_seconds INTEGER,
    is_stream        INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'detected',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    lease_until      TEXT,
    worker_id        TEXT,
    last_error       TEXT,
    skip_reason      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (platform, item_id)
);
CREATE INDEX jobs_status_due ON jobs (status, next_attempt_at);

CREATE TABLE clips (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs (id),
    rank            INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending_review',
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (job_id, rank)
);
CREATE INDEX clips_status ON clips (status, created_at);

CREATE TABLE clip_versions (
    id               INTEGER PRIMARY KEY,
    clip_id          INTEGER NOT NULL REFERENCES clips (id),
    version          INTEGER NOT NULL,
    start_s          REAL NOT NULL,
    end_s            REAL NOT NULL,
    title            TEXT,
    caption          TEXT,
    hashtags         TEXT,            -- JSON array
    score            REAL,
    reason           TEXT,
    captions_enabled INTEGER NOT NULL DEFAULT 1,
    note             TEXT,            -- reviewer note that produced this version
    video_path       TEXT,
    thumb_path       TEXT,
    prompt_version   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (clip_id, version),
    CHECK (start_s >= 0 AND end_s > start_s)
);

CREATE TABLE clip_reviews (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clips (id),
    version    INTEGER NOT NULL,
    action     TEXT NOT NULL,
    reason_tag TEXT,
    payload    TEXT,                  -- JSON
    created_at TEXT NOT NULL
);
CREATE INDEX clip_reviews_clip ON clip_reviews (clip_id, created_at);

CREATE TABLE clip_comments (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clips (id),
    version    INTEGER NOT NULL,
    body       TEXT NOT NULL CHECK (length(trim(body)) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    component   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    items_new   INTEGER NOT NULL DEFAULT 0,
    quota_units INTEGER NOT NULL DEFAULT 0,
    errors      TEXT                  -- JSON
);

CREATE TABLE spend (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER REFERENCES jobs (id),
    provider       TEXT NOT NULL,
    kind           TEXT NOT NULL,     -- llm | transcribe
    usd            REAL NOT NULL CHECK (usd >= 0),
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    audio_seconds  REAL,
    created_at     TEXT NOT NULL
);
CREATE INDEX spend_day ON spend (kind, created_at);
