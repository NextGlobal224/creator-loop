CREATE TABLE schema_migrations (
  id TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL, app_version TEXT NOT NULL
);
CREATE TABLE sources (
  source_id TEXT PRIMARY KEY, platform TEXT NOT NULL, canonical_url TEXT, external_id TEXT,
  publisher_name TEXT, published_at TEXT, captured_at TEXT,
  rights_status TEXT NOT NULL CHECK (rights_status IN ('UNKNOWN','OWNED','LICENSED','REFERENCE_ONLY','RESTRICTED')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX sources_external_id ON sources(platform, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY, media_type TEXT NOT NULL CHECK (media_type IN ('VIDEO','IMAGE','TEXT','AUDIO','DOCUMENT')),
  display_name TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE source_assets (
  source_id TEXT NOT NULL REFERENCES sources(source_id), asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  relationship_type TEXT NOT NULL CHECK (relationship_type IN ('ORIGIN','REPOST','REFERENCE','UNKNOWN')),
  verification_status TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY(source_id,asset_id,relationship_type)
);
CREATE TABLE processing_runs (
  run_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id), input_file_id TEXT REFERENCES asset_files(file_id),
  task_type TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  tool_name TEXT NOT NULL, tool_version TEXT NOT NULL, model_name TEXT, model_version TEXT,
  started_at TEXT, finished_at TEXT, error_code TEXT, error_message TEXT, created_at TEXT NOT NULL,
  CHECK (status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR finished_at IS NOT NULL),
  CHECK (status != 'RUNNING' OR started_at IS NOT NULL)
);
CREATE TABLE asset_files (
  file_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  role TEXT NOT NULL CHECK (role IN ('ORIGINAL','DERIVED_AUDIO','DERIVED_FRAME','THUMBNAIL','OTHER')),
  storage_key TEXT NOT NULL, sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size>=0),
  mime_type TEXT NOT NULL, parent_file_id TEXT REFERENCES asset_files(file_id),
  processing_run_id TEXT REFERENCES processing_runs(run_id), created_at TEXT NOT NULL,
  width_px INTEGER, height_px INTEGER, duration_ms INTEGER,
  UNIQUE(file_id, asset_id)
);
-- Resolve forward reference after both tables exist.
CREATE TRIGGER processing_input_same_asset_insert BEFORE INSERT ON processing_runs
WHEN NEW.input_file_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM asset_files WHERE file_id=NEW.input_file_id AND asset_id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'processing input belongs to another asset'); END;
CREATE TRIGGER processing_input_same_asset BEFORE UPDATE OF input_file_id,asset_id ON processing_runs
WHEN NEW.input_file_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM asset_files WHERE file_id=NEW.input_file_id AND asset_id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'processing input belongs to another asset'); END;
CREATE TRIGGER asset_file_parent_same_asset_update BEFORE UPDATE OF parent_file_id,asset_id ON asset_files
WHEN NEW.parent_file_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM asset_files WHERE file_id=NEW.parent_file_id AND asset_id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'parent file belongs to another asset'); END;
CREATE TRIGGER asset_file_parent_same_asset BEFORE INSERT ON asset_files
WHEN NEW.parent_file_id IS NOT NULL AND NOT EXISTS
 (SELECT 1 FROM asset_files WHERE file_id=NEW.parent_file_id AND asset_id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'parent file belongs to another asset'); END;
CREATE TABLE evidences (
  evidence_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  evidence_type TEXT NOT NULL CHECK (evidence_type IN ('SPEECH','OCR_TEXT','VISUAL_OBSERVATION','DIRECT_TEXT','METADATA','OTHER')),
  created_at TEXT NOT NULL, deleted_at TEXT, UNIQUE(evidence_id,asset_id)
);
CREATE TABLE evidence_versions (
  evidence_version_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidences(evidence_id),
  asset_id TEXT NOT NULL, version_no INTEGER NOT NULL CHECK(version_no>0),
  anchor_file_id TEXT NOT NULL, content TEXT NOT NULL, locator_type TEXT NOT NULL
    CHECK(locator_type IN ('TIME_RANGE','IMAGE_REGION','TEXT_RANGE','WHOLE_ASSET')),
  locator_data TEXT NOT NULL CHECK(json_valid(locator_data)),
  producer_type TEXT NOT NULL CHECK(producer_type IN ('MODEL','TOOL','HUMAN')),
  processing_run_id TEXT REFERENCES processing_runs(run_id), created_by TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(evidence_id,asset_id) REFERENCES evidences(evidence_id,asset_id),
  FOREIGN KEY(anchor_file_id,asset_id) REFERENCES asset_files(file_id,asset_id),
  UNIQUE(evidence_id,version_no)
);
CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY, claim_type TEXT NOT NULL
    CHECK(claim_type IN ('FACTUAL','INTERPRETIVE','EDITORIAL_HYPOTHESIS')),
  created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE claim_versions (
  claim_version_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  version_no INTEGER NOT NULL CHECK(version_no>0), statement TEXT NOT NULL,
  created_by TEXT, created_at TEXT NOT NULL, UNIQUE(claim_id,version_no)
);
CREATE TABLE claim_evidence (
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
  evidence_version_id TEXT NOT NULL REFERENCES evidence_versions(evidence_version_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN ('SUPPORTS','CONTRADICTS','CONTEXT')),
  PRIMARY KEY(claim_version_id,evidence_version_id,relation_type)
);
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','ARCHIVED')), created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE project_references (
  project_reference_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  asset_id TEXT REFERENCES assets(asset_id), claim_version_id TEXT REFERENCES claim_versions(claim_version_id),
  source_id TEXT REFERENCES sources(source_id),
  usage_intent TEXT NOT NULL CHECK(usage_intent IN ('RESEARCH','QUOTE','REUSE_MEDIA')),
  CHECK ((asset_id IS NOT NULL)+(claim_version_id IS NOT NULL)+(source_id IS NOT NULL)=1)
);
CREATE TABLE drafts (
  draft_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','ARCHIVED')), created_at TEXT NOT NULL,
  UNIQUE(draft_id,project_id)
);
CREATE TABLE draft_versions (
  draft_version_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
  version_no INTEGER NOT NULL CHECK(version_no>0), body_text TEXT NOT NULL, format TEXT NOT NULL,
  created_by TEXT, created_at TEXT NOT NULL, UNIQUE(draft_id,version_no), UNIQUE(draft_version_id,draft_id)
);
CREATE TABLE draft_version_parents (
  child_draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  parent_draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id), relation_type TEXT NOT NULL,
  CHECK(child_draft_version_id!=parent_draft_version_id),
  PRIMARY KEY(child_draft_version_id,parent_draft_version_id)
);
CREATE TABLE draft_claims (
  draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
  use_type TEXT NOT NULL CHECK(use_type IN ('ASSERTED','INSPIRATION','QUOTE','BACKGROUND')),
  PRIMARY KEY(draft_version_id,claim_version_id)
);
CREATE TABLE draft_assertions (
  assertion_id TEXT PRIMARY KEY, draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  text_start INTEGER NOT NULL, text_end INTEGER NOT NULL, asserted_text TEXT NOT NULL,
  claim_version_id TEXT REFERENCES claim_versions(claim_version_id),
  review_state TEXT NOT NULL CHECK(review_state IN ('UNREVIEWED','SUPPORTED','NEEDS_SOURCE','EDITORIAL')),
  CHECK(0<=text_start AND text_start<text_end)
);
CREATE TABLE selection_events (
  selection_event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  selected_draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  candidate_set_id TEXT NOT NULL, reason_text TEXT, actor_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE selection_candidates (
  selection_event_id TEXT NOT NULL REFERENCES selection_events(selection_event_id),
  draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  PRIMARY KEY(selection_event_id,draft_version_id)
);
CREATE TABLE review_events (
  review_event_id TEXT PRIMARY KEY, evidence_version_id TEXT REFERENCES evidence_versions(evidence_version_id),
  claim_version_id TEXT REFERENCES claim_versions(claim_version_id),
  draft_version_id TEXT REFERENCES draft_versions(draft_version_id),
  action TEXT NOT NULL CHECK(action IN ('ACCEPT','CORRECT','REJECT','REQUEST_CHANGES','REOPEN')),
  actor_id TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  CHECK ((evidence_version_id IS NOT NULL)+(claim_version_id IS NOT NULL)+(draft_version_id IS NOT NULL)=1)
);
CREATE TABLE publication_packages (
  package_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  draft_version_id TEXT NOT NULL REFERENCES draft_versions(draft_version_id),
  platform TEXT NOT NULL, format TEXT NOT NULL, fingerprint TEXT NOT NULL, item_count INTEGER NOT NULL CHECK(item_count>0), sealed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE publication_package_items (
  package_item_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES publication_packages(package_id),
  item_type TEXT NOT NULL CHECK(item_type IN ('CAPTION','MEDIA','THUMBNAIL','CTA','ALT_TEXT','OTHER')),
  position INTEGER NOT NULL CHECK(position>=0), text_payload TEXT, file_id TEXT REFERENCES asset_files(file_id),
  content_digest TEXT NOT NULL, UNIQUE(package_id,item_type,position),
  CHECK ((text_payload IS NOT NULL)+(file_id IS NOT NULL)=1)
);
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES publication_packages(package_id),
  package_fingerprint TEXT NOT NULL, decision TEXT NOT NULL
    CHECK(decision IN ('APPROVED','REJECTED','REVOKED')),
  actor_id TEXT NOT NULL, decided_at TEXT NOT NULL, reason TEXT
);
CREATE TABLE posts (
  post_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES publication_packages(package_id),
  platform TEXT NOT NULL, external_post_id TEXT, external_url TEXT, published_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('PENDING','PUBLISHED','FAILED','REMOVED')), created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX posts_external_id ON posts(platform,external_post_id) WHERE external_post_id IS NOT NULL;
CREATE TABLE observations (
  observation_id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES posts(post_id), observed_at TEXT NOT NULL,
  collector_type TEXT NOT NULL CHECK(collector_type IN ('API','MANUAL','IMPORT')),
  processing_run_id TEXT REFERENCES processing_runs(run_id), created_at TEXT NOT NULL
);
CREATE TABLE observation_metrics (
  observation_id TEXT NOT NULL REFERENCES observations(observation_id), metric_key TEXT NOT NULL,
  metric_scope TEXT NOT NULL CHECK(metric_scope IN ('POST','ACCOUNT','OTHER')),
  numeric_value REAL NOT NULL, unit TEXT NOT NULL, definition_version TEXT NOT NULL, raw_value TEXT,
  PRIMARY KEY(observation_id,metric_key,metric_scope)
);
CREATE TABLE tags (
  tag_id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE asset_tags (
  asset_id TEXT NOT NULL REFERENCES assets(asset_id), tag_id TEXT NOT NULL REFERENCES tags(tag_id),
  PRIMARY KEY(asset_id,tag_id)
);
-- Versioned records and approved snapshots are append-only.
CREATE TRIGGER evidence_version_no_update BEFORE UPDATE ON evidence_versions BEGIN SELECT RAISE(ABORT,'evidence version immutable'); END;
CREATE TRIGGER evidence_version_no_delete BEFORE DELETE ON evidence_versions BEGIN SELECT RAISE(ABORT,'evidence version immutable'); END;
CREATE TRIGGER claim_version_no_update BEFORE UPDATE ON claim_versions BEGIN SELECT RAISE(ABORT,'claim version immutable'); END;
CREATE TRIGGER claim_version_no_delete BEFORE DELETE ON claim_versions BEGIN SELECT RAISE(ABORT,'claim version immutable'); END;
CREATE TRIGGER draft_version_no_update BEFORE UPDATE ON draft_versions BEGIN SELECT RAISE(ABORT,'draft version immutable'); END;
CREATE TRIGGER draft_version_no_delete BEFORE DELETE ON draft_versions BEGIN SELECT RAISE(ABORT,'draft version immutable'); END;
CREATE TRIGGER package_seal_guard BEFORE UPDATE OF sealed_at ON publication_packages
WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR
  (SELECT count(*) FROM publication_package_items WHERE package_id=NEW.package_id) != NEW.item_count OR
  NOT EXISTS (SELECT 1 FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
              WHERE v.draft_version_id=NEW.draft_version_id AND d.project_id=NEW.project_id)
BEGIN SELECT RAISE(ABORT,'package cannot be sealed'); END;
CREATE TRIGGER package_no_update BEFORE UPDATE ON publication_packages
WHEN NOT (OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL AND
          NEW.package_id=OLD.package_id AND NEW.project_id=OLD.project_id AND
          NEW.draft_version_id=OLD.draft_version_id AND NEW.platform=OLD.platform AND
          NEW.format=OLD.format AND NEW.fingerprint=OLD.fingerprint AND
          NEW.item_count=OLD.item_count AND NEW.created_at=OLD.created_at)
BEGIN SELECT RAISE(ABORT,'package immutable'); END;
CREATE TRIGGER package_no_delete BEFORE DELETE ON publication_packages BEGIN SELECT RAISE(ABORT,'package immutable'); END;
CREATE TRIGGER package_item_no_update BEFORE UPDATE ON publication_package_items BEGIN SELECT RAISE(ABORT,'package item immutable'); END;
CREATE TRIGGER package_item_no_delete BEFORE DELETE ON publication_package_items BEGIN SELECT RAISE(ABORT,'package item immutable'); END;
CREATE TRIGGER observation_no_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT,'observation immutable'); END;
CREATE TRIGGER observation_no_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT,'observation immutable'); END;
CREATE TRIGGER observation_metric_no_update BEFORE UPDATE ON observation_metrics BEGIN SELECT RAISE(ABORT,'observation metric immutable'); END;
CREATE TRIGGER observation_metric_no_delete BEFORE DELETE ON observation_metrics BEGIN SELECT RAISE(ABORT,'observation metric immutable'); END;
CREATE TRIGGER approval_requires_items BEFORE INSERT ON approvals
WHEN (SELECT sealed_at FROM publication_packages WHERE package_id=NEW.package_id) IS NULL OR
  (SELECT count(*) FROM publication_package_items WHERE package_id=NEW.package_id) !=
     (SELECT item_count FROM publication_packages WHERE package_id=NEW.package_id)
  OR NEW.package_fingerprint != (SELECT fingerprint FROM publication_packages WHERE package_id=NEW.package_id)
BEGIN SELECT RAISE(ABORT,'package missing items or fingerprint mismatch'); END;
CREATE TRIGGER package_item_count_cap BEFORE INSERT ON publication_package_items
WHEN (SELECT sealed_at FROM publication_packages WHERE package_id=NEW.package_id) IS NOT NULL OR
  (SELECT count(*) FROM publication_package_items WHERE package_id=NEW.package_id) >=
     (SELECT item_count FROM publication_packages WHERE package_id=NEW.package_id)
BEGIN SELECT RAISE(ABORT,'package item count exceeded'); END;
CREATE TRIGGER review_event_no_update BEFORE UPDATE ON review_events BEGIN SELECT RAISE(ABORT,'review event immutable'); END;
CREATE TRIGGER review_event_no_delete BEFORE DELETE ON review_events BEGIN SELECT RAISE(ABORT,'review event immutable'); END;
CREATE TRIGGER approval_no_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT,'approval immutable'); END;
CREATE TRIGGER approval_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT,'approval immutable'); END;
CREATE TRIGGER post_platform_matches_update BEFORE UPDATE OF platform,package_id ON posts
WHEN NEW.platform != (SELECT platform FROM publication_packages WHERE package_id=NEW.package_id)
BEGIN SELECT RAISE(ABORT,'post platform mismatch'); END;
CREATE TRIGGER draft_project_immutable BEFORE UPDATE OF project_id ON drafts BEGIN SELECT RAISE(ABORT,'draft project immutable'); END;
CREATE TRIGGER package_project_matches BEFORE INSERT ON publication_packages
WHEN NEW.sealed_at IS NOT NULL OR NOT EXISTS (SELECT 1 FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
                 WHERE v.draft_version_id=NEW.draft_version_id AND d.project_id=NEW.project_id)
BEGIN SELECT RAISE(ABORT,'package draft project mismatch'); END;
CREATE TRIGGER post_platform_matches BEFORE INSERT ON posts
WHEN NEW.platform != (SELECT platform FROM publication_packages WHERE package_id=NEW.package_id)
BEGIN SELECT RAISE(ABORT,'post platform mismatch'); END;
-- Identity/lineage links cannot be reassigned after insertion.
CREATE TRIGGER asset_file_lineage_immutable BEFORE UPDATE OF asset_id,parent_file_id ON asset_files
BEGIN SELECT RAISE(ABORT,'asset file lineage immutable'); END;
CREATE TRIGGER processing_asset_immutable BEFORE UPDATE OF asset_id ON processing_runs
BEGIN SELECT RAISE(ABORT,'processing asset immutable'); END;
CREATE TRIGGER post_link_immutable BEFORE UPDATE OF package_id,platform ON posts
BEGIN SELECT RAISE(ABORT,'post package/platform immutable'); END;
