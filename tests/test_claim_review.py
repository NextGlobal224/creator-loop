"""Exercise Claim review history, exact citations and atomic correction on SQLite."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.claim_review import (
    correct_claim,
    current_claim_review,
    record_claim_review,
)
from creator_loop.claims import EvidenceLink, claim_support_review, create_claim
from creator_loop.database import initialize
from creator_loop.evidence_correction import correct_text_evidence
from creator_loop.evidence_reopen import EvidenceReopenError
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original
from test_claims import _NonWindowsOwnedFile


class ClaimReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Dữ liệu Claim review"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.evidence = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=0,
            end=4,
            actor="creator",
        )
        self.links = [EvidenceLink(self.evidence.evidence_version_id, "SUPPORTS")]
        self.claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Có Café",
            actor="editor",
            links=self.links,
        )

    def _review(self, action="ACCEPT", *, reason="", actor="reviewer", version=None):
        return record_claim_review(
            self.db,
            claim_version_id=version or self.claim.claim_version_id,
            data_root=self.root,
            action=action,
            actor=actor,
            reason=reason,
        )

    def _correct(self, *, links=None, version=None):
        return correct_claim(
            self.db,
            claim_version_id=version or self.claim.claim_version_id,
            statement="Nhận định đã sửa",
            actor=" editor ",
            reason=" Thêm ngữ cảnh ",
            links=self.links if links is None else links,
        )

    def _counts(self):
        return tuple(
            self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "claim_versions",
                "claim_evidence",
                "claim_version_seals",
                "review_events",
            )
        )

    def test_actions_have_exact_target_and_append_only_history(self) -> None:
        self.assertEqual(
            current_claim_review(self.db, self.claim.claim_version_id), "PENDING"
        )
        for action in ("ACCEPT", "REQUEST_CHANGES", "REJECT", "REOPEN"):
            self._review(
                action,
                reason=" Kiểm lại " if action != "ACCEPT" else "",
                actor=" reviewer ",
            )
            self.assertEqual(
                current_claim_review(self.db, self.claim.claim_version_id), action
            )
        events = self.db.execute(
            "SELECT evidence_version_id,claim_version_id,draft_version_id,action,actor_id,reason FROM review_events ORDER BY rowid"
        ).fetchall()
        self.assertEqual(
            events,
            [
                (
                    None,
                    self.claim.claim_version_id,
                    None,
                    action,
                    "reviewer",
                    None if action == "ACCEPT" else "Kiểm lại",
                )
                for action in ("ACCEPT", "REQUEST_CHANGES", "REJECT", "REOPEN")
            ],
        )
        for sql in (
            "UPDATE review_events SET actor_id='changed'",
            "DELETE FROM review_events",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
            self.db.rollback()
        self.assertEqual(self._counts()[-1], 4)

    def test_correction_creates_sealed_version_and_old_correct_event(self) -> None:
        self._review()
        old = self.db.execute("SELECT * FROM claim_versions").fetchall()
        corrected = self._correct(
            links=[EvidenceLink(self.evidence.evidence_version_id, "CONTRADICTS")]
        )
        self.assertEqual(
            (corrected.claim_id, corrected.version_no), (self.claim.claim_id, 2)
        )
        self.assertNotEqual(corrected.claim_version_id, self.claim.claim_version_id)
        self.assertEqual(
            self.db.execute(
                "SELECT * FROM claim_versions WHERE version_no=1"
            ).fetchall(),
            old,
        )
        self.assertEqual(self._counts(), (2, 2, 2, 2))
        self.assertEqual(
            current_claim_review(self.db, self.claim.claim_version_id), "CORRECT"
        )
        self.assertEqual(
            current_claim_review(self.db, corrected.claim_version_id), "PENDING"
        )
        self.assertEqual(
            self.db.execute(
                "SELECT action,actor_id,reason FROM review_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone(),
            ("CORRECT", "editor", "Thêm ngữ cảnh"),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT relation_type FROM claim_evidence WHERE claim_version_id=?",
                (self.claim.claim_version_id,),
            ).fetchone()[0],
            "SUPPORTS",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT relation_type FROM claim_evidence WHERE claim_version_id=?",
                (corrected.claim_version_id,),
            ).fetchone()[0],
            "CONTRADICTS",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "DELETE FROM claim_evidence WHERE claim_version_id=?",
                (corrected.claim_version_id,),
            )
        self.db.rollback()

    def test_second_correction_and_review_reject_old_version(self) -> None:
        corrected = self._correct()
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "latest Claim Version"):
            self._correct()
        with self.assertRaisesRegex(ValueError, "latest Claim Version"):
            self._review()
        self.assertEqual(self._counts(), before)
        self._review(version=corrected.claim_version_id)

    def test_event_failure_rolls_back_replacement_citations_and_seal(self) -> None:
        self.db.execute(
            "CREATE TRIGGER fail_claim_event BEFORE INSERT ON review_events WHEN NEW.claim_version_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'event failed'); END"
        )
        self.db.commit()
        before = self._counts()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "event failed"):
            self._correct()
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self._counts(), before)

    def test_caller_owns_successful_correction_and_review_commit(self) -> None:
        self.db.execute("BEGIN")
        corrected = self._correct()
        self._review(version=corrected.claim_version_id)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self._counts(), (2, 2, 2, 2))
        self.db.rollback()
        self.assertEqual(self._counts(), (1, 1, 1, 0))

    def test_failed_correction_preserves_unrelated_caller_work(self) -> None:
        self.db.execute("INSERT INTO claims VALUES('caller','INTERPRETIVE','now',NULL)")
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "live Evidence Version"):
            self._correct(links=self.links + [EvidenceLink("missing", "CONTEXT")])
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self._counts(), before)
        self.db.commit()
        self.assertIsNotNone(
            self.db.execute("SELECT 1 FROM claims WHERE claim_id='caller'").fetchone()
        )

    def test_accept_rechecks_bytes_and_missing_anchor_but_reject_can_explain(
        self,
    ) -> None:
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            self._review()
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(self._counts()[-1], 0)
        self.stored.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            self._review()
        self.assertEqual(failure.exception.reason, "missing_file")
        self._review("REJECT", reason="Mất anchor")

    def test_accept_checks_every_citation_and_rejects_deleted_evidence(self) -> None:
        other = Path(self.temp.name) / "Khác.txt"
        other.write_text("Nguồn khác", encoding="utf-8")
        imported = intake_text_original(other, root=self.root)
        context = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=0,
            end=5,
            actor="editor",
        )
        corrected = self._correct(
            links=self.links + [EvidenceLink(context.evidence_version_id, "CONTEXT")]
        )
        self.root.joinpath(*imported.storage_key.split("/")).unlink()
        with self.assertRaises(EvidenceReopenError):
            self._review(version=corrected.claim_version_id)
        self.db.execute(
            "UPDATE evidences SET deleted_at='now' WHERE evidence_id=?",
            (self.evidence.evidence_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live Evidence"):
            self._review(version=corrected.claim_version_id)
        self.assertEqual(
            current_claim_review(self.db, corrected.claim_version_id), "PENDING"
        )

    def test_invalid_actions_actor_and_reason_never_write(self) -> None:
        for action, actor, reason in (
            ("CORRECT", "editor", "reason"),
            ("ACCEPT", " ", ""),
            ("REJECT", "editor", " "),
            ("REQUEST_CHANGES", "editor", ""),
            ("REOPEN", "editor", ""),
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self._review(action, actor=actor, reason=reason)
        for actor, reason in (("", "reason"), ("editor", "")):
            with self.assertRaises(ValueError):
                correct_claim(
                    self.db,
                    claim_version_id=self.claim.claim_version_id,
                    statement="New",
                    actor=actor,
                    reason=reason,
                    links=self.links,
                )
        self.assertEqual(self._counts(), (1, 1, 1, 0))

    def test_missing_unsealed_deleted_claim_versions_are_rejected(self) -> None:
        self.db.execute(
            "INSERT INTO claim_versions VALUES('unsealed',?,2,'Building','editor','now')",
            (self.claim.claim_id,),
        )
        self.db.commit()
        for version in ("missing", "unsealed"):
            with self.assertRaisesRegex(ValueError, "Live sealed"):
                self._review(version=version)
            with self.assertRaisesRegex(ValueError, "Live sealed"):
                self._correct(version=version)
        self.db.execute(
            "UPDATE claims SET deleted_at='now' WHERE claim_id=?",
            (self.claim.claim_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Live sealed"):
            self._review()
        with self.assertRaisesRegex(ValueError, "Live sealed"):
            self._correct()
        self.assertEqual(self._counts()[-1], 0)

    def test_accept_does_not_rewrite_stale_citations_or_authorize_publication(
        self,
    ) -> None:
        correct_text_evidence(
            self.db,
            evidence_version_id=self.evidence.evidence_version_id,
            data_root=self.root,
            start=0,
            end=10,
            actor="editor",
            reason="Wider",
        )
        self._review()
        support = claim_support_review(self.db, self.claim.claim_version_id)
        self.assertEqual(
            (support.stale_links, support.accepted_current_supports), (1, 0)
        )
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_version_id FROM claim_evidence"
            ).fetchone()[0],
            self.evidence.evidence_version_id,
        )
