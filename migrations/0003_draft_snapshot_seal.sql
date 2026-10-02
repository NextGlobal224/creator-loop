-- A Draft Version owns one immutable parent/citation/assertion snapshot.
CREATE TABLE draft_version_seals (
  draft_version_id TEXT PRIMARY KEY REFERENCES draft_versions(draft_version_id),
  sealed_at TEXT NOT NULL
);

CREATE TRIGGER draft_seal_validate_snapshot
BEFORE INSERT ON draft_version_seals
WHEN EXISTS (
  SELECT 1 FROM draft_assertions a JOIN draft_versions v ON v.draft_version_id=a.draft_version_id
  WHERE a.draft_version_id=NEW.draft_version_id AND (
    typeof(a.text_start)!='integer' OR typeof(a.text_end)!='integer' OR
    a.text_end>length(v.body_text) OR
    a.asserted_text!=substr(v.body_text,a.text_start+1,a.text_end-a.text_start) OR
    (a.review_state='SUPPORTED' AND a.claim_version_id IS NULL) OR
    (a.claim_version_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM draft_claims c WHERE c.draft_version_id=a.draft_version_id
      AND c.claim_version_id=a.claim_version_id
    ))
  )
) OR EXISTS (
  SELECT 1 FROM draft_claims c WHERE c.draft_version_id=NEW.draft_version_id
  AND NOT EXISTS (SELECT 1 FROM claim_version_seals s WHERE s.claim_version_id=c.claim_version_id)
) OR EXISTS (
  SELECT 1 FROM draft_version_parents p
  JOIN draft_versions cv ON cv.draft_version_id=p.child_draft_version_id
  JOIN drafts cd ON cd.draft_id=cv.draft_id
  JOIN draft_versions pv ON pv.draft_version_id=p.parent_draft_version_id
  JOIN drafts pd ON pd.draft_id=pv.draft_id
  WHERE p.child_draft_version_id=NEW.draft_version_id AND cd.project_id!=pd.project_id
) OR EXISTS (
  WITH RECURSIVE ancestors(version_id) AS (
    SELECT parent_draft_version_id FROM draft_version_parents WHERE child_draft_version_id=NEW.draft_version_id
    UNION
    SELECT p.parent_draft_version_id FROM draft_version_parents p JOIN ancestors a ON p.child_draft_version_id=a.version_id
  ) SELECT 1 FROM ancestors WHERE version_id=NEW.draft_version_id
)
BEGIN SELECT RAISE(ABORT,'invalid draft snapshot'); END;

-- Validate and preserve historical snapshots; reject corrupt legacy graphs.
INSERT INTO draft_version_seals(draft_version_id,sealed_at)
SELECT draft_version_id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM draft_versions;

CREATE TRIGGER draft_seal_requires_sealed_parents
BEFORE INSERT ON draft_version_seals
WHEN EXISTS (
  SELECT 1 FROM draft_version_parents p WHERE p.child_draft_version_id=NEW.draft_version_id
  AND NOT EXISTS (SELECT 1 FROM draft_version_seals s WHERE s.draft_version_id=p.parent_draft_version_id)
)
BEGIN SELECT RAISE(ABORT,'draft parent must be sealed'); END;

CREATE TRIGGER draft_seal_no_update BEFORE UPDATE ON draft_version_seals
BEGIN SELECT RAISE(ABORT,'draft seal immutable'); END;
CREATE TRIGGER draft_seal_no_delete BEFORE DELETE ON draft_version_seals
BEGIN SELECT RAISE(ABORT,'draft seal immutable'); END;

CREATE TRIGGER draft_parent_no_insert_after_seal BEFORE INSERT ON draft_version_parents
WHEN EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.child_draft_version_id)
BEGIN SELECT RAISE(ABORT,'draft parents sealed'); END;
CREATE TRIGGER draft_parent_no_update BEFORE UPDATE ON draft_version_parents
BEGIN SELECT RAISE(ABORT,'draft parent immutable'); END;
CREATE TRIGGER draft_parent_no_delete BEFORE DELETE ON draft_version_parents
BEGIN SELECT RAISE(ABORT,'draft parent immutable'); END;

CREATE TRIGGER draft_claim_no_insert_after_seal BEFORE INSERT ON draft_claims
WHEN EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'draft citations sealed'); END;
CREATE TRIGGER draft_claim_no_update BEFORE UPDATE ON draft_claims
BEGIN SELECT RAISE(ABORT,'draft citation immutable'); END;
CREATE TRIGGER draft_claim_no_delete BEFORE DELETE ON draft_claims
BEGIN SELECT RAISE(ABORT,'draft citation immutable'); END;

CREATE TRIGGER draft_assertion_no_insert_after_seal BEFORE INSERT ON draft_assertions
WHEN EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'draft assertions sealed'); END;
CREATE TRIGGER draft_assertion_no_update BEFORE UPDATE ON draft_assertions
BEGIN SELECT RAISE(ABORT,'draft assertion immutable'); END;
CREATE TRIGGER draft_assertion_no_delete BEFORE DELETE ON draft_assertions
BEGIN SELECT RAISE(ABORT,'draft assertion immutable'); END;

CREATE TRIGGER package_requires_sealed_draft BEFORE INSERT ON publication_packages
WHEN NOT EXISTS (SELECT 1 FROM draft_version_seals WHERE draft_version_id=NEW.draft_version_id)
BEGIN SELECT RAISE(ABORT,'package needs sealed draft'); END;
