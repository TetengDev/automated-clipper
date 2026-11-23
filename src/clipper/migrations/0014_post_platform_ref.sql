-- The platform's id for an upload in progress (CLP-122): the Facebook Reel's video_id or
-- the Instagram container id (and, once published, its media id), as a small JSON object
-- the publisher owns. Written right after the platform creates it and again just before
-- the call that publishes, so a worker restarted mid-upload can check the upload by id
-- (`lookup`). NULL: no Meta upload was started (other platforms, posts from before
-- CLP-122). The CHECK keeps it a valid JSON text of at most 4096 bytes.

ALTER TABLE posts ADD COLUMN platform_ref TEXT
    CHECK (platform_ref IS NULL
           OR (json_valid(platform_ref) AND length(CAST(platform_ref AS BLOB)) <= 4096));
