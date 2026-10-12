-- URL is a Source; an actual excerpt may have a TEXT original.
CREATE TABLE source_captures (
 source_id TEXT PRIMARY KEY REFERENCES sources(source_id),
 keyword TEXT NOT NULL CHECK(length(trim(keyword)) BETWEEN 1 AND 200),
 captured_at TEXT NOT NULL,
 excerpt TEXT CHECK(excerpt IS NULL OR length(excerpt) BETWEEN 1 AND 8192),
 snapshot_file_id TEXT REFERENCES asset_files(file_id),
 CHECK((excerpt IS NULL AND snapshot_file_id IS NULL) OR (excerpt IS NOT NULL AND snapshot_file_id IS NOT NULL))
);
CREATE TRIGGER source_capture_snapshot BEFORE INSERT ON source_captures
WHEN NEW.snapshot_file_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
 JOIN source_assets sa ON sa.asset_id=a.asset_id
 WHERE f.file_id=NEW.snapshot_file_id AND f.role='ORIGINAL' AND a.media_type='TEXT'
 AND sa.source_id=NEW.source_id AND sa.relationship_type='REFERENCE')
BEGIN SELECT RAISE(ABORT,'capture needs its exact Source TEXT original'); END;
CREATE TRIGGER source_capture_no_update BEFORE UPDATE ON source_captures
BEGIN SELECT RAISE(ABORT,'source capture is immutable'); END;
CREATE TRIGGER source_capture_no_delete BEFORE DELETE ON source_captures
BEGIN SELECT RAISE(ABORT,'source capture is immutable'); END;

CREATE TRIGGER source_capture_no_replace BEFORE INSERT ON source_captures
WHEN EXISTS (SELECT 1 FROM source_captures WHERE source_id=NEW.source_id)
BEGIN SELECT RAISE(ABORT,'source capture is immutable'); END;

CREATE TRIGGER source_capture_keep_link_delete BEFORE DELETE ON source_assets
WHEN OLD.relationship_type='REFERENCE' AND EXISTS (SELECT 1 FROM source_captures c JOIN asset_files f ON f.file_id=c.snapshot_file_id WHERE c.source_id=OLD.source_id AND f.asset_id=OLD.asset_id)
BEGIN SELECT RAISE(ABORT,'capture Source lineage is retained'); END;
CREATE TRIGGER source_capture_keep_link_update BEFORE UPDATE ON source_assets
WHEN OLD.relationship_type='REFERENCE' AND EXISTS (SELECT 1 FROM source_captures c JOIN asset_files f ON f.file_id=c.snapshot_file_id WHERE c.source_id=OLD.source_id AND f.asset_id=OLD.asset_id)
AND (NEW.source_id<>OLD.source_id OR NEW.asset_id<>OLD.asset_id OR NEW.relationship_type<>'REFERENCE')
BEGIN SELECT RAISE(ABORT,'capture Source lineage is retained'); END;
