-- What the commentary fetching_sources and transcribing stages (CLP-145) record per
-- candidate. All NULL until the stage gets to the candidate; discovery (CLP-144) never
-- writes them. Paths are relative to the day's job media dir (`candidates/<id>/...`).
--
-- source_path: the fetched Twitch clip (video and audio), e.g. candidates/12/source.mp4.
-- source_kind: 'vertical' when one of the clip's portrait formats was fetched, else
-- 'landscape' (the clip had no vertical version); rendering frames the two differently.
-- loudness_peaks: the clip audio's loudest moments, a JSON array of {t_s, lufs} in time
-- order (the moment hints for the script prompt and the face zoom).
-- transcript_path: the clip's transcript.json (word timestamps), e.g.
-- candidates/12/transcript.json.
-- drop_reason: why the candidate was dropped after discovery ("fetch: ..." or
-- "transcribe: ..."); NULL while it is still in the running. Kept apart from
-- skip_reason, which says why discovery filtered a clip out: a candidate dropped here
-- still counts as one of the day's selected candidates, so a re-run never pulls in the
-- next one in its place.

ALTER TABLE story_candidates ADD COLUMN source_path TEXT;
ALTER TABLE story_candidates ADD COLUMN source_kind TEXT
    CHECK (source_kind IS NULL OR source_kind IN ('vertical', 'landscape'));
ALTER TABLE story_candidates ADD COLUMN loudness_peaks TEXT
    CHECK (loudness_peaks IS NULL OR json_valid(loudness_peaks));
ALTER TABLE story_candidates ADD COLUMN transcript_path TEXT;
ALTER TABLE story_candidates ADD COLUMN drop_reason TEXT;
