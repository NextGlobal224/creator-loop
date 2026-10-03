"""Exercise exact A/B/C decisions, immutable history and transaction ownership."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.drafts import append_draft_version, create_draft
from creator_loop.projects import archive_project, create_project
from creator_loop.selections import list_selections, select_draft


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Dữ liệu Selection" / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.project = create_project(self.db, title="Huế").project_id
        self.versions = tuple(
            create_draft(
                self.db,
                project_id=self.project,
                body_text=body,
                format="POST",
                actor="author",
            ).draft_version_id
            for body in ("A — Huế", "B — Đà Nẵng", "C — Hội An")
        )

    def _choose(self, **kwargs):
        values = dict(
            project_id=self.project,
            candidate_version_ids=self.versions,
            selected_draft_version_id=self.versions[1],
            actor="editor",
            reason="B hợp góc kể đã chọn 🏞",
        )
        values.update(kwargs)
        return select_draft(self.db, **values)

    def _counts(self):
        return tuple(
            self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in (
                "selection_events",
                "selection_candidates",
                "selection_event_seals",
            )
        )

    def test_a_b_c_reopen_preserves_actor_reason_and_versions_after_correction(self):
        first = self._choose()
        corrected = append_draft_version(
            self.db,
            draft_version_id=self.versions[1],
            body_text="B sửa mới",
            format="POST",
            actor="editor",
        )
        second = self._choose(
            candidate_version_ids=(self.versions[0], corrected.draft_version_id),
            selected_draft_version_id=corrected.draft_version_id,
            reason="Chọn bản sửa",
        )
        self.assertNotEqual(first.candidate_set_id, second.candidate_set_id)
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(
                list_selections(reopened, project_id=self.project), [first, second]
            )
            self.assertEqual(
                reopened.execute(
                    "SELECT body_text FROM draft_versions WHERE draft_version_id=?",
                    (first.selected_draft_version_id,),
                ).fetchone()[0],
                "B — Đà Nẵng",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
        )

    def test_empty_duplicates_outside_set_or_actor_reject_without_partial_writes(self):
        before = self._counts()
        for values in (
            {"candidate_version_ids": ()},
            {"candidate_version_ids": (self.versions[0], self.versions[0])},
            {"candidate_version_ids": self.versions[:1]},
            {"actor": " \t"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self._choose(**values)
            self.assertEqual(self._counts(), before)

    def test_foreign_missing_unsealed_and_archived_candidates_reject(self):
        other = create_project(self.db, title="Other").project_id
        foreign = create_draft(
            self.db,
            project_id=other,
            body_text="foreign",
            format="POST",
            actor="editor",
        ).draft_version_id
        self.db.execute(
            "INSERT INTO draft_versions VALUES('raw',?,2,'unsealed','POST','editor','now')",
            (
                self.db.execute(
                    "SELECT draft_id FROM draft_versions WHERE draft_version_id=?",
                    (self.versions[0],),
                ).fetchone()[0],
            ),
        )
        self.db.commit()
        for invalid in (foreign, "missing", "raw"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self._choose(candidate_version_ids=(self.versions[1], invalid))
            self.assertEqual(self._counts(), (0, 0, 0))
        self.db.execute(
            "UPDATE drafts SET status='ARCHIVED' WHERE draft_id=(SELECT draft_id FROM draft_versions WHERE draft_version_id=?)",
            (self.versions[0],),
        )
        self.db.commit()
        with self.assertRaises(ValueError):
            self._choose()

    def test_archived_project_history_readable_but_no_new_decisions(self):
        decision = self._choose(reason=None)
        self.assertIsNone(decision.reason_text)
        archive_project(self.db, project_id=self.project)
        with self.assertRaises(ValueError):
            self._choose()
        self.assertEqual(list_selections(self.db, project_id=self.project), [decision])

    def test_completed_history_and_membership_cannot_be_rewritten_by_sql(self):
        decision = self._choose()
        for sql in (
            "UPDATE selection_events SET actor_id='rewrite'",
            "DELETE FROM selection_events",
            "UPDATE selection_candidates SET draft_version_id=draft_version_id",
            "DELETE FROM selection_candidates",
            "UPDATE selection_event_seals SET sealed_at='rewrite'",
            "DELETE FROM selection_event_seals",
            "INSERT OR REPLACE INTO selection_events SELECT selection_event_id,project_id,selected_draft_version_id,candidate_set_id,'rewrite',actor_id,created_at FROM selection_events",
            "INSERT OR REPLACE INTO selection_event_seals SELECT selection_event_id,'rewrite' FROM selection_event_seals",
            "INSERT OR REPLACE INTO drafts SELECT draft_id,project_id,'ARCHIVED',created_at FROM drafts",
            "INSERT OR REPLACE INTO draft_versions SELECT draft_version_id,draft_id,version_no,'rewrite',format,created_by,created_at FROM draft_versions",
            "INSERT OR REPLACE INTO draft_versions SELECT 'replacement',draft_id,version_no,'rewrite',format,created_by,created_at FROM draft_versions WHERE version_no=1 LIMIT 1",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
            self.db.rollback()
        extra = create_draft(
            self.db,
            project_id=self.project,
            body_text="D",
            format="POST",
            actor="editor",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "sealed"):
            self.db.execute(
                "INSERT INTO selection_candidates VALUES(?,?)",
                (decision.selection_event_id, extra.draft_version_id),
            )
        self.db.rollback()
        self.assertEqual(list_selections(self.db, project_id=self.project), [decision])

    def test_incomplete_direct_sql_is_not_exposed_and_cannot_be_sealed(self):
        self.db.execute(
            "INSERT INTO selection_events VALUES('raw',?,?,'set',NULL,'editor','now')",
            (self.project, self.versions[0]),
        )
        self.db.execute(
            "INSERT INTO selection_candidates VALUES('raw',?)", (self.versions[1],)
        )
        self.db.commit()
        self.assertEqual(list_selections(self.db, project_id=self.project), [])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid selection"):
            self.db.execute("INSERT INTO selection_event_seals VALUES('raw','now')")
        self.db.rollback()

    def test_caller_transaction_retains_control_of_decision(self):
        self.db.execute("BEGIN")
        self.db.execute(
            "UPDATE projects SET title='Caller change' WHERE project_id=?",
            (self.project,),
        )
        self._choose()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self._counts(), (0, 0, 0))
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM projects WHERE project_id=?", (self.project,)
            ).fetchone()[0],
            "Huế",
        )

    def test_seal_failure_rolls_back_decision_only_and_preserves_caller_writes(self):
        self.db.execute(
            "CREATE TRIGGER injected_seal_failure BEFORE INSERT ON selection_event_seals BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        self.db.execute(
            "UPDATE projects SET title='Caller change' WHERE project_id=?",
            (self.project,),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self._choose()
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self._counts(), (0, 0, 0))
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM projects WHERE project_id=?", (self.project,)
            ).fetchone()[0],
            "Caller change",
        )
        self.db.commit()

    def test_sql_seal_rejects_foreign_project_even_when_selected_is_a_member(self):
        other = create_project(self.db, title="Other").project_id
        self.db.execute(
            "INSERT INTO selection_events VALUES('foreign',?,?,'set',NULL,'editor','now')",
            (other, self.versions[0]),
        )
        self.db.execute(
            "INSERT INTO selection_candidates VALUES('foreign',?)", (self.versions[0],)
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid selection"):
            self.db.execute("INSERT INTO selection_event_seals VALUES('foreign','now')")
        self.db.rollback()


if __name__ == "__main__":
    unittest.main()
