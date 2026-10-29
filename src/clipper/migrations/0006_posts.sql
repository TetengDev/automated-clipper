-- Posts (CLP-92): one row per (clip, account) the clip is scheduled to. Statuses are
-- validated in Python (`clipper.posting.models.PostStatus`): scheduled, uploading, posted,
-- post_failed, cancelled. A clip's own status is unchanged; `exported` stays terminal.
-- `idempotency_key` (uuid4) lets a publisher find an upload a crashed worker started;
-- `video_path` is the rendered file, so one file never goes to two accounts on a platform.
-- `worker_id`/`claimed_at` record who moved a post to `uploading` and when.

CREATE TABLE posts (
    id              INTEGER PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips (id),
    clip_version    INTEGER NOT NULL,
    account_id      TEXT NOT NULL,
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at    TEXT NOT NULL,
    posted_at       TEXT,
    external_id     TEXT,
    url             TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    video_path      TEXT NOT NULL,
    worker_id       TEXT,
    claimed_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (clip_id, account_id)
);
CREATE INDEX posts_status_due ON posts (status, scheduled_at);
CREATE INDEX posts_account_time ON posts (account_id, scheduled_at);
CREATE UNIQUE INDEX posts_one_file_per_platform ON posts (platform, video_path)
    WHERE status != 'cancelled';
