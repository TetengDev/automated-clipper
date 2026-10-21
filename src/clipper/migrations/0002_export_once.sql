-- At most one successful export per clip version (CLP-21). Failed attempts are unlimited.
-- Keep the first export row of any duplicates written before the index existed.

DELETE FROM clip_reviews WHERE action = 'export' AND id NOT IN (
    SELECT MIN(id) FROM clip_reviews WHERE action = 'export' GROUP BY clip_id, version
);

CREATE UNIQUE INDEX clip_reviews_export_once ON clip_reviews (clip_id, version)
    WHERE action = 'export';
