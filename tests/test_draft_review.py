"""Check Draft review against real exact anchors and atomic immutable snapshots."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.claims import EvidenceLink, create_claim
from creator_loop.database import initialize
from creator_loop.draft_review import (
    correct_draft,
    current_draft_review,
    record_draft_review,
)
from creator_loop.drafts import DraftAssertion, DraftClaimLink, create_draft
from creator_loop.evidence_reopen import EvidenceReopenError
from creator_loop.projects import create_project
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original
from test_claims import _NonWindowsOwnedFile


class DraftReviewTests(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Dữ liệu review Draft"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        source = Path(self.temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.anchor = self.root.joinpath(*imported.storage_key.split("/"))
        self.db = sqlite3.connect(self.path)
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
        self.claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Có Café",
            actor="editor",
            links=[EvidenceLink(self.evidence.evidence_version_id, "SUPPORTS")],
        )
        self.project = create_project(self.db, title="Project").project_id
        self.links = [DraftClaimLink(self.claim.claim_version_id, "ASSERTED")]
        self.draft = create_draft(
            self.db,
            project_id=self.project,
            body_text="Café ở Huế",
            format="POST",
            actor="editor",
            claims=self.links,
            assertions=[
                DraftAssertion(0, 4, self.claim.claim_version_id, "UNREVIEWED"),
                DraftAssertion(5, 10, None, "NEEDS_SOURCE"),
            ],
        )

    def _review(self, action="ACCEPT", reason="", version=None, actor=" reviewer "):
        return record_draft_review(
            self.db,
            draft_version_id=version or self.draft.draft_version_id,
            data_root=self.root,
            action=action,
            actor=actor,
            reason=reason,
        )

    def _correct(self, version=None):
        return correct_draft(
            self.db,
            draft_version_id=version or self.draft.draft_version_id,
            body_text="Café — bản đã sửa 🏞",
            format="POST",
            actor=" editor ",
            reason=" Sửa lời ",
            claims=self.links,
            assertions=[DraftAssertion(0, 4, self.claim.claim_version_id, "SUPPORTED")],
        )

    def _counts(self):
        return tuple(
            self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "draft_versions",
                "draft_version_seals",
                "draft_version_parents",
                "draft_claims",
                "draft_assertions",
                "review_events",
            )
        )

    def test_actions_have_exact_draft_target_and_do_not_rewrite_assertion_flags(self):
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "PENDING"
        )
        before = self.db.execute("SELECT * FROM draft_assertions").fetchall()
        for action in ("ACCEPT", "REQUEST_CHANGES", "REJECT", "REOPEN"):
            self._review(action, reason=" Kiểm lại " if action != "ACCEPT" else "")
            self.assertEqual(
                current_draft_review(self.db, self.draft.draft_version_id), action
            )
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_version_id,claim_version_id,draft_version_id,actor_id FROM review_events"
            ).fetchall(),
            [(None, None, self.draft.draft_version_id, "reviewer")] * 4,
        )
        self.assertEqual(
            self.db.execute("SELECT * FROM draft_assertions").fetchall(), before
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
        )

    def test_correction_appends_sealed_snapshot_and_correct_on_old_version(self):
        old = self.db.execute("SELECT * FROM draft_versions").fetchall()
        corrected = self._correct()
        self.assertEqual(
            (corrected.draft_id, corrected.version_no), (self.draft.draft_id, 2)
        )
        self.assertEqual(
            self.db.execute(
                "SELECT * FROM draft_versions WHERE version_no=1"
            ).fetchall(),
            old,
        )
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "CORRECT"
        )
        self.assertEqual(
            current_draft_review(self.db, corrected.draft_version_id), "PENDING"
        )
        self.assertEqual(
            self.db.execute(
                "SELECT action,actor_id,reason FROM review_events"
            ).fetchone(),
            ("CORRECT", "editor", "Sửa lời"),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT parent_draft_version_id FROM draft_version_parents"
            ).fetchone()[0],
            self.draft.draft_version_id,
        )
        with self.assertRaisesRegex(ValueError, "Latest active sealed"):
            self._correct()
        with self.assertRaisesRegex(ValueError, "Latest active sealed"):
            self._review()
        self._review(version=corrected.draft_version_id)

    def test_event_failure_rolls_back_all_replacement_rows_and_keeps_caller_work(self):
        self.db.execute(
            "CREATE TRIGGER fail_draft_event BEFORE INSERT ON review_events WHEN NEW.draft_version_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'event failed'); END"
        )
        self.db.commit()
        before = self._counts()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "event failed"):
            self._correct()
        self.assertEqual(self._counts(), before)
        self.assertFalse(self.db.in_transaction)
        self.db.execute(
            "INSERT INTO projects VALUES('caller','Pending','ACTIVE','now',NULL)"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "event failed"):
            self._correct()
        self.assertEqual(self._counts(), before)
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM projects WHERE project_id='caller'"
            ).fetchone()
        )

    def test_successful_review_and_correction_leave_rollback_to_caller(self):
        before = self._counts()
        self.db.execute("BEGIN")
        corrected = self._correct()
        self._review(version=corrected.draft_version_id)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self._counts(), before)

    def test_accept_rechecks_anchor_while_reject_can_explain_missing_source(self):
        raw = bytearray(self.anchor.read_bytes())
        raw[-1] ^= 1
        self.anchor.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failed:
            self._review()
        self.assertEqual(failed.exception.reason, "digest_mismatch")
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "PENDING"
        )
        self.anchor.unlink()
        self._review("REJECT", reason="Nguồn mất")
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "REJECT"
        )

    def test_accept_rejects_soft_deleted_claim_or_evidence(self):
        self.db.execute("UPDATE evidences SET deleted_at='now'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live Evidence"):
            self._review()
        self.db.execute("UPDATE evidences SET deleted_at=NULL")
        self.db.execute("UPDATE claims SET deleted_at='now'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live sealed Claim"):
            self._review()
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "PENDING"
        )

    def test_archived_project_and_draft_and_unsealed_targets_cannot_be_reviewed(self):
        self.db.execute("UPDATE projects SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Active live Project"):
            self._review()
        with self.assertRaisesRegex(ValueError, "Active live Project"):
            self._correct()
        self.db.execute("UPDATE projects SET status='ACTIVE'")
        self.db.execute("UPDATE drafts SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Latest active sealed"):
            self._review()
        self.db.execute("UPDATE drafts SET status='ACTIVE'")
        self.db.execute(
            "INSERT INTO draft_versions VALUES('building',?,2,'abc','POST','editor','now')",
            (self.draft.draft_id,),
        )
        self.db.commit()
        for version in ("missing", "building", self.draft.draft_version_id):
            with self.assertRaisesRegex(ValueError, "Latest active sealed"):
                self._review(version=version)
        with self.assertRaises(ValueError):
            self._correct()
        self.assertEqual(
            current_draft_review(self.db, self.draft.draft_version_id), "PENDING"
        )

    def test_invalid_action_actor_reason_never_append_events_or_versions(self):
        before = self._counts()
        for action, reason, actor in (
            ("CORRECT", "why", "editor"),
            ("ACCEPT", "", ""),
            ("REJECT", "", "editor"),
            ("REQUEST_CHANGES", "", "editor"),
            ("REOPEN", "", "editor"),
        ):
            with self.assertRaises(ValueError):
                self._review(action, reason=reason, actor=actor)
        with self.assertRaises(ValueError):
            correct_draft(
                self.db,
                draft_version_id=self.draft.draft_version_id,
                body_text="new",
                format="POST",
                actor="editor",
                reason="",
            )
        self.assertEqual(self._counts(), before)
