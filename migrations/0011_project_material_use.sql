-- Permission to use selected source versions in a Draft is NOT factual ACCEPT
-- or publication Approval. Versions/items/seals are append-only exact snapshots.
CREATE TABLE project_material_use_versions (
  use_version_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  file_id TEXT NOT NULL REFERENCES asset_files(file_id),
  version_no INTEGER NOT NULL CHECK(version_no>0),
  parent_version_id TEXT REFERENCES project_material_use_versions(use_version_id),
  state TEXT NOT NULL CHECK(state IN ('SELECTED','EXCLUDED','REOPENED')),
  actor_id TEXT NOT NULL CHECK(length(trim(actor_id))>0),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id,file_id,version_no)
);
CREATE TABLE project_material_use_items (
  use_version_id TEXT NOT NULL REFERENCES project_material_use_versions(use_version_id),
  position INTEGER NOT NULL CHECK(position>=0),
  evidence_version_id TEXT NOT NULL REFERENCES evidence_versions(evidence_version_id),
  PRIMARY KEY(use_version_id,position),
  UNIQUE(use_version_id,evidence_version_id)
);
CREATE TABLE project_material_use_seals (
  use_version_id TEXT PRIMARY KEY REFERENCES project_material_use_versions(use_version_id),
  sealed_at TEXT NOT NULL
);
CREATE TRIGGER material_use_identity BEFORE INSERT ON project_material_use_versions
WHEN EXISTS(SELECT 1 FROM project_material_use_versions WHERE use_version_id=NEW.use_version_id)
 OR NOT EXISTS(SELECT 1 FROM asset_files f JOIN assets a USING(asset_id)
   JOIN projects p ON p.project_id=NEW.project_id
   WHERE f.file_id=NEW.file_id AND f.role='ORIGINAL' AND a.deleted_at IS NULL
   AND p.status='ACTIVE' AND p.deleted_at IS NULL
   AND EXISTS(SELECT 1 FROM project_references r WHERE r.project_id=p.project_id
     AND (r.asset_id=a.asset_id OR r.source_id IN
       (SELECT source_id FROM source_assets WHERE asset_id=a.asset_id))))
 OR NEW.version_no<>1+COALESCE((SELECT MAX(version_no) FROM project_material_use_versions
   WHERE project_id=NEW.project_id AND file_id=NEW.file_id),0)
 OR (NEW.version_no=1 AND NEW.parent_version_id IS NOT NULL)
 OR (NEW.version_no>1 AND NOT EXISTS(SELECT 1 FROM project_material_use_versions v
   JOIN project_material_use_seals s USING(use_version_id)
   WHERE v.use_version_id=NEW.parent_version_id AND v.project_id=NEW.project_id
   AND v.file_id=NEW.file_id AND v.version_no=NEW.version_no-1))
BEGIN SELECT RAISE(ABORT,'Material use requires linked Project Original and current parent'); END;
CREATE TRIGGER material_use_item BEFORE INSERT ON project_material_use_items
WHEN EXISTS(SELECT 1 FROM project_material_use_seals WHERE use_version_id=NEW.use_version_id)
 OR EXISTS(SELECT 1 FROM project_material_use_items WHERE use_version_id=NEW.use_version_id
   AND (position=NEW.position OR evidence_version_id=NEW.evidence_version_id))
 OR NOT EXISTS(SELECT 1 FROM project_material_use_versions u JOIN evidence_versions v
   ON v.anchor_file_id=u.file_id JOIN evidences e USING(evidence_id)
   WHERE u.use_version_id=NEW.use_version_id AND u.state='SELECTED'
   AND v.evidence_version_id=NEW.evidence_version_id AND e.deleted_at IS NULL)
BEGIN SELECT RAISE(ABORT,'Material use item must match unsealed selected Original'); END;
CREATE TRIGGER material_use_seal BEFORE INSERT ON project_material_use_seals
WHEN EXISTS(SELECT 1 FROM project_material_use_seals WHERE use_version_id=NEW.use_version_id)
 OR NOT EXISTS(SELECT 1 FROM project_material_use_versions WHERE use_version_id=NEW.use_version_id)
 OR EXISTS(SELECT 1 FROM project_material_use_versions u WHERE u.use_version_id=NEW.use_version_id
   AND ((u.state='SELECTED' AND NOT EXISTS(SELECT 1 FROM project_material_use_items i
     WHERE i.use_version_id=u.use_version_id))
    OR (u.state<>'SELECTED' AND EXISTS(SELECT 1 FROM project_material_use_items i
     WHERE i.use_version_id=u.use_version_id))))
BEGIN SELECT RAISE(ABORT,'Material use seal requires complete selection'); END;
CREATE TRIGGER material_use_version_no_update BEFORE UPDATE ON project_material_use_versions
BEGIN SELECT RAISE(ABORT,'Material use versions are immutable'); END;
CREATE TRIGGER material_use_version_no_delete BEFORE DELETE ON project_material_use_versions
BEGIN SELECT RAISE(ABORT,'Material use versions are immutable'); END;
CREATE TRIGGER material_use_item_no_update BEFORE UPDATE ON project_material_use_items
BEGIN SELECT RAISE(ABORT,'Material use items are immutable'); END;
CREATE TRIGGER material_use_item_no_delete BEFORE DELETE ON project_material_use_items
BEGIN SELECT RAISE(ABORT,'Material use items are immutable'); END;
CREATE TRIGGER material_use_seal_no_update BEFORE UPDATE ON project_material_use_seals
BEGIN SELECT RAISE(ABORT,'Material use seals are immutable'); END;
CREATE TRIGGER material_use_seal_no_delete BEFORE DELETE ON project_material_use_seals
BEGIN SELECT RAISE(ABORT,'Material use seals are immutable'); END;
