"""Check complete Draft snapshots, exact assertions and immutable parent graphs."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.claims import EvidenceLink, create_claim
from creator_loop.database import initialize
from creator_loop.drafts import (
    DraftAssertion,
    DraftClaimLink,
    DraftParent,
    append_draft_version,
    combine_drafts,
    create_draft,
)
from creator_loop.projects import create_project


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Dữ liệu Draft" / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.project = create_project(self.db, title="Huế").project_id
        # Domain fixture for exact Claim provenance; no media decode is asserted.
        self.db.execute("INSERT INTO assets VALUES('a','TEXT','Source','now',NULL)")
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES('f','a','ORIGINAL','storage/originals/f.txt',?,4,'text/plain','now')",
            ("0" * 64,),
        )
        self.db.execute(
            "INSERT INTO evidences VALUES('e','a','DIRECT_TEXT','now',NULL)"
        )
        self.db.execute(
            "INSERT INTO evidence_versions VALUES('ev','e','a',1,'f','Café','TEXT_RANGE','{\"start\":0,\"end\":4}','HUMAN',NULL,'creator','now')"
        )
        self.db.commit()
        self.claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Có Café",
            actor="editor",
            links=[EvidenceLink("ev", "SUPPORTS")],
        )
        self.links = [DraftClaimLink(self.claim.claim_version_id, "ASSERTED")]

    def _create(self, **kwargs):
        values = dict(
            project_id=self.project,
            body_text="Café 🏞 ở Huế",
            format="POST",
            actor="editor",
            claims=self.links,
            assertions=[
                DraftAssertion(0, 4, self.claim.claim_version_id, "UNREVIEWED"),
                DraftAssertion(5, 6, None, "EDITORIAL"),
            ],
        )
        values.update(kwargs)
        return create_draft(self.db, **values)

    def _counts(self):
        return tuple(
            self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "drafts",
                "draft_versions",
                "draft_version_parents",
                "draft_claims",
                "draft_assertions",
                "draft_version_seals",
            )
        )

    def test_unicode_snapshot_append_and_combination_preserve_exact_history(self):
        a = self._create()
        b = self._create(body_text="Café 🏞 ở Đà Nẵng")
        old = {
            table: self.db.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "draft_versions",
                "draft_claims",
                "draft_assertions",
                "draft_version_seals",
            )
        }
        combined = combine_drafts(
            self.db,
            project_id=self.project,
            parent_version_ids=[a.draft_version_id, b.draft_version_id],
            body_text="Café 🏞 ở hai nơi",
            format="POST",
            actor="editor",
            claims=self.links,
            assertions=[
                DraftAssertion(0, 4, self.claim.claim_version_id, "SUPPORTED"),
                DraftAssertion(5, 6, None, "EDITORIAL"),
                DraftAssertion(7, 9, None, "NEEDS_SOURCE"),
            ],
        )
        revised = append_draft_version(
            self.db,
            draft_version_id=combined.draft_version_id,
            body_text="Café 🏞 — một bản sửa",
            format="POST",
            actor="editor",
            claims=self.links,
            assertions=[
                DraftAssertion(0, 4, self.claim.claim_version_id, "UNREVIEWED")
            ],
        )
        self.assertEqual((revised.draft_id, revised.version_no), (combined.draft_id, 2))
        parents = self.db.execute(
            "SELECT parent_draft_version_id,relation_type FROM draft_version_parents WHERE child_draft_version_id=?",
            (combined.draft_version_id,),
        ).fetchall()
        self.assertEqual(
            set(parents),
            {(a.draft_version_id, "COMBINE"), (b.draft_version_id, "COMBINE")},
        )
        self.assertEqual(
            self.db.execute(
                "SELECT parent_draft_version_id,relation_type FROM draft_version_parents WHERE child_draft_version_id=?",
                (revised.draft_version_id,),
            ).fetchall(),
            [(combined.draft_version_id, "EDIT")],
        )
        spans = self.db.execute(
            "SELECT text_start,text_end,asserted_text,review_state FROM draft_assertions WHERE draft_version_id=? ORDER BY text_start",
            (combined.draft_version_id,),
        ).fetchall()
        self.assertEqual(
            spans,
            [
                (0, 4, "Café", "SUPPORTED"),
                (5, 6, "🏞", "EDITORIAL"),
                (7, 9, "ở ", "NEEDS_SOURCE"),
            ],
        )
        for table, rows in old.items():
            self.assertTrue(
                all(
                    row in self.db.execute(f"SELECT * FROM {table}").fetchall()
                    for row in rows
                )
            )
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_noop_does_not_append_but_changed_citation_is_a_new_snapshot(self):
        saved = self._create()
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "No Draft snapshot changes"):
            append_draft_version(
                self.db,
                draft_version_id=saved.draft_version_id,
                body_text="Café 🏞 ở Huế",
                format="POST",
                actor="editor",
                claims=self.links,
                assertions=[
                    DraftAssertion(0, 4, self.claim.claim_version_id, "UNREVIEWED"),
                    DraftAssertion(5, 6, None, "EDITORIAL"),
                ],
            )
        self.assertEqual(self._counts(), before)
        changed = append_draft_version(
            self.db,
            draft_version_id=saved.draft_version_id,
            body_text="Café 🏞 ở Huế",
            format="POST",
            actor="editor",
            claims=[DraftClaimLink(self.claim.claim_version_id, "QUOTE")],
            assertions=[
                DraftAssertion(0, 4, self.claim.claim_version_id, "UNREVIEWED"),
                DraftAssertion(5, 6, None, "EDITORIAL"),
            ],
        )
        self.assertEqual(changed.version_no, 2)
        with self.assertRaisesRegex(ValueError, "Latest active sealed"):
            append_draft_version(
                self.db,
                draft_version_id=saved.draft_version_id,
                body_text="Stale edit",
                format="POST",
                actor="editor",
            )

    def test_invalid_offsets_state_and_claim_membership_never_create_a_draft(self):
        for assertion in (
            DraftAssertion(-1, 2),
            DraftAssertion(0, 100),
            DraftAssertion(2, 2),
            DraftAssertion(True, 2),
            DraftAssertion(0, 2, None, "SUPPORTED"),
            DraftAssertion(0, 2, "missing", "UNREVIEWED"),
            DraftAssertion(0, 2, None, "INVALID"),
        ):
            with self.subTest(assertion=assertion), self.assertRaises(ValueError):
                self._create(assertions=[assertion])
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_unsealed_missing_deleted_claim_and_duplicate_links_are_rejected(self):
        for links in ([DraftClaimLink("missing", "ASSERTED")], self.links + self.links):
            with self.assertRaises(ValueError):
                self._create(claims=links, assertions=[])
        self.db.execute(
            "INSERT INTO claim_versions VALUES('building',?,2,'new','editor','now')",
            (self.claim.claim_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live sealed"):
            self._create(claims=[DraftClaimLink("building", "ASSERTED")], assertions=[])
        self.db.execute("UPDATE claims SET deleted_at='now'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live sealed"):
            self._create()
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_archived_project_and_draft_block_mutations(self):
        saved = self._create()
        self.db.execute("UPDATE drafts SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Latest active sealed"):
            append_draft_version(
                self.db,
                draft_version_id=saved.draft_version_id,
                body_text="new",
                format="POST",
                actor="editor",
            )
        self.db.execute("UPDATE projects SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Active live Project"):
            self._create()

    def test_parent_versions_must_be_sealed_distinct_and_in_same_project(self):
        saved = self._create()
        another = create_project(self.db, title="Khác").project_id
        foreign = self._create(project_id=another)
        self.db.execute(
            "INSERT INTO draft_versions VALUES('building',?,2,'new','POST','editor','now')",
            (saved.draft_id,),
        )
        self.db.commit()
        before = self._counts()
        for parents in (
            [DraftParent("missing", "COMBINE")],
            [DraftParent("building", "EDIT")],
            [DraftParent(foreign.draft_version_id, "COMBINE")],
            [DraftParent(saved.draft_version_id, "EDIT")] * 2,
        ):
            with self.assertRaises(ValueError):
                self._create(parents=parents)
        for candidates in ([saved.draft_version_id], [saved.draft_version_id] * 2):
            with self.assertRaisesRegex(ValueError, "two distinct"):
                combine_drafts(
                    self.db,
                    project_id=self.project,
                    parent_version_ids=candidates,
                    body_text="new",
                    format="POST",
                    actor="editor",
                )
        self.assertEqual(self._counts(), before)

    def test_seal_failure_rolls_back_all_snapshot_rows_and_preserves_caller(self):
        self.db.execute(
            "CREATE TRIGGER fail_draft_seal BEFORE INSERT ON draft_version_seals BEGIN SELECT RAISE(ABORT,'seal failed'); END"
        )
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "seal failed"):
            self._create()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))
        self.db.execute(
            "INSERT INTO projects VALUES('caller','Pending','ACTIVE','now',NULL)"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "seal failed"):
            self._create()
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM projects WHERE project_id='caller'"
            ).fetchone()
        )

    def test_successful_snapshot_leaves_transaction_to_caller(self):
        self.db.execute("BEGIN")
        saved = self._create()
        self.assertTrue(self.db.in_transaction)
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM draft_version_seals WHERE draft_version_id=?",
                (saved.draft_version_id,),
            ).fetchone()
        )
        self.db.rollback()
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_sealed_associations_and_seal_reject_direct_mutation(self):
        saved = self._create()
        second = self._create(parents=[DraftParent(saved.draft_version_id, "COMBINE")])
        statements = (
            ("UPDATE draft_claims SET use_type='QUOTE'", ()),
            ("DELETE FROM draft_claims", ()),
            (
                "INSERT INTO draft_claims VALUES(?,?,'QUOTE')",
                (second.draft_version_id, self.claim.claim_version_id),
            ),
            ("UPDATE draft_assertions SET review_state='EDITORIAL'", ()),
            ("DELETE FROM draft_assertions", ()),
            (
                "INSERT INTO draft_assertions VALUES('new',?,0,1,'C',NULL,'EDITORIAL')",
                (second.draft_version_id,),
            ),
            ("UPDATE draft_version_parents SET relation_type='EDIT'", ()),
            ("DELETE FROM draft_version_parents", ()),
            (
                "INSERT INTO draft_version_parents VALUES(?,?,'EDIT')",
                (saved.draft_version_id, second.draft_version_id),
            ),
            ("UPDATE draft_version_seals SET sealed_at='later'", ()),
            ("DELETE FROM draft_version_seals", ()),
        )
        before = self._counts()
        for sql, args in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)
            self.db.rollback()
        self.assertEqual(self._counts(), before)

    def test_unsealed_invalid_assertion_and_parent_cycle_cannot_seal_or_package(self):
        saved = self._create()
        self.db.execute(
            "INSERT INTO draft_versions VALUES('bad',?,2,'abc','POST','editor','now')",
            (saved.draft_id,),
        )
        self.db.execute(
            "INSERT INTO draft_assertions VALUES('bad-span','bad',0,4,'abcd',NULL,'NEEDS_SOURCE')"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid draft snapshot"):
            self.db.execute("INSERT INTO draft_version_seals VALUES('bad','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO publication_packages VALUES('package',?,'bad','LOCAL','POST','fingerprint',1,NULL,'now')",
                (self.project,),
            )
        self.db.rollback()
        for number in (2, 3):
            self.db.execute(
                "INSERT INTO draft_versions VALUES(?,?,?,'abc','POST','editor','now')",
                (f"cycle{number}", saved.draft_id, number),
            )
        self.db.execute(
            "INSERT INTO draft_version_parents VALUES('cycle2','cycle3','EDIT')"
        )
        self.db.execute(
            "INSERT INTO draft_version_parents VALUES('cycle3','cycle2','EDIT')"
        )
        # Both the sealed-parent guard and the recursive cycle guard reject
        # this graph; SQLite does not promise an order between these triggers.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO draft_version_seals VALUES('cycle2','now')")
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM draft_version_seals WHERE draft_version_id IN ('cycle2','cycle3')"
            ).fetchone()[0],
            0,
        )
        self.db.rollback()
