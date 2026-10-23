-- Selection metadata from the editing playbook (CLP-51): the playbook version the clip
-- was selected with (NULL before the playbook existed) and Claude's sensitive-moment flag
-- (victim, minor, tragedy), shown to the reviewer.

ALTER TABLE clip_versions ADD COLUMN playbook_version TEXT;
ALTER TABLE clip_versions ADD COLUMN sensitive INTEGER NOT NULL DEFAULT 0;
