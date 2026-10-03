-- Event + candidate membership is a completed immutable decision only after seal.
CREATE TABLE selection_event_seals (
  selection_event_id TEXT PRIMARY KEY REFERENCES selection_events(selection_event_id),
  sealed_at TEXT NOT NULL
);

CREATE TRIGGER selection_seal_validate_snapshot
BEFORE INSERT ON selection_event_seals
WHEN EXISTS (
  SELECT 1 FROM selection_events e WHERE e.selection_event_id=NEW.selection_event_id AND (
    length(trim(e.actor_id))=0 OR length(trim(e.candidate_set_id))=0 OR
    NOT EXISTS (
      SELECT 1 FROM selection_candidates c WHERE c.selection_event_id=e.selection_event_id
      AND c.draft_version_id=e.selected_draft_version_id
    ) OR EXISTS (
      SELECT 1 FROM selection_candidates c
      JOIN draft_versions v ON v.draft_version_id=c.draft_version_id
      JOIN drafts d ON d.draft_id=v.draft_id
      WHERE c.selection_event_id=e.selection_event_id AND (
        d.project_id!=e.project_id OR NOT EXISTS (
          SELECT 1 FROM draft_version_seals s WHERE s.draft_version_id=c.draft_version_id
        )
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'invalid selection snapshot'); END;

-- Invalid legacy decisions abort migration; do not silently repair history.
INSERT INTO selection_event_seals(selection_event_id,sealed_at)
SELECT selection_event_id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM selection_events;

-- SQLite REPLACE can delete without firing DELETE triggers when recursive
-- triggers are off. Reject identity reuse before INSERT, including references.
CREATE TRIGGER selection_seal_no_replace BEFORE INSERT ON selection_event_seals
WHEN EXISTS (SELECT 1 FROM selection_event_seals WHERE selection_event_id=NEW.selection_event_id)
BEGIN SELECT RAISE(ABORT,'selection seal immutable'); END;
CREATE TRIGGER selection_event_no_replace BEFORE INSERT ON selection_events
WHEN EXISTS (SELECT 1 FROM selection_events WHERE selection_event_id=NEW.selection_event_id)
BEGIN SELECT RAISE(ABORT,'selection event immutable'); END;
CREATE TRIGGER draft_identity_no_replace BEFORE INSERT ON drafts
WHEN EXISTS (SELECT 1 FROM drafts WHERE draft_id=NEW.draft_id)
BEGIN SELECT RAISE(ABORT,'draft identity immutable'); END;
CREATE TRIGGER draft_version_no_replace BEFORE INSERT ON draft_versions
WHEN EXISTS (SELECT 1 FROM draft_versions WHERE draft_version_id=NEW.draft_version_id)
   OR EXISTS (SELECT 1 FROM draft_versions WHERE draft_id=NEW.draft_id AND version_no=NEW.version_no)
BEGIN SELECT RAISE(ABORT,'draft version immutable'); END;

CREATE TRIGGER selection_seal_no_update BEFORE UPDATE ON selection_event_seals
BEGIN SELECT RAISE(ABORT,'selection seal immutable'); END;
CREATE TRIGGER selection_seal_no_delete BEFORE DELETE ON selection_event_seals
BEGIN SELECT RAISE(ABORT,'selection seal immutable'); END;
CREATE TRIGGER selection_event_no_update BEFORE UPDATE ON selection_events
BEGIN SELECT RAISE(ABORT,'selection event immutable'); END;
CREATE TRIGGER selection_event_no_delete BEFORE DELETE ON selection_events
BEGIN SELECT RAISE(ABORT,'selection event immutable'); END;
CREATE TRIGGER selection_candidate_no_insert_after_seal BEFORE INSERT ON selection_candidates
WHEN EXISTS (SELECT 1 FROM selection_event_seals WHERE selection_event_id=NEW.selection_event_id)
BEGIN SELECT RAISE(ABORT,'selection candidates sealed'); END;
CREATE TRIGGER selection_candidate_no_update BEFORE UPDATE ON selection_candidates
BEGIN SELECT RAISE(ABORT,'selection candidate immutable'); END;
CREATE TRIGGER selection_candidate_no_delete BEFORE DELETE ON selection_candidates
BEGIN SELECT RAISE(ABORT,'selection candidate immutable'); END;
