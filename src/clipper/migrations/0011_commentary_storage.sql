-- Commentary Shorts (CLP-142, CLP-143). Statuses and JSON shapes are validated in Python.
--
-- story_candidates: every Twitch clip the discovering stage finds (CLP-144), one row per
-- Twitch clip id across all channels and days, so no clip is used for two Shorts.
-- job_id is the day's commentary job that found it, score its candidate score (view
-- velocity, view_count / max(age_hours, 1), times 1 + trending boost + pinned boost),
-- pinned whether its streamer is pinned, skip_reason why a filter dropped it (NULL: it
-- passed), and used_by_clip_id the commentary Short (a clips row) that used it. YouTube
-- moments of pinned YouTube streamers (CLP-152) get their own storage later.
CREATE TABLE story_candidates (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs (id),
    account_id        TEXT NOT NULL,
    twitch_clip_id    TEXT NOT NULL UNIQUE,
    broadcaster_id    TEXT NOT NULL,
    broadcaster_login TEXT NOT NULL,
    broadcaster_name  TEXT,
    game_id           TEXT,
    title             TEXT,
    url               TEXT NOT NULL,
    language          TEXT,
    view_count        INTEGER NOT NULL CHECK (view_count >= 0),
    duration_s        REAL NOT NULL CHECK (duration_s > 0),
    clip_created_at   TEXT NOT NULL,
    trending          INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    score             REAL NOT NULL CHECK (score >= 0),
    skip_reason       TEXT,
    used_by_clip_id   INTEGER REFERENCES clips (id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX story_candidates_job ON story_candidates (job_id, score);
CREATE INDEX story_candidates_used ON story_candidates (used_by_clip_id);

-- streamer_rankings: each day's trending-streamer ranking (CLP-144), for the review page.
-- One row per streamer per commentary job. Rank 1 has the highest views_per_hour: the sum,
-- over the streamer's clips of the channel's games in the last 24 h, of view_count per hour
-- since the clip was made (at least one hour). pinned marks a pinned streamer, kept
-- whatever its rank. Excluded streamers never appear.
CREATE TABLE streamer_rankings (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs (id),
    rank              INTEGER NOT NULL CHECK (rank >= 1),
    broadcaster_id    TEXT NOT NULL,
    broadcaster_login TEXT NOT NULL,
    broadcaster_name  TEXT,
    clip_count        INTEGER NOT NULL CHECK (clip_count >= 0),
    view_count        INTEGER NOT NULL CHECK (view_count >= 0),
    views_per_hour    REAL NOT NULL CHECK (views_per_hour >= 0),
    pinned            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    UNIQUE (job_id, rank),
    UNIQUE (job_id, broadcaster_id)
);

-- commentary_stories: the story behind each version of a commentary Short (CLP-146): its
-- hook and narration script, excerpts (JSON array of {clip_id, start_s, end_s,
-- after_sentence}, where clip_id is a Twitch clip id) and guardrails (JSON object of the
-- figures the transformation checks measured: excerpt count and seconds, runtime and
-- narration share). One row per (clip, version), like clip_versions.
CREATE TABLE commentary_stories (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    hook       TEXT NOT NULL,
    script     TEXT NOT NULL CHECK (length(trim(script)) > 0),
    excerpts   TEXT NOT NULL,
    guardrails TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (clip_id, version),
    FOREIGN KEY (clip_id, version) REFERENCES clip_versions (clip_id, version)
);
