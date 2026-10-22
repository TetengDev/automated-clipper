-- The editing-playbook version the clip was selected with (CLP-51). NULL for clips
-- selected before the playbook existed.

ALTER TABLE clip_versions ADD COLUMN playbook_version TEXT;
