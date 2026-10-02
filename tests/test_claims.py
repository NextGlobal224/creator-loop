"""Check Claim Version citations and support review projections."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.claims import (
    EvidenceLink,
    append_claim_version,
    claim_support_review,
    create_claim,
)
from creator_loop.database import initialize
from creator_loop.evidence_correction import correct_text_evidence
from creator_loop.evidence_review import record_evidence_review
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original


class _NonWindowsOwnedFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = path.open("x+b")
        self.delete_on_close = False

    @classmethod
    def create_new(cls, path: Path) -> _NonWindowsOwnedFile:
        return cls(path)

    def discard(self) -> None:
        self.delete_on_close = True

    def close(self) -> None:
        self.stream.close()
        if self.delete_on_close:
            self.path.unlink()


class ClaimVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu Claim"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.support = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=0,
            end=4,
            actor="creator",
        )
        self.context = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=7,
            end=10,
            actor="creator",
        )

    def _create(self):
        return create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Nguồn nhắc đến Café",
            actor="editor",
            links=[
                EvidenceLink(self.support.evidence_version_id, "SUPPORTS"),
                EvidenceLink(self.context.evidence_version_id, "CONTEXT"),
            ],
        )

    def test_exact_links_and_accepted_support_projection(self) -> None:
        saved = self._create()
        self.assertEqual(saved.version_no, 1)
        links = self.db.execute(
            """SELECT evidence_version_id,relation_type FROM claim_evidence
               WHERE claim_version_id=? ORDER BY relation_type""",
            (saved.claim_version_id,),
        ).fetchall()
        self.assertEqual(
            links,
            [
                (self.context.evidence_version_id, "CONTEXT"),
                (self.support.evidence_version_id, "SUPPORTS"),
            ],
        )
        before = claim_support_review(self.db, saved.claim_version_id)
        self.assertEqual(
            (before.support_links, before.accepted_current_supports), (1, 0)
        )
        record_evidence_review(
            self.db,
            evidence_version_id=self.support.evidence_version_id,
            data_root=self.root,
            action="ACCEPT",
            actor="reviewer",
        )
        after = claim_support_review(self.db, saved.claim_version_id)
        self.assertEqual(
            (after.support_links, after.accepted_current_supports, after.stale_links),
            (1, 1, 0),
        )

    def test_append_version_preserves_old_statement_and_citations(self) -> None:
        first = self._create()
        second = append_claim_version(
            self.db,
            claim_id=first.claim_id,
            statement="Nguồn nhắc đến Huế",
            actor="editor",
            links=[EvidenceLink(self.context.evidence_version_id, "SUPPORTS")],
        )
        self.assertEqual((second.claim_id, second.version_no), (first.claim_id, 2))
        versions = self.db.execute(
            "SELECT version_no,statement FROM claim_versions WHERE claim_id=? ORDER BY version_no",
            (first.claim_id,),
        ).fetchall()
        self.assertEqual(
            versions,
            [(1, "Nguồn nhắc đến Café"), (2, "Nguồn nhắc đến Huế")],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM claim_evidence WHERE claim_version_id=?",
                (first.claim_version_id,),
            ).fetchone()[0],
            2,
        )

    def test_sealed_claim_citations_reject_direct_mutation(self) -> None:
        saved = self._create()
        original = self.db.execute(
            """SELECT evidence_version_id,relation_type FROM claim_evidence
               WHERE claim_version_id=? ORDER BY evidence_version_id""",
            (saved.claim_version_id,),
        ).fetchall()
        statements = (
            (
                """UPDATE claim_evidence SET relation_type='CONTRADICTS'
                   WHERE claim_version_id=? AND relation_type='SUPPORTS'""",
                (saved.claim_version_id,),
            ),
            (
                "DELETE FROM claim_evidence WHERE claim_version_id=?",
                (saved.claim_version_id,),
            ),
            (
                """INSERT INTO claim_evidence(
                     claim_version_id,evidence_version_id,relation_type)
                   VALUES(?,?,'CONTRADICTS')""",
                (saved.claim_version_id, self.support.evidence_version_id),
            ),
            (
                "DELETE FROM claim_version_seals WHERE claim_version_id=?",
                (saved.claim_version_id,),
            ),
        )
        for sql, args in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)
        self.assertEqual(
            self.db.execute(
                """SELECT evidence_version_id,relation_type FROM claim_evidence
                   WHERE claim_version_id=? ORDER BY evidence_version_id""",
                (saved.claim_version_id,),
            ).fetchall(),
            original,
        )

    def test_missing_evidence_rolls_back_claim_and_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "live Evidence Version"):
            create_claim(
                self.db,
                claim_type="FACTUAL",
                statement="Chưa đủ nguồn",
                actor="editor",
                links=[
                    EvidenceLink(self.support.evidence_version_id, "SUPPORTS"),
                    EvidenceLink("missing", "CONTEXT"),
                ],
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claim_evidence").fetchone()[0], 0
        )

    def test_evidence_correction_marks_old_claim_link_stale(self) -> None:
        saved = self._create()
        record_evidence_review(
            self.db,
            evidence_version_id=self.support.evidence_version_id,
            data_root=self.root,
            action="ACCEPT",
            actor="reviewer",
        )
        correct_text_evidence(
            self.db,
            evidence_version_id=self.support.evidence_version_id,
            data_root=self.root,
            start=0,
            end=10,
            actor="editor",
            reason="Thêm ngữ cảnh",
        )
        review = claim_support_review(self.db, saved.claim_version_id)
        self.assertEqual(
            (
                review.support_links,
                review.accepted_current_supports,
                review.stale_links,
            ),
            (1, 0, 1),
        )
        link = self.db.execute(
            """SELECT evidence_version_id FROM claim_evidence
               WHERE claim_version_id=? AND relation_type='SUPPORTS'""",
            (saved.claim_version_id,),
        ).fetchone()[0]
        self.assertEqual(link, self.support.evidence_version_id)

    def test_successful_writers_leave_commit_to_caller(self) -> None:
        self.db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES(?,?,?)",
            ("caller-claim", "INTERPRETIVE", "2026-10-02T00:00:00Z"),
        )
        self.assertTrue(self.db.in_transaction)
        saved = self._create()
        appended = append_claim_version(
            self.db,
            claim_id=saved.claim_id,
            statement="Bản mới",
            actor="editor",
            links=[EvidenceLink(self.support.evidence_version_id, "SUPPORTS")],
        )
        self.assertEqual(appended.version_no, 2)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        for table in (
            "claims",
            "claim_versions",
            "claim_evidence",
            "claim_version_seals",
        ):
            self.assertEqual(
                self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )

    def test_failed_writers_roll_back_only_their_savepoint(self) -> None:
        saved = self._create()
        self.db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES(?,?,?)",
            ("caller-claim", "INTERPRETIVE", "2026-10-02T00:00:00Z"),
        )
        with self.assertRaisesRegex(ValueError, "live Evidence Version"):
            create_claim(
                self.db,
                claim_type="FACTUAL",
                statement="Không đủ",
                actor="editor",
                links=[
                    EvidenceLink(self.support.evidence_version_id, "SUPPORTS"),
                    EvidenceLink("missing", "CONTEXT"),
                ],
            )
        with self.assertRaisesRegex(ValueError, "live Evidence Version"):
            append_claim_version(
                self.db,
                claim_id=saved.claim_id,
                statement="Bản mới",
                actor="editor",
                links=[
                    EvidenceLink(self.support.evidence_version_id, "SUPPORTS"),
                    EvidenceLink("missing", "CONTEXT"),
                ],
            )
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM claims WHERE claim_id='caller-claim'"
            ).fetchone()[0],
            1,
        )
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claims").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claim_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claim_evidence").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM claim_version_seals").fetchone()[0], 1
        )
