-- The approved slot, kept apart from the retry/deferral time (CLP-115, CLP-120).
-- `publish_at` is when the post goes live: the slot it was approved, rescheduled or
-- released for, a re-picked slot once a deferral outlived the old one, or a retry's slot.
-- `scheduled_at` stays the time of the post's next attempt; a deferral (quota, paused
-- account, claim-time cap) moves only `scheduled_at`. NULL for a held post (no slot).
-- Existing posts take their current `scheduled_at`: a post deferred before this
-- migration has lost its original slot, so its deferral time stands in for it.

-- `posts_account_publish` serves the slot picker's per-account range query on slots.

ALTER TABLE posts ADD COLUMN publish_at TEXT;
UPDATE posts SET publish_at = scheduled_at WHERE status != 'held';
CREATE INDEX posts_account_publish ON posts (account_id, publish_at);
