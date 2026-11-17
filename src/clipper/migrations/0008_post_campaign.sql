-- Campaign pre-flight (CLP-97). `campaign_id` is the campaign a post counts for (NULL: none).
-- `metadata` is the JSON the post is published with (title, caption with the hashtag line,
-- hashtags, flags), built and checked at scheduling, so the publisher sends exactly what
-- the pre-flight passed. NULL for posts scheduled before CLP-97 (built from the clip then).
-- A post that breaks its campaign's brief is `held` (a status validated in Python) with
-- the reason in `error`; it is never published.

ALTER TABLE posts ADD COLUMN campaign_id TEXT;
ALTER TABLE posts ADD COLUMN metadata TEXT;
