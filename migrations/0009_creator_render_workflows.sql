-- Additive workflow provenance; core Evidence/Claim/Draft/Approval guards remain.
CREATE TABLE creator_workflows (
  workflow_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  mode TEXT NOT NULL CHECK(mode IN ('AUTOMATIC','MANUAL')),
  output_format TEXT NOT NULL CHECK(output_format IN ('TEXT','IMAGE_CAPTION','VIDEO')),
  purpose TEXT NOT NULL, audience TEXT NOT NULL, style TEXT NOT NULL,
  backend_name TEXT NOT NULL, backend_version TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL, analysis_text TEXT NOT NULL, direction_text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE creator_workflow_materials (
  workflow_id TEXT NOT NULL REFERENCES creator_workflows(workflow_id),
  position INTEGER NOT NULL CHECK(position>=0), source_id TEXT REFERENCES sources(source_id),
  file_id TEXT REFERENCES asset_files(file_id), sha256 TEXT,
  evidence_version_id TEXT REFERENCES evidence_versions(evidence_version_id),
  CHECK(source_id IS NOT NULL OR file_id IS NOT NULL),
  CHECK((file_id IS NULL AND sha256 IS NULL) OR (file_id IS NOT NULL AND length(sha256)=64)),
  PRIMARY KEY(workflow_id,position)
);
CREATE TABLE creator_workflow_claims (
  workflow_id TEXT NOT NULL REFERENCES creator_workflows(workflow_id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
  PRIMARY KEY(workflow_id,claim_version_id)
);
CREATE TABLE creator_workflow_drafts (
  workflow_id TEXT NOT NULL REFERENCES creator_workflows(workflow_id),
  draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  created_at TEXT NOT NULL, PRIMARY KEY(workflow_id,draft_version_id)
);
CREATE TRIGGER creator_material_digest BEFORE INSERT ON creator_workflow_materials
WHEN NEW.file_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM asset_files WHERE file_id=NEW.file_id AND sha256=NEW.sha256)
BEGIN SELECT RAISE(ABORT,'workflow input digest mismatch'); END;
CREATE TRIGGER creator_material_anchor BEFORE INSERT ON creator_workflow_materials
WHEN NEW.evidence_version_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM evidence_versions WHERE evidence_version_id=NEW.evidence_version_id AND anchor_file_id=NEW.file_id)
BEGIN SELECT RAISE(ABORT,'workflow evidence anchor mismatch'); END;
CREATE TRIGGER creator_draft_project BEFORE INSERT ON creator_workflow_drafts
WHEN NOT EXISTS
 (SELECT 1 FROM creator_workflows w JOIN drafts d ON d.project_id=w.project_id
 JOIN draft_versions v ON v.draft_id=d.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
 WHERE w.workflow_id=NEW.workflow_id AND v.draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'workflow Draft must be sealed in the same Project'); END;

-- Generated MP4s have Draft/run/input lineage; they are not falsely parented to
-- an unrelated Asset. Explicit later import can create an actual VIDEO Asset.
CREATE TABLE draft_render_requests (
  render_run_id TEXT PRIMARY KEY REFERENCES processing_runs(run_id),
  draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  request_json TEXT NOT NULL CHECK(json_valid(request_json)),
  input_fingerprint TEXT NOT NULL, sealed_at TEXT
);
CREATE TABLE draft_render_inputs (
  render_run_id TEXT NOT NULL REFERENCES draft_render_requests(render_run_id),
  position INTEGER NOT NULL CHECK(position>=0), file_id TEXT NOT NULL REFERENCES asset_files(file_id),
  sha256 TEXT NOT NULL CHECK(length(sha256)=64),
  usage_type TEXT NOT NULL CHECK(usage_type IN ('SCRIPT','IMAGE','CLIP','AUDIO')),
  PRIMARY KEY(render_run_id,position)
);
CREATE TABLE draft_render_outputs (
  render_run_id TEXT PRIMARY KEY REFERENCES draft_render_requests(render_run_id),
  storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
  byte_size INTEGER NOT NULL CHECK(byte_size>0), mime_type TEXT NOT NULL CHECK(mime_type='video/mp4'),
  duration_ms INTEGER NOT NULL CHECK(duration_ms>0), width_px INTEGER NOT NULL CHECK(width_px>0),
  height_px INTEGER NOT NULL CHECK(height_px>0), tool_name TEXT NOT NULL, tool_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER render_request_draft BEFORE INSERT ON draft_render_requests
WHEN NOT EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'render requires an exact sealed Draft'); END;
CREATE TRIGGER render_input_guard BEFORE INSERT ON draft_render_inputs
WHEN EXISTS(SELECT 1 FROM draft_render_requests WHERE render_run_id=NEW.render_run_id AND sealed_at IS NOT NULL)
 OR NOT EXISTS(SELECT 1 FROM asset_files WHERE file_id=NEW.file_id AND sha256=NEW.sha256)
BEGIN SELECT RAISE(ABORT,'render input sealed or digest mismatch'); END;
CREATE TRIGGER render_request_seal BEFORE UPDATE ON draft_render_requests
WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL
 OR NEW.render_run_id!=OLD.render_run_id OR NEW.draft_version_id!=OLD.draft_version_id
 OR NEW.request_json!=OLD.request_json OR NEW.input_fingerprint!=OLD.input_fingerprint
 OR NOT EXISTS(SELECT 1 FROM draft_render_inputs WHERE render_run_id=NEW.render_run_id AND usage_type='SCRIPT')
BEGIN SELECT RAISE(ABORT,'invalid render seal'); END;
CREATE TRIGGER render_output_complete BEFORE INSERT ON draft_render_outputs
WHEN NOT EXISTS(SELECT 1 FROM draft_render_requests q JOIN processing_runs r ON r.run_id=q.render_run_id
 WHERE q.render_run_id=NEW.render_run_id AND q.sealed_at IS NOT NULL AND r.status='SUCCEEDED'
 AND r.task_type='DRAFT_VIDEO_RENDER')
BEGIN SELECT RAISE(ABORT,'incomplete render cannot be an output'); END;

CREATE TRIGGER creator_workflows_no_replace BEFORE INSERT ON creator_workflows
WHEN EXISTS(SELECT 1 FROM creator_workflows WHERE workflow_id=NEW.workflow_id)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER creator_workflows_no_update BEFORE UPDATE ON creator_workflows
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER creator_workflows_no_delete BEFORE DELETE ON creator_workflows
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER creator_workflow_materials_no_replace BEFORE INSERT ON creator_workflow_materials
WHEN EXISTS(SELECT 1 FROM creator_workflow_materials WHERE workflow_id=NEW.workflow_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER creator_workflow_materials_no_update BEFORE UPDATE ON creator_workflow_materials
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER creator_workflow_materials_no_delete BEFORE DELETE ON creator_workflow_materials
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER creator_workflow_claims_no_replace BEFORE INSERT ON creator_workflow_claims
WHEN EXISTS(SELECT 1 FROM creator_workflow_claims WHERE workflow_id=NEW.workflow_id AND claim_version_id=NEW.claim_version_id)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER creator_workflow_claims_no_update BEFORE UPDATE ON creator_workflow_claims
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER creator_workflow_claims_no_delete BEFORE DELETE ON creator_workflow_claims
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER creator_workflow_drafts_no_replace BEFORE INSERT ON creator_workflow_drafts
WHEN EXISTS(SELECT 1 FROM creator_workflow_drafts WHERE workflow_id=NEW.workflow_id AND draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER creator_workflow_drafts_no_update BEFORE UPDATE ON creator_workflow_drafts
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER creator_workflow_drafts_no_delete BEFORE DELETE ON creator_workflow_drafts
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER draft_render_requests_no_replace BEFORE INSERT ON draft_render_requests
WHEN EXISTS(SELECT 1 FROM draft_render_requests WHERE render_run_id=NEW.render_run_id)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER draft_render_requests_no_delete BEFORE DELETE ON draft_render_requests
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER draft_render_inputs_no_replace BEFORE INSERT ON draft_render_inputs
WHEN EXISTS(SELECT 1 FROM draft_render_inputs WHERE render_run_id=NEW.render_run_id AND position=NEW.position)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER draft_render_inputs_no_update BEFORE UPDATE ON draft_render_inputs
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER draft_render_inputs_no_delete BEFORE DELETE ON draft_render_inputs
BEGIN SELECT RAISE(ABORT,'immutable history'); END;

CREATE TRIGGER draft_render_outputs_no_replace BEFORE INSERT ON draft_render_outputs
WHEN EXISTS(SELECT 1 FROM draft_render_outputs WHERE render_run_id=NEW.render_run_id)
BEGIN SELECT RAISE(ABORT,'immutable identity'); END;
CREATE TRIGGER draft_render_outputs_no_update BEFORE UPDATE ON draft_render_outputs
BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER draft_render_outputs_no_delete BEFORE DELETE ON draft_render_outputs
BEGIN SELECT RAISE(ABORT,'immutable history'); END;
