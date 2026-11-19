-- Channel insights (CLP-154). Sources and states are validated in Python.
--
-- channel_videos: every video the insights job knows on a channel: the uploads the Data
-- API lists (hand uploads included) and the pipeline's posted YouTube posts (post_id set).
-- A Short is a video of 180 seconds or less (duration_s from videos.list contentDetails;
-- for a pipeline post not yet seen by the Data API, the rendered length). published_at is
-- when it went public (or goes public, for a post still waiting for its publishAt).
CREATE TABLE channel_videos (
    video_id      TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    post_id       INTEGER REFERENCES posts (id),
    title         TEXT,
    published_at  TEXT NOT NULL,
    duration_s    REAL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX channel_videos_account ON channel_videos (account_id, published_at);
CREATE INDEX channel_videos_post ON channel_videos (post_id);

-- post_metrics: one snapshot of one video's numbers, keyed by the YouTube video id.
-- post_id is set when the video is a pipeline post, NULL for a hand upload. CLP-98
-- planned this table with post_id, captured_at, views, watch_time_s, likes, shares and
-- comments; this story creates it with video_id as the key and post_id optional, and
-- CLP-98 reuses it. source 'public' rows come from the Data API (views, likes, comments,
-- comments_enabled) hourly for 48 h after the Short goes public, then daily. source
-- 'analytics' rows come from the YouTube Analytics API once a day, and through_date is
-- the last day their data covers (Analytics lags 1 to 3 days). traffic is a JSON object
-- of views by insightTrafficSourceType; retention a JSON array of
-- [elapsedVideoTimeRatio, audienceWatchRatio] pairs.
CREATE TABLE post_metrics (
    id               INTEGER PRIMARY KEY,
    video_id         TEXT NOT NULL,
    account_id       TEXT NOT NULL,
    post_id          INTEGER REFERENCES posts (id),
    captured_at      TEXT NOT NULL,
    source           TEXT NOT NULL,
    through_date     TEXT,
    views            INTEGER,
    watch_time_s     REAL,
    likes            INTEGER,
    shares           INTEGER,
    comments         INTEGER,
    engaged_views    INTEGER,
    avg_view_s       REAL,
    avg_view_pct     REAL,
    subs_gained      INTEGER,
    subs_lost        INTEGER,
    comments_enabled INTEGER,
    traffic          TEXT,
    retention        TEXT,
    UNIQUE (video_id, source, captured_at)
);
CREATE INDEX post_metrics_post ON post_metrics (post_id);

-- channel_metrics: one row per account, day and source; a later capture on the same day
-- replaces it. date is the account's local date (public) or the last day the data covers
-- (analytics). YouTube rounds public subscriber counts. A public shorts_views_90d is a
-- lower-bound estimate: the lifetime views of the public Shorts of the last 90 days.
CREATE TABLE channel_metrics (
    id                 INTEGER PRIMARY KEY,
    account_id         TEXT NOT NULL,
    date               TEXT NOT NULL,
    source             TEXT NOT NULL,
    subscribers        INTEGER,
    total_views        INTEGER,
    shorts_views_90d   INTEGER,
    public_uploads_90d INTEGER,
    captured_at        TEXT NOT NULL,
    UNIQUE (account_id, date, source)
);

-- post_tags: format tags of a Short, one value per key, keyed by video_id. Approval tags
-- a pipeline post before it has a video id (post_id only); the insights job fills in
-- video_id once the post is uploaded. Hand uploads have video_id only. The repo refuses
-- a row with neither.
CREATE TABLE post_tags (
    id       INTEGER PRIMARY KEY,
    video_id TEXT,
    post_id  INTEGER REFERENCES posts (id),
    key      TEXT NOT NULL,
    value    TEXT NOT NULL
);
CREATE UNIQUE INDEX post_tags_video_key ON post_tags (video_id, key) WHERE video_id IS NOT NULL;
CREATE UNIQUE INDEX post_tags_post_key ON post_tags (post_id, key) WHERE post_id IS NOT NULL;
CREATE INDEX post_tags_key_value ON post_tags (key, value);

-- insights_state: per account, the outcome of the last Analytics attempt: ok,
-- needs_reauth (the refresh token lacks yt-analytics.readonly), no_token or error.
CREATE TABLE insights_state (
    account_id TEXT PRIMARY KEY,
    analytics  TEXT NOT NULL,
    detail     TEXT,
    checked_at TEXT NOT NULL
);

-- commentary_stories.format: the Short's format as a JSON object with hook_type,
-- structure and topic (written by the scripting stage, CLP-146) and palette (written by
-- the rendering stage, CLP-147). NULL until they do; those tags then read 'unknown'.
ALTER TABLE commentary_stories ADD COLUMN format TEXT;
