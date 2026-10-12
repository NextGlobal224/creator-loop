CREATE TABLE facebook_searches (
 search_id TEXT PRIMARY KEY,
 created_at TEXT NOT NULL,
 criteria_json TEXT NOT NULL CHECK(json_valid(criteria_json)),
 queries_json TEXT NOT NULL CHECK(json_valid(queries_json))
);
CREATE TABLE facebook_candidates (
 candidate_id TEXT PRIMARY KEY,
 search_id TEXT NOT NULL REFERENCES facebook_searches(search_id),
 external_id TEXT NOT NULL,
 canonical_url TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
 UNIQUE(search_id,external_id)
);
CREATE TABLE facebook_selection_events (
 event_id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL REFERENCES facebook_candidates(candidate_id),
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 created_at TEXT NOT NULL
);
CREATE TRIGGER facebook_selection_source BEFORE INSERT ON facebook_selection_events
WHEN NOT EXISTS(SELECT 1 FROM facebook_candidates c JOIN sources s
 ON s.external_id=c.external_id AND s.platform='FB'
 WHERE c.candidate_id=NEW.candidate_id AND s.source_id=NEW.source_id)
BEGIN SELECT RAISE(ABORT,'selected source must match observed candidate'); END;
CREATE TRIGGER facebook_search_no_update BEFORE UPDATE ON facebook_searches BEGIN SELECT RAISE(ABORT,'search is immutable'); END;
CREATE TRIGGER facebook_search_no_delete BEFORE DELETE ON facebook_searches BEGIN SELECT RAISE(ABORT,'search is immutable'); END;
CREATE TRIGGER facebook_search_no_replace BEFORE INSERT ON facebook_searches WHEN EXISTS(SELECT 1 FROM facebook_searches WHERE search_id=NEW.search_id) BEGIN SELECT RAISE(ABORT,'search is immutable'); END;
CREATE TRIGGER facebook_candidate_no_update BEFORE UPDATE ON facebook_candidates BEGIN SELECT RAISE(ABORT,'candidate is immutable'); END;
CREATE TRIGGER facebook_candidate_no_delete BEFORE DELETE ON facebook_candidates BEGIN SELECT RAISE(ABORT,'candidate is immutable'); END;
CREATE TRIGGER facebook_candidate_no_replace BEFORE INSERT ON facebook_candidates WHEN EXISTS(SELECT 1 FROM facebook_candidates WHERE candidate_id=NEW.candidate_id OR (search_id=NEW.search_id AND external_id=NEW.external_id)) BEGIN SELECT RAISE(ABORT,'candidate is immutable'); END;
CREATE TRIGGER facebook_selection_no_update BEFORE UPDATE ON facebook_selection_events BEGIN SELECT RAISE(ABORT,'selection is immutable'); END;
CREATE TRIGGER facebook_selection_no_delete BEFORE DELETE ON facebook_selection_events BEGIN SELECT RAISE(ABORT,'selection is immutable'); END;
CREATE TRIGGER facebook_selection_no_replace BEFORE INSERT ON facebook_selection_events WHEN EXISTS(SELECT 1 FROM facebook_selection_events WHERE event_id=NEW.event_id) BEGIN SELECT RAISE(ABORT,'selection is immutable'); END;
