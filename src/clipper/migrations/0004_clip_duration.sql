-- The rendered length of a clip version (CLP-73). With jump cuts it is shorter than
-- end_s - start_s, which stay the clip's position in the source video. NULL for versions
-- rendered before this column existed: their length is end_s - start_s.

ALTER TABLE clip_versions ADD COLUMN duration_s REAL;
