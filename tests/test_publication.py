"""Approval/Post gate on real TEXT originals, review history and concurrent DB writes."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.claim_review import record_claim_review
from creator_loop.claims import EvidenceLink, create_claim
from creator_loop.database import _connect_write, initialize
from creator_loop.drafts import DraftAssertion, DraftClaimLink, create_draft
from creator_loop.evidence_review import record_evidence_review
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.projects import create_project
from creator_loop.publication import _record_approval, list_approvals, list_posts
from creator_loop.publication_policy import FactualPublicationBlocked
from creator_loop.source_association import SourceDetails, create_source_for_asset
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original
from test_claims import _NonWindowsOwnedFile


class PublicationTests(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            owned = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned.start()
            self.addCleanup(owned.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Publication dữ liệu"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        source = Path(temp.name) / "source.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        self.file = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*self.file.storage_key.split("/"))
        self.db = _connect_write(self.path)
        self.addCleanup(self.db.close)
        self.repo = PublicationRepository(self.path)
        self.project = create_project(self.db, title="Publication")
        self.evidence = create_text_evidence(
            self.db,
            file_id=self.file.file_id,
            data_root=self.root,
            start=0,
            end=4,
            actor="editor",
        )
        self.claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Có Café",
            actor="editor",
            links=[EvidenceLink(self.evidence.evidence_version_id, "SUPPORTS")],
        )
        self.package, self.fingerprint = self._package()

    def _package(self, caption="Có Café", *, media=False, unresolved=False):
        draft = create_draft(
            self.db,
            project_id=self.project.project_id,
            body_text=caption,
            format="POST",
            actor="editor",
            claims=[DraftClaimLink(self.claim.claim_version_id, "ASSERTED")],
            assertions=[
                DraftAssertion(
                    0,
                    len(caption),
                    self.claim.claim_version_id,
                    "NEEDS_SOURCE" if unresolved else "SUPPORTED",
                )
            ],
        )
        items = [PackageItem("CAPTION", 0, text_payload=caption)]
        if media:
            items.append(
                PackageItem(
                    "OTHER",
                    0,
                    file_id=self.file.file_id,
                    content_digest=hashlib.sha256(self.stored.read_bytes()).hexdigest(),
                )
            )
        identity = uuid4().hex
        fingerprint = self.repo.create_package(
            package_id=identity,
            project_id=self.project.project_id,
            draft_version_id=draft.draft_version_id,
            platform="FB",
            format="POST",
            created_at="2026-10-03T02:00:00Z",
            items=items,
            data_root=self.root,
        )
        return identity, fingerprint

    def _accept_versions(self):
        record_evidence_review(
            self.db,
            evidence_version_id=self.evidence.evidence_version_id,
            data_root=self.root,
            action="ACCEPT",
            actor="evidence reviewer",
        )
        record_claim_review(
            self.db,
            claim_version_id=self.claim.claim_version_id,
            data_root=self.root,
            action="ACCEPT",
            actor="claim reviewer",
        )

    def _decision(
        self,
        decision="APPROVED",
        *,
        package=None,
        fingerprint=None,
        actor="human editor",
        reason="",
    ):
        return self.repo.record_approval(
            package_id=package or self.package,
            expected_fingerprint=fingerprint or self.fingerprint,
            decision=decision,
            actor=actor,
            reason=reason,
            data_root=self.root,
        )

    def _post(
        self,
        *,
        identity="post",
        package=None,
        fingerprint=None,
        external="external",
        url=None,
        published="2026-10-03T02:01:00Z",
    ):
        return self.repo.record_manual_post(
            post_id=identity,
            package_id=package or self.package,
            expected_fingerprint=fingerprint or self.fingerprint,
            external_post_id=external,
            external_url=url,
            published_at=published,
            data_root=self.root,
        )

    def test_factual_gate_is_required_by_approval_and_no_post_without_approval(self):
        with self.assertRaises(FactualPublicationBlocked) as caught:
            self._decision()
        self.assertEqual(
            {b.code for b in caught.exception.blockers},
            {"claim_not_accepted", "supports_not_accepted"},
        )
        self.assertEqual(list_approvals(self.db, self.package), ())
        with self.assertRaisesRegex(ValueError, "current NONE"):
            self._post()
        self._accept_versions()
        approval = self._decision(actor=" human editor ")
        self.assertEqual(
            (approval.actor_id, approval.package_fingerprint, approval.reason),
            ("human editor", self.fingerprint, None),
        )
        post = self._post()
        self.assertEqual(
            (post.package_id, post.platform, post.status),
            (self.package, "FB", "PUBLISHED"),
        )

    def test_review_reopen_after_approval_blocks_post_and_revocation_is_available(self):
        self._accept_versions()
        self._decision()
        record_evidence_review(
            self.db,
            evidence_version_id=self.evidence.evidence_version_id,
            data_root=self.root,
            action="REOPEN",
            actor="reviewer",
            reason="Kiểm lại",
        )
        with self.assertRaises(FactualPublicationBlocked):
            self._post()
        self.stored.unlink()
        self._decision("REVOKED", reason="Nguồn không còn đạt")
        self.assertEqual(
            [a.decision for a in list_approvals(self.db, self.package)],
            ["APPROVED", "REVOKED"],
        )
        self.assertEqual(list_posts(self.db, self.package), ())

    def test_latest_revoke_or_reject_blocks_until_explicit_new_approval(self):
        self._accept_versions()
        for decision in ("REVOKED", "REJECTED"):
            self._decision()
            self._decision(decision, reason="Không đăng snapshot này")
            with self.assertRaisesRegex(ValueError, "current " + decision):
                self._post(identity=decision)
        self._decision()
        self._post()
        self.assertEqual(len(list_posts(self.db, self.package)), 1)

    def test_caption_change_has_new_fingerprint_and_needs_its_own_approval(self):
        self._accept_versions()
        self._decision()
        package, fingerprint = self._package("Có Café!")
        self.assertNotEqual(fingerprint, self.fingerprint)
        with self.assertRaisesRegex(ValueError, "current NONE"):
            self._post(package=package, fingerprint=fingerprint)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self._decision(package=package, fingerprint=self.fingerprint)
        self._decision(package=package, fingerprint=fingerprint)
        self._post(package=package, fingerprint=fingerprint)

    def test_missing_tampered_source_or_deleted_asset_blocks_post_after_approval(self):
        self._accept_versions()
        self._decision()
        original = self.stored.read_bytes()
        self.stored.write_bytes(b"X" * len(original))
        with self.assertRaises(FactualPublicationBlocked):
            self._post()
        self.stored.write_bytes(original)
        self.db.execute("UPDATE assets SET deleted_at='later'")
        self.db.commit()
        with self.assertRaises(FactualPublicationBlocked):
            self._post()
        self._decision("REVOKED", reason="Không còn nguồn")
        self.assertEqual(list_posts(self.db, self.package), ())

    def test_media_rights_are_rechecked_at_approval_and_post_not_granted_by_citation(
        self,
    ):
        source = create_source_for_asset(
            self.db,
            asset_id=self.file.asset_id,
            details=SourceDetails(platform="LOCAL", rights_status="OWNED"),
            relationship_type="ORIGIN",
        )
        self.package, self.fingerprint = self._package(media=True)
        self._accept_versions()
        self.db.execute(
            "UPDATE sources SET rights_status='REFERENCE_ONLY' WHERE source_id=?",
            (source,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "reuse rights unresolved"):
            self._decision()
        self.db.execute(
            "UPDATE sources SET rights_status='OWNED' WHERE source_id=?", (source,)
        )
        self.db.commit()
        self._decision()
        self.db.execute(
            "UPDATE sources SET rights_status='RESTRICTED' WHERE source_id=?", (source,)
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "reuse rights unresolved"):
            self._post()
        self._decision("REVOKED", reason="Quyền media thay đổi")

    def test_exact_retry_or_known_external_identity_returns_one_post(self):
        self._accept_versions()
        self._decision()
        first = self._post(url="https://example.invalid/p")
        self.assertEqual(self._post(url="https://example.invalid/p"), first)
        self.assertEqual(
            self._post(identity="retry", url="https://example.invalid/p"), first
        )
        with self.assertRaisesRegex(ValueError, "Retry payload"):
            self._post(url="https://example.invalid/changed")
        self.assertEqual(len(list_posts(self.db, self.package)), 1)

    def test_prepare_pending_post_is_gated_retry_safe_and_rechecked_on_confirmation(
        self,
    ):
        payload = dict(
            post_id="pending",
            package_id=self.package,
            expected_fingerprint=self.fingerprint,
            data_root=self.root,
        )
        with self.assertRaisesRegex(ValueError, "current NONE"):
            self.repo.prepare_manual_post(**payload)
        self._accept_versions()
        self._decision()
        pending = self.repo.prepare_manual_post(**payload)
        self.assertEqual(pending.status, "PENDING")
        self.assertEqual(self.repo.prepare_manual_post(**payload), pending)
        self._decision("REVOKED", reason="Rút trước khi đăng")
        with self.assertRaisesRegex(ValueError, "REVOKED"):
            self._post(identity="pending")
        self.assertEqual(list_posts(self.db, self.package), (pending,))
        self._decision()
        published = self._post(identity="pending")
        self.assertEqual(
            (published.status, published.created_at), ("PUBLISHED", pending.created_at)
        )
        self.assertEqual(self._post(identity="pending"), published)
        self.assertEqual(len(list_posts(self.db, self.package)), 1)

    def test_unknown_external_fields_stay_null_and_timestamp_has_explicit_zone(self):
        self._accept_versions()
        self._decision()
        for bad in ("not a date", "2026-10-03T02:00:00", ""):
            with self.assertRaisesRegex(ValueError, "ISO8601"):
                self._post(published=bad)
        post = self._post(external=None, url=" ", published="2026-10-03T09:01:00+07:00")
        self.assertEqual(
            (post.external_post_id, post.external_url, post.published_at),
            (None, None, "2026-10-03T02:01:00Z"),
        )

    def test_invalid_actor_reason_and_unresolved_assertions_cannot_be_approved(self):
        self._accept_versions()
        with self.assertRaisesRegex(ValueError, "actor"):
            self._decision(actor=" ")
        for decision in ("REJECTED", "REVOKED"):
            with self.assertRaisesRegex(ValueError, "reason"):
                self._decision(decision)
        package, fingerprint = self._package(unresolved=True)
        with self.assertRaisesRegex(ValueError, "NEEDS_SOURCE"):
            self._decision(package=package, fingerprint=fingerprint)
        self.assertEqual(list_approvals(self.db, package), ())

    def test_archived_project_blocks_approval_and_post_but_allows_revoke(self):
        self._accept_versions()
        self._decision()
        self.db.execute("UPDATE projects SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "active live Project"):
            self._decision()
        with self.assertRaisesRegex(ValueError, "active live Project"):
            self._post()
        self._decision("REVOKED", reason="Project đã lưu trữ")

    def test_partial_insert_failure_rolls_back_approval_and_post(self):
        self._accept_versions()
        self.db.execute(
            "CREATE TRIGGER fail_approval BEFORE INSERT ON approvals BEGIN SELECT RAISE(ABORT,'injected approval failure'); END"
        )
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._decision()
        self.assertEqual(list_approvals(self.db, self.package), ())
        self.db.execute("DROP TRIGGER fail_approval")
        self.db.execute(
            "CREATE TRIGGER fail_post BEFORE INSERT ON posts BEGIN SELECT RAISE(ABORT,'injected post failure'); END"
        )
        self.db.commit()
        self._decision()
        with self.assertRaises(sqlite3.IntegrityError):
            self._post()
        self.assertEqual(list_posts(self.db, self.package), ())

    def test_publication_does_not_take_over_callers_pending_transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("UPDATE projects SET title='caller WIP'")
        with self.assertRaisesRegex(RuntimeError, "owns its transaction"):
            _record_approval(
                self.db,
                package_id=self.package,
                expected_fingerprint=self.fingerprint,
                decision="APPROVED",
                actor="human",
                reason="",
                data_root=self.root,
            )
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute("SELECT title FROM projects").fetchone()[0], "caller WIP"
        )
        self.db.rollback()

    def test_concurrent_revoke_cannot_write_between_gate_and_post_insert(self):
        self._accept_versions()
        self._decision()
        outcomes = []

        def race():
            with closing(sqlite3.connect(self.path, timeout=0.02)) as other:
                try:
                    other.execute(
                        "INSERT INTO approvals VALUES('race',?,?, 'REVOKED','other','now','race')",
                        (self.package, self.fingerprint),
                    )
                    other.commit()
                except sqlite3.OperationalError as exc:
                    outcomes.append(str(exc))
            return 1

        def connection(path):
            db = _connect_write(path)
            db.create_function("race_revoke", 0, race)
            db.execute(
                "CREATE TEMP TRIGGER race_post BEFORE INSERT ON posts BEGIN SELECT race_revoke(); END"
            )
            return db

        with patch("creator_loop.packages._connect_write", side_effect=connection):
            self._post()
        self.assertEqual(outcomes, ["database is locked"])
        self._decision("REVOKED", reason="Sau commit")
        with self.assertRaisesRegex(ValueError, "REVOKED"):
            self._post(identity="second", external="second")
        self.assertEqual(len(list_posts(self.db, self.package)), 1)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_exact_support_file_is_locked_until_approval_commit(self):
        self._accept_versions()
        outcomes = []

        def probe():
            try:
                with self.stored.open("r+b"):
                    outcomes.append("allowed")
            except PermissionError:
                outcomes.append("blocked")
            return 1

        def connection(path):
            db = _connect_write(path)
            db.create_function("probe_anchor", 0, probe)
            db.execute(
                "CREATE TEMP TRIGGER probe_approval BEFORE INSERT ON approvals BEGIN SELECT probe_anchor(); END"
            )
            return db

        with patch("creator_loop.packages._connect_write", side_effect=connection):
            self._decision()
        self.assertEqual(outcomes, ["blocked"])
        with self.stored.open("r+b") as stream:
            self.assertTrue(stream.read())
