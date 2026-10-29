-- Per-platform post metadata (CLP-96): a JSON object with a title, caption and hashtags
-- for each of tiktok, instagram, youtube and facebook. NULL for versions stored before
-- CLP-96; readers derive the variants from the single title, caption and hashtags.

ALTER TABLE clip_versions ADD COLUMN variants TEXT;
