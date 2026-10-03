"""Real Qt/SQLite manual publication, exact text/media and asynchronous errors."""

import os
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claim_review import record_claim_review
    from creator_loop.claims import EvidenceLink, create_claim
    from creator_loop.creator_ui import CreatorDialog
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.drafts import DraftClaimLink, append_draft_version, create_draft
    from creator_loop.evidence_review import record_evidence_review
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.media_intake import intake_image_original
    from creator_loop.packages import PublicationRepository
    from creator_loop.projects import archive_project, create_project
    from creator_loop.publication import list_approvals, list_posts, package_snapshot
    from creator_loop.publication_ui import PublicationDialog
    from creator_loop.source_association import SourceDetails, create_source_for_asset
    from creator_loop.text_evidence import create_text_evidence
    from creator_loop.text_intake import intake_text_original


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class PublicationDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Publication Qt Huế"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        source = Path(temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        self.text_file = intake_text_original(source, root=self.root)
        self.raw = "A\r\nCafé\u00a0🏞 — Huế"
        with closing(_connect_write(self.path)) as db:
            self.project = create_project(db, title="Huế").project_id
            self.evidence = create_text_evidence(
                db,
                file_id=self.text_file.file_id,
                data_root=self.root,
                start=0,
                end=4,
                actor="editor",
            )
            self.claim = create_claim(
                db,
                claim_type="FACTUAL",
                statement="Café",
                actor="editor",
                links=[EvidenceLink(self.evidence.evidence_version_id, "SUPPORTS")],
            )
            self.draft = create_draft(
                db,
                project_id=self.project,
                body_text=self.raw,
                format="POST",
                actor="creator",
                claims=[DraftClaimLink(self.claim.claim_version_id, "ASSERTED")],
            )
        self.warnings = patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
        )
        self.warnings.start()
        self.addCleanup(self.warnings.stop)

    def _dialog(self):
        dialog = PublicationDialog(self.root)
        self.addCleanup(dialog.close)
        dialog.project.setCurrentIndex(dialog.project.findData(self.project))
        dialog.draft.setCurrentIndex(dialog.draft.findData(self.draft.draft_version_id))
        return dialog

    def _wait(self, dialog):
        deadline = time.monotonic() + 10
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker, "Publication worker did not finish")

    def _create(self, dialog):
        self.assertTrue(dialog.create_button.isEnabled())
        dialog.create_package()
        self._wait(dialog)
        self.assertTrue(dialog._loaded, dialog.status.text())
        self.assertIsNotNone(dialog._snapshot, dialog.status.text())
        return dialog._snapshot.package_id

    def _accept(self):
        with closing(_connect_write(self.path)) as db:
            record_evidence_review(
                db,
                evidence_version_id=self.evidence.evidence_version_id,
                data_root=self.root,
                action="ACCEPT",
                actor="evidence reviewer",
            )
            record_claim_review(
                db,
                claim_version_id=self.claim.claim_version_id,
                data_root=self.root,
                action="ACCEPT",
                actor="claim reviewer",
            )

    def _check(self, dialog):
        dialog.check()
        self._wait(dialog)

    def _approve(self, dialog):
        self._accept()
        self._check(dialog)
        dialog.actor.setText("human Huế")
        dialog.review_confirmed.setChecked(True)
        self.assertTrue(
            dialog.approve_button.isEnabled(), dialog.eligibility.toPlainText()
        )
        dialog.decide("APPROVED")
        self._wait(dialog)

    def test_factual_blockers_then_human_approval_pending_and_manual_confirmation(self):
        dialog = self._dialog()
        package = self._create(dialog)
        dialog.actor.setText("human Huế")
        dialog.review_confirmed.setChecked(True)
        self._check(dialog)
        self.assertFalse(dialog.approve_button.isEnabled())
        self.assertIn(self.claim.claim_version_id, dialog.eligibility.toPlainText())
        self.assertIn("ACCEPT", dialog.eligibility.toPlainText())
        self._approve(dialog)
        self._check(dialog)
        dialog.prepare()
        self._wait(dialog)
        self.assertEqual(dialog.selected_post().status, "PENDING")
        self._check(dialog)
        dialog.external_id.setText("external Huế")
        dialog.external_url.setText("https://example.invalid/huế")
        dialog.published_at.setText("2026-10-03T10:30:00+07:00")
        dialog.post_confirmed.setChecked(True)
        self.assertTrue(dialog.publish_button.isEnabled())
        dialog.publish()
        self._wait(dialog)
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(list_approvals(db, package)[-1].actor_id, "human Huế")
            posts = list_posts(db, package)
            self.assertEqual(len(posts), 1)
            self.assertEqual(
                (posts[0].status, posts[0].published_at),
                ("PUBLISHED", "2026-10-03T03:30:00Z"),
            )
        self.assertFalse(dialog.publish_button.isEnabled())

    def test_raw_caption_snapshot_and_historical_draft_navigation_survive_correction(
        self,
    ):
        dialog = self._dialog()
        package = self._create(dialog)
        with closing(_connect_write(self.path)) as db:
            append_draft_version(
                db,
                draft_version_id=self.draft.draft_version_id,
                body_text="Bản mới",
                format="POST",
                actor="creator",
                claims=[DraftClaimLink(self.claim.claim_version_id, "ASSERTED")],
            )
        self.assertTrue(dialog.reload(package))
        self.assertEqual(dialog._snapshot.items[0][2], self.raw)
        self.assertEqual(dialog._snapshot.draft_version_id, self.draft.draft_version_id)
        opened = []
        dialog.draft_requested.connect(opened.append)
        dialog.open_draft()
        self.assertEqual(opened, [self.draft.draft_version_id])

    def test_gate_is_rechecked_after_successful_ui_inspection(self):
        dialog = self._dialog()
        package = self._create(dialog)
        self._accept()
        self._check(dialog)
        dialog.actor.setText("human Huế")
        dialog.reason.setText("Giữ input")
        dialog.review_confirmed.setChecked(True)
        with closing(_connect_write(self.path)) as db:
            record_evidence_review(
                db,
                evidence_version_id=self.evidence.evidence_version_id,
                data_root=self.root,
                action="REOPEN",
                actor="reviewer",
                reason="Kiểm lại",
            )
        dialog.decide("APPROVED")
        self._wait(dialog)
        self.assertIn("REOPEN", dialog.status.text())
        self.assertEqual(
            (dialog.actor.text(), dialog.reason.text()), ("human Huế", "Giữ input")
        )
        self.assertFalse(dialog.approve_button.isEnabled())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(list_approvals(db, package), ())

    def test_ordered_png_media_cta_alt_preview_and_handle_release(self):
        imported = []
        for color in (0xFFFF0000, 0xFF0000FF):
            image = QImage(8, 6, QImage.Format.Format_RGB32)
            image.fill(color)
            source = self.root.parent / f"Ảnh {color}.png"
            self.assertTrue(image.save(str(source)))
            media = intake_image_original(source, root=self.root)
            imported.append(media)
            with closing(_connect_write(self.path)) as db:
                create_source_for_asset(
                    db,
                    asset_id=media.asset_id,
                    details=SourceDetails(platform="LOCAL", rights_status="OWNED"),
                    relationship_type="ORIGIN",
                )
        dialog = self._dialog()
        for media in imported:
            dialog.file.setCurrentIndex(dialog.file.findData(media.file_id))
            dialog.add_media()
        dialog.media.setCurrentRow(1)
        dialog.move_media(-1)
        dialog.cta.setText("CTA 🏞")
        dialog.alt.setText("Hai ảnh")
        package = self._create(dialog)
        self.assertEqual(
            [
                (item[3], item[1])
                for item in dialog._snapshot.items
                if item[0] == "MEDIA"
            ],
            [(imported[1].file_id, 0), (imported[0].file_id, 1)],
        )
        self.assertIn(
            ("CTA", 0, "CTA 🏞"), [item[:3] for item in dialog._snapshot.items]
        )
        row = next(
            i
            for i, item in enumerate(dialog._snapshot.items)
            if item[3] == imported[1].file_id
        )
        dialog.items.setCurrentCell(row, 0)
        stored = self.root.joinpath(*imported[1].storage_key.split("/"))
        previewed = []

        def viewer(_content, source):
            self.assertEqual(source.anchor_file_id, imported[1].file_id)
            self.assertEqual(source.image.pixel(0, 0), 0xFF0000FF)
            mock = MagicMock()

            def inspect():
                with self.assertRaises(PermissionError):
                    with stored.open("r+b"):
                        pass
                previewed.append(source.anchor_file_id)
                return 0

            mock.exec.side_effect = inspect
            return mock

        with patch("creator_loop.publication_ui.WholeEvidenceView", side_effect=viewer):
            dialog.preview_file()
            self._wait(dialog)
        self.assertEqual(previewed, [imported[1].file_id])
        with stored.open("r+b") as stream:
            self.assertTrue(stream.read())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                package_snapshot(db, package).items, dialog._snapshot.items
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM evidences").fetchone()[0], 1
            )

    def test_partial_write_failure_keeps_composer_input(self):
        dialog = self._dialog()
        dialog.cta.setText("Giữ input 🏞")
        with closing(_connect_write(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_item BEFORE INSERT ON publication_package_items BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
            db.commit()
        dialog.create_package()
        self._wait(dialog)
        self.assertEqual(dialog.cta.text(), "Giữ input 🏞")
        self.assertEqual(dialog.draft.currentData(), self.draft.draft_version_id)
        self.assertTrue(dialog.create_button.isEnabled())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM publication_packages").fetchone()[0], 0
            )

    def test_read_failure_after_commit_disables_all_writes_until_reload(self):
        dialog = self._dialog()
        with patch.object(dialog, "reload", return_value=False):
            dialog.create_package()
            self._wait(dialog)
        self.assertIn("chưa đọc lại", dialog.status.text())
        self.assertFalse(dialog.create_button.isEnabled())
        self.assertFalse(dialog.approve_button.isEnabled())
        self.assertFalse(dialog.new_button.isEnabled())
        dialog.new_package()
        self.assertFalse(dialog._loaded)
        self.assertFalse(dialog.create_button.isEnabled())
        with closing(open_readonly(self.path)) as db:
            identity = db.execute(
                "SELECT package_id FROM publication_packages"
            ).fetchone()[0]
        self.assertTrue(dialog.reload(identity))
        self.assertEqual(dialog._snapshot.package_id, identity)

    def test_worker_captures_actor_and_busy_close_is_blocked(self):
        dialog = self._dialog()
        self._create(dialog)
        self._accept()
        self._check(dialog)
        dialog.actor.setText("captured human")
        dialog.review_confirmed.setChecked(True)
        entered, release = threading.Event(), threading.Event()
        original = PublicationRepository.record_approval
        threads = []

        def delayed(repo, **kwargs):
            threads.append(QThread.currentThread() is self.app.thread())
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test release not received")
            return original(repo, **kwargs)

        dialog.show()
        try:
            with patch.object(PublicationRepository, "record_approval", delayed):
                dialog.decide("APPROVED")
                self.assertTrue(entered.wait(2))
                self.assertFalse(dialog.project.isEnabled())
                self.assertFalse(dialog.actor.isEnabled())
                dialog.actor.setText("changed widget")
                dialog.close()
                dialog.reject()
                self.assertTrue(dialog.isVisible())
                release.set()
                self._wait(dialog)
        finally:
            release.set()
            self._wait(dialog)
        self.assertEqual(threads, [False])
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                list_approvals(db, dialog.selected_package_id())[-1].actor_id,
                "captured human",
            )

    def test_archived_history_allows_revoke_and_foreign_package_is_rejected(self):
        dialog = self._dialog()
        package = self._create(dialog)
        self._approve(dialog)
        with closing(_connect_write(self.path)) as db:
            archive_project(db, project_id=self.project)
        dialog.reload(package)
        self.assertFalse(dialog.create_button.isEnabled())
        self.assertFalse(dialog.approve_button.isEnabled())
        dialog.actor.setText("human")
        dialog.reason.setText("Project lưu trữ")
        self.assertTrue(dialog.revoke_button.isEnabled())
        dialog.decide("REVOKED")
        self._wait(dialog)
        with closing(_connect_write(self.path)) as db:
            foreign_project = create_project(db, title="Other").project_id
        dialog.project.setCurrentIndex(
            dialog.project.findData(foreign_project)
        )  # Not yet loaded.
        dialog.reload()
        dialog.project.setCurrentIndex(dialog.project.findData(foreign_project))
        dialog.packages.addItem("foreign injected", package)
        dialog.packages.setCurrentIndex(dialog.packages.findData(package))
        self.assertFalse(dialog._loaded)
        self.assertIn("selected Project", dialog.status.text())
        self.assertFalse(dialog.approve_button.isEnabled())

    def test_copy_uses_exact_raw_text_rechecks_gate_and_preserves_one_pending_post(
        self,
    ):
        dialog = self._dialog()
        package = self._create(dialog)
        self._approve(dialog)
        self._check(dialog)
        dialog.prepare()
        self._wait(dialog)
        self._check(dialog)
        row = next(
            i for i, item in enumerate(dialog._snapshot.items) if item[0] == "CAPTION"
        )
        dialog.items.setCurrentCell(row, 0)
        clipboard = MagicMock()
        with patch(
            "creator_loop.publication_ui.QApplication.clipboard", return_value=clipboard
        ):
            dialog.copy_text()
            self._wait(dialog)
            clipboard.setText.assert_called_once_with(self.raw)
            self._check(dialog)
            dialog.items.setCurrentCell(row, 0)
            self.assertTrue(dialog.copy_button.isEnabled())
            with closing(_connect_write(self.path)) as db:
                record_evidence_review(
                    db,
                    evidence_version_id=self.evidence.evidence_version_id,
                    data_root=self.root,
                    action="REOPEN",
                    actor="reviewer",
                    reason="Kiểm lại",
                )
            dialog.copy_text()
            self._wait(dialog)
            clipboard.setText.assert_called_once_with(self.raw)
            self.assertIn("REOPEN", dialog.status.text())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(len(list_posts(db, package)), 1)
            self.assertEqual(list_posts(db, package)[0].status, "PENDING")

    def test_library_opens_exact_historical_draft_from_publication(self):
        dialog = self._dialog()
        self._create(dialog)
        with closing(_connect_write(self.path)) as db:
            append_draft_version(
                db,
                draft_version_id=self.draft.draft_version_id,
                body_text="New",
                format="POST",
                actor="creator",
            )
        window = LibraryWindow(self.root)
        self.addCleanup(window.close)
        opened = []

        def inspect(creator):
            opened.append(
                (
                    creator.selected_version_id(),
                    creator.current_body(),
                    creator.body.isReadOnly(),
                )
            )
            return 0

        with (
            patch("creator_loop.library_ui.PublicationDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=dialog.open_draft),
            patch.object(CreatorDialog, "exec", inspect),
        ):
            window.choose_publication()
        self.assertEqual(opened, [(self.draft.draft_version_id, self.raw, True)])
