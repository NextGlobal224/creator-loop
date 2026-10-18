-- Version-specific editorial choices are written before the Draft seal.
CREATE TABLE creator_draft_choices (
  draft_version_id TEXT PRIMARY KEY REFERENCES draft_versions(draft_version_id),
  direction_text TEXT NOT NULL CHECK(length(trim(direction_text))>0),
  image_file_id TEXT REFERENCES asset_files(file_id),
  image_sha256 TEXT,
  image_provenance TEXT NOT NULL,
  image_usage_note TEXT NOT NULL,
  CHECK((image_file_id IS NULL AND image_sha256 IS NULL AND image_provenance='' AND image_usage_note='') OR
        (image_file_id IS NOT NULL AND length(image_sha256)=64 AND length(trim(image_provenance))>0 AND length(trim(image_usage_note))>0))
);
CREATE TRIGGER creator_choice_before_seal BEFORE INSERT ON creator_draft_choices
WHEN EXISTS(SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
 OR EXISTS(SELECT 1 FROM creator_draft_choices WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'Draft choices must precede seal'); END;
CREATE TRIGGER creator_choice_image BEFORE INSERT ON creator_draft_choices
WHEN NEW.image_file_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM asset_files f JOIN assets a USING(asset_id) JOIN draft_versions v
 WHERE f.file_id=NEW.image_file_id AND f.sha256=NEW.image_sha256 AND f.role='ORIGINAL'
 AND a.media_type='IMAGE' AND a.deleted_at IS NULL
 AND v.draft_version_id=NEW.draft_version_id AND v.format='IMAGE_CAPTION')
BEGIN SELECT RAISE(ABORT,'Draft image must match registered Original digest and format'); END;
CREATE TRIGGER creator_choice_no_update BEFORE UPDATE ON creator_draft_choices
BEGIN SELECT RAISE(ABORT,'Draft choices are immutable'); END;
CREATE TRIGGER creator_choice_no_delete BEFORE DELETE ON creator_draft_choices
BEGIN SELECT RAISE(ABORT,'Draft choices are immutable'); END;
