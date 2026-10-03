"""Factual policy on sealed Packages, real TEXT bytes and human review history."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.claim_review import record_claim_review
from creator_loop.claims import EvidenceLink, append_claim_version, create_claim
from creator_loop.database import initialize
from creator_loop.drafts import DraftClaimLink, create_draft
from creator_loop.evidence_correction import correct_text_evidence
from creator_loop.evidence_review import record_evidence_review
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.projects import create_project
from creator_loop.publication_policy import (
    FactualPublicationBlocked,
    require_factual_reviews,
)
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original
from test_claims import _NonWindowsOwnedFile


class PublicationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned.start()
            self.addCleanup(owned.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Dữ liệu publication"
        self.root.mkdir()
        path = self.root / "creator_loop.sqlite3"
        initialize(path)
        source = Path(temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*imported.storage_key.split("/"))
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.repo = PublicationRepository(path)
        self.project = create_project(self.db, title="Publication V1")
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
        self.package = self._package([self.claim.claim_version_id])

    def _package(self, claims):
        draft = create_draft(
            self.db,
            project_id=self.project.project_id,
            body_text="Có Café",
            format="TEXT",
            actor="creator",
            claims=[DraftClaimLink(version, "ASSERTED") for version in claims],
        )
        package_id = uuid4().hex
        self.repo.create_package(
            package_id=package_id,
            project_id=self.project.project_id,
            draft_version_id=draft.draft_version_id,
            platform="MANUAL",
            format="TEXT",
            created_at="2026-10-03T02:30:00Z",
            items=[PackageItem("CAPTION", 0, text_payload="Có Café")],
        )
        return package_id

    def _claim_review(self, action="ACCEPT", version=None):
        record_claim_review(
            self.db,
            claim_version_id=version or self.claim.claim_version_id,
            data_root=self.root,
            action=action,
            actor="human reviewer",
            reason="Kiểm lại" if action != "ACCEPT" else "",
        )

    def _evidence_review(self, action="ACCEPT", version=None):
        record_evidence_review(
            self.db,
            evidence_version_id=version or self.evidence.evidence_version_id,
            data_root=self.root,
            action=action,
            actor="human reviewer",
            reason="Kiểm lại" if action != "ACCEPT" else "",
        )

    def _blockers(self, package=None):
        return self.repo.factual_review_blockers(
            package_id=package or self.package,
            data_root=self.root,
        )

    def test_both_exact_versions_require_accept_and_reasons_are_explicit(self):
        blockers = self._blockers()
        self.assertEqual(
            {b.code for b in blockers},
            {"claim_not_accepted", "supports_not_accepted"},
        )
        for blocker in blockers:
            self.assertEqual(blocker.version_id, self.claim.claim_version_id)
            self.assertIn("ACCEPT", blocker.message)
        with self.assertRaises(FactualPublicationBlocked) as caught:
            require_factual_reviews(
                self.db, package_id=self.package, data_root=self.root
            )
        self.assertEqual(caught.exception.blockers, blockers)
        self._claim_review()
        self.assertEqual([b.code for b in self._blockers()], ["supports_not_accepted"])
        self._evidence_review()
        self.assertEqual(self._blockers(), ())
        require_factual_reviews(self.db, package_id=self.package, data_root=self.root)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
        )

    def test_evidence_accept_alone_does_not_accept_claim(self):
        self._evidence_review()
        self.assertEqual([b.code for b in self._blockers()], ["claim_not_accepted"])

    def test_accepted_context_and_contradicts_never_count_as_supports(self):
        claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Nhận định",
            actor="editor",
            links=[
                EvidenceLink(self.evidence.evidence_version_id, r)
                for r in ("CONTEXT", "CONTRADICTS")
            ],
        )
        package = self._package([claim.claim_version_id])
        self._evidence_review()
        self._claim_review(version=claim.claim_version_id)
        self.assertEqual(
            [b.code for b in self._blockers(package)], ["supports_not_accepted"]
        )

    def test_one_accepted_support_is_enough_not_all_supports(self):
        second = create_text_evidence(
            self.db,
            file_id=self.evidence.anchor_file_id,
            data_root=self.root,
            start=7,
            end=10,
            actor="creator",
        )
        claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Café ở Huế",
            actor="editor",
            links=self.links + [EvidenceLink(second.evidence_version_id, "SUPPORTS")],
        )
        package = self._package([claim.claim_version_id])
        self._claim_review(version=claim.claim_version_id)
        self._evidence_review()
        self.assertEqual(self._blockers(package), ())

    def test_accept_new_claim_version_does_not_approve_pinned_old_version(self):
        newer = append_claim_version(
            self.db,
            claim_id=self.claim.claim_id,
            statement="Câu mới",
            actor="editor",
            links=self.links,
        )
        self._claim_review(version=newer.claim_version_id)
        self._evidence_review()
        blockers = self._blockers()
        self.assertEqual([b.code for b in blockers], ["claim_not_accepted"])
        self.assertEqual(blockers[0].version_id, self.claim.claim_version_id)
        self.assertEqual(self._blockers(self._package([newer.claim_version_id])), ())

    def test_new_unreviewed_version_does_not_replace_accepted_pinned_claim(self):
        self._claim_review()
        self._evidence_review()
        append_claim_version(
            self.db,
            claim_id=self.claim.claim_id,
            statement="Chưa duyệt",
            actor="editor",
            links=self.links,
        )
        self.assertEqual(self._blockers(), ())

    def test_corrected_evidence_accept_does_not_replace_old_support(self):
        self._claim_review()
        self._evidence_review()
        corrected = correct_text_evidence(
            self.db,
            evidence_version_id=self.evidence.evidence_version_id,
            data_root=self.root,
            start=7,
            end=10,
            actor="editor",
            reason="Sửa anchor",
        )
        self._evidence_review(version=corrected.new_version_id)
        blockers = self._blockers()
        self.assertEqual([b.code for b in blockers], ["supports_not_accepted"])
        self.assertIn(
            self.evidence.evidence_version_id + ": CORRECT", blockers[0].message
        )

    def test_latest_reopen_reject_request_changes_override_old_accept(self):
        self._claim_review()
        for action in ("REOPEN", "REJECT", "REQUEST_CHANGES"):
            self._evidence_review()
            self._evidence_review(action)
            self.assertEqual(
                [b.code for b in self._blockers()], ["supports_not_accepted"]
            )
            self._evidence_review()
            self._claim_review(action)
            self.assertEqual([b.code for b in self._blockers()], ["claim_not_accepted"])
            self._claim_review()

    def test_accepted_but_missing_or_tampered_anchor_blocks(self):
        self._claim_review()
        self._evidence_review()
        original = self.stored.read_bytes()
        self.stored.write_bytes(b"X" * len(original))
        self.assertIn("digest_mismatch", self._blockers()[0].message)
        self.stored.unlink()
        self.assertIn("missing_file", self._blockers()[0].message)

    def test_soft_deleted_claim_or_support_blocks_without_losing_history(self):
        self._claim_review()
        self._evidence_review()
        self.db.execute(
            "UPDATE evidences SET deleted_at='2026-10-03' WHERE evidence_id=?",
            (self.evidence.evidence_id,),
        )
        self.db.commit()
        self.assertEqual([b.code for b in self._blockers()], ["supports_not_accepted"])
        self.db.execute(
            "UPDATE claims SET deleted_at='2026-10-03' WHERE claim_id=?",
            (self.claim.claim_id,),
        )
        self.db.commit()
        self.assertIn("claim_unavailable", [b.code for b in self._blockers()])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 2
        )

    def test_every_factual_claim_is_checked_and_editorial_is_not_auto_approved(self):
        self._claim_review()
        self._evidence_review()
        second = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Chưa có nguồn",
            actor="editor",
            links=[EvidenceLink(self.evidence.evidence_version_id, "CONTEXT")],
        )
        blockers = self._blockers(
            self._package([self.claim.claim_version_id, second.claim_version_id])
        )
        self.assertEqual({b.version_id for b in blockers}, {second.claim_version_id})
        editorial = create_claim(
            self.db,
            claim_type="EDITORIAL_HYPOTHESIS",
            statement="Giả thuyết",
            actor="editor",
            links=[EvidenceLink(self.evidence.evidence_version_id, "CONTEXT")],
        )
        self.assertEqual(
            self._blockers(self._package([editorial.claim_version_id])), ()
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
        )

    def test_missing_package_is_not_vacuously_eligible(self):
        with self.assertRaisesRegex(ValueError, "Sealed complete Package"):
            self._blockers("missing")
