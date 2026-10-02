-- A Claim Version's citation set is completed before the version is exposed.
CREATE TABLE claim_version_seals (
  claim_version_id TEXT PRIMARY KEY REFERENCES claim_versions(claim_version_id),
  sealed_at TEXT NOT NULL
);

-- Existing v1 versions are historical snapshots, including any with no citations.
-- Seal them before installing the new-version minimum-citation guard.
INSERT INTO claim_version_seals(claim_version_id, sealed_at)
SELECT claim_version_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM claim_versions;

CREATE TRIGGER claim_version_seal_requires_citation
BEFORE INSERT ON claim_version_seals
WHEN NOT EXISTS (
  SELECT 1 FROM claim_evidence
  WHERE claim_version_id = NEW.claim_version_id
)
BEGIN SELECT RAISE(ABORT, 'claim version needs a citation'); END;

CREATE TRIGGER claim_evidence_no_insert_after_seal
BEFORE INSERT ON claim_evidence
WHEN EXISTS (
  SELECT 1 FROM claim_version_seals
  WHERE claim_version_id = NEW.claim_version_id
)
BEGIN SELECT RAISE(ABORT, 'claim citations sealed'); END;

CREATE TRIGGER claim_evidence_no_update
BEFORE UPDATE ON claim_evidence
BEGIN SELECT RAISE(ABORT, 'claim citation immutable'); END;

CREATE TRIGGER claim_evidence_no_delete
BEFORE DELETE ON claim_evidence
BEGIN SELECT RAISE(ABORT, 'claim citation immutable'); END;

CREATE TRIGGER claim_version_seal_no_update
BEFORE UPDATE ON claim_version_seals
BEGIN SELECT RAISE(ABORT, 'claim version seal immutable'); END;

CREATE TRIGGER claim_version_seal_no_delete
BEFORE DELETE ON claim_version_seals
BEGIN SELECT RAISE(ABORT, 'claim version seal immutable'); END;
