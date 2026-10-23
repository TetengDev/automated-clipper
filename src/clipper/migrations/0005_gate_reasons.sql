-- Quality gates (CLP-74): the gates a clip version failed, as a JSON array of reason
-- codes. [] passed every gate; NULL was not gated (rendered before the gates, or a
-- re-render).

ALTER TABLE clip_versions ADD COLUMN gate_reasons TEXT;
