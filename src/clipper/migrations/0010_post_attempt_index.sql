-- Claim-time caps (CLP-106) count each account's failed upload attempts from the audit
-- rows by when they happened, and its uploads from posts changed in the window.

CREATE INDEX clip_reviews_action_time ON clip_reviews (action, created_at);
CREATE INDEX posts_account_updated ON posts (account_id, updated_at);
