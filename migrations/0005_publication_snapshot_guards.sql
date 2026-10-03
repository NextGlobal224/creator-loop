-- SQLite REPLACE may delete a conflicting row without firing DELETE triggers
-- when recursive_triggers is off. Reject identity/version reuse before insertion.
CREATE TRIGGER evidence_identity_no_replace BEFORE INSERT ON evidences
WHEN EXISTS (SELECT 1 FROM evidences WHERE evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'evidence identity immutable'); END;
CREATE TRIGGER evidence_version_no_replace BEFORE INSERT ON evidence_versions
WHEN EXISTS (SELECT 1 FROM evidence_versions WHERE evidence_version_id=NEW.evidence_version_id
  OR (evidence_id=NEW.evidence_id AND version_no=NEW.version_no))
BEGIN SELECT RAISE(ABORT,'evidence version immutable'); END;
CREATE TRIGGER claim_identity_no_replace BEFORE INSERT ON claims
WHEN EXISTS (SELECT 1 FROM claims WHERE claim_id=NEW.claim_id)
BEGIN SELECT RAISE(ABORT,'claim identity immutable'); END;
CREATE TRIGGER claim_type_immutable BEFORE UPDATE OF claim_type ON claims
WHEN NEW.claim_type IS NOT OLD.claim_type
BEGIN SELECT RAISE(ABORT,'claim type immutable'); END;
CREATE TRIGGER claim_version_no_replace BEFORE INSERT ON claim_versions
WHEN EXISTS (SELECT 1 FROM claim_versions WHERE claim_version_id=NEW.claim_version_id
  OR (claim_id=NEW.claim_id AND version_no=NEW.version_no))
BEGIN SELECT RAISE(ABORT,'claim version immutable'); END;
CREATE TRIGGER claim_seal_no_replace BEFORE INSERT ON claim_version_seals
WHEN EXISTS (SELECT 1 FROM claim_version_seals WHERE claim_version_id=NEW.claim_version_id)
BEGIN SELECT RAISE(ABORT,'claim seal immutable'); END;
CREATE TRIGGER draft_seal_no_replace BEFORE INSERT ON draft_version_seals
WHEN EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'draft seal immutable'); END;
CREATE TRIGGER draft_assertion_no_replace BEFORE INSERT ON draft_assertions
WHEN EXISTS (SELECT 1 FROM draft_assertions WHERE assertion_id=NEW.assertion_id)
BEGIN SELECT RAISE(ABORT,'draft assertion immutable'); END;
CREATE TRIGGER review_event_no_replace BEFORE INSERT ON review_events
WHEN EXISTS (SELECT 1 FROM review_events WHERE review_event_id=NEW.review_event_id)
BEGIN SELECT RAISE(ABORT,'review event immutable'); END;
CREATE TRIGGER package_no_replace BEFORE INSERT ON publication_packages
WHEN EXISTS (SELECT 1 FROM publication_packages WHERE package_id=NEW.package_id)
BEGIN SELECT RAISE(ABORT,'package immutable'); END;
CREATE TRIGGER package_item_no_replace BEFORE INSERT ON publication_package_items
WHEN EXISTS (SELECT 1 FROM publication_package_items WHERE package_item_id=NEW.package_item_id
  OR (package_id=NEW.package_id AND item_type=NEW.item_type AND position=NEW.position))
BEGIN SELECT RAISE(ABORT,'package item immutable'); END;
CREATE TRIGGER approval_no_replace BEFORE INSERT ON approvals
WHEN EXISTS (SELECT 1 FROM approvals WHERE approval_id=NEW.approval_id)
BEGIN SELECT RAISE(ABORT,'approval immutable'); END;
CREATE TRIGGER post_no_replace BEFORE INSERT ON posts
WHEN EXISTS (SELECT 1 FROM posts WHERE post_id=NEW.post_id
  OR (external_post_id IS NOT NULL AND platform=NEW.platform AND external_post_id=NEW.external_post_id))
BEGIN SELECT RAISE(ABORT,'post identity immutable'); END;
CREATE TRIGGER observation_no_replace BEFORE INSERT ON observations
WHEN EXISTS (SELECT 1 FROM observations WHERE observation_id=NEW.observation_id)
BEGIN SELECT RAISE(ABORT,'observation immutable'); END;
CREATE TRIGGER observation_metric_no_replace BEFORE INSERT ON observation_metrics
WHEN EXISTS (SELECT 1 FROM observation_metrics WHERE observation_id=NEW.observation_id
  AND metric_key=NEW.metric_key AND metric_scope=NEW.metric_scope)
BEGIN SELECT RAISE(ABORT,'observation metric immutable'); END;
CREATE TRIGGER asset_identity_no_replace BEFORE INSERT ON assets
WHEN EXISTS (SELECT 1 FROM assets WHERE asset_id=NEW.asset_id)
BEGIN SELECT RAISE(ABORT,'asset identity immutable'); END;
CREATE TRIGGER asset_file_no_replace BEFORE INSERT ON asset_files
WHEN EXISTS (SELECT 1 FROM asset_files WHERE file_id=NEW.file_id)
BEGIN SELECT RAISE(ABORT,'asset file identity immutable'); END;
