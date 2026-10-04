"""One real source Qt/SQLite workflow; not frozen UI or hardware acceptance."""

import hashlib
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QMessageBox,
            QTableWidgetItem,
        )
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.app_lock import AppDataLock
    from creator_loop.claim_ui import ClaimDialog
    from creator_loop.creator_ui import CreatorDialog
    from creator_loop.database import initialize, open_readonly
    from creator_loop.evidence_review_ui import EvidenceReviewDialog
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.observation_ui import ObservationDialog
    from creator_loop.observations import list_observations
    from creator_loop.project_ui import ProjectDialog
    from creator_loop.publication import list_approvals, list_posts, package_snapshot
    from creator_loop.publication_ui import PublicationDialog
    from creator_loop.selection_ui import SelectionDialog
    from creator_loop.selections import list_selections
    from creator_loop.source_ui import SourceDialog
    from creator_loop.text_evidence_ui import TextEvidenceDialog


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ProductUIFlowTests(unittest.TestCase):
    def _wait(self, window):
        deadline = time.monotonic() + 20
        while window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(window._worker, "Workflow worker did not finish")
        self.app.processEvents()
        self.assertFalse(self.warning.called, self.warning.call_args)

    def _dialog(self, cls, *args):
        window = cls(self.root, *args)
        self.addCleanup(window.close)
        return window

    def _select_original(self, library, kind):
        matches = [
            row
            for row in range(library.table.rowCount())
            if library.table.item(row, 0).text() == kind
        ]
        self.assertEqual(len(matches), 1)
        library.table.selectRow(matches[0])
        return library.table.item(matches[0], 0).data(Qt.ItemDataRole.UserRole)

    def test_three_inputs_to_publication_observation_reopen_and_revocation(self):
        self.app = QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Creator Loop luồng đầy đủ Huế"
        self.root.mkdir()
        (self.root / "runtime").mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        warnings = patch.object(QMessageBox, "warning")
        self.warning = warnings.start()
        self.addCleanup(warnings.stop)
        with AppDataLock(self.root):
            initialize(self.path)
            library = self._dialog(LibraryWindow)
            library.show()
            text = Path(temp.name) / "Ghi chép.txt"
            text.write_text("Cafe\u0301 ở Huế", encoding="utf-8")
            image_path = Path(temp.name) / "Ảnh tự tạo.png"
            image = QImage(12, 8, QImage.Format.Format_RGB32)
            image.fill(0xFF0088AA)
            self.assertTrue(image.save(str(image_path)))
            video = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
            for kind, source in (
                ("TEXT", text),
                ("IMAGE", image_path),
                ("VIDEO", video),
            ):
                with patch.object(
                    QFileDialog, "getOpenFileName", return_value=(str(source), "")
                ):
                    library.choose_original(kind, "Tất cả tệp (*)")
                self._wait(library)
            self.assertEqual(library.table.rowCount(), 3)
            image_id = self._select_original(library, "IMAGE")

            def owned_image(dialog):
                dialog.platform.setText("LOCAL")
                dialog.rights.setCurrentText("OWNED")
                dialog.relationship.setCurrentText("ORIGIN")
                return SourceDialog.DialogCode.Accepted

            with patch.object(SourceDialog, "exec", owned_image):
                library.choose_source()
                self._wait(library)
            self._select_original(library, "TEXT")

            def excerpt(dialog):
                dialog.start.setValue(0)
                dialog.end.setValue(len("Café ở Huế"))
                return TextEvidenceDialog.DialogCode.Accepted

            with patch.object(TextEvidenceDialog, "exec", excerpt):
                library.choose_text_evidence()
                self._wait(library)
            self.assertEqual(library.evidence_table.item(0, 1).text(), "Café ở Huế")
            evidence_id = library.evidence_table.item(0, 0).data(
                Qt.ItemDataRole.UserRole
            )
            library.evidence_table.selectRow(0)
            with patch.object(QMessageBox, "information") as displayed:
                library.reopen_selected_text_evidence()
                self._wait(library)
            self.assertEqual(displayed.call_args.args[2], "Café ở Huế")
            with patch.object(
                EvidenceReviewDialog, "exec", lambda d: d.DialogCode.Accepted
            ):
                library.choose_evidence_review()
                self._wait(library)
            self.assertEqual(library.evidence_table.item(0, 3).text(), "ACCEPT")

            claim = self._dialog(ClaimDialog)
            claim.statement.setPlainText("Café ở Huế")
            claim.evidence.setCurrentIndex(claim.evidence.findData(evidence_id))
            claim.add_button.click()
            claim.save_button.click()
            self._wait(claim)
            claim_id = claim.selected_version_id()
            self.assertIsNotNone(claim_id)
            claim.review_action.setCurrentIndex(claim.review_action.findData("ACCEPT"))
            claim.review_button.click()
            self._wait(claim)
            self.assertIn("ACCEPT", claim.summary.text())

            project = self._dialog(ProjectDialog)
            project.title.setText("Chuyện Huế")
            project.create_button.click()
            self._wait(project)
            project_id = project.selected_project_id()
            self.assertIsNotNone(project_id)
            project.target_type.setCurrentIndex(
                project.target_type.findData("claim_version_id")
            )
            project.target.setCurrentIndex(project.target.findData(claim_id))
            project.usage.setCurrentIndex(project.usage.findData("RESEARCH"))
            project.add_button.click()
            self._wait(project)

            creator = self._dialog(CreatorDialog)
            creator.project.setCurrentIndex(creator.project.findData(project_id))
            drafts = []
            for suffix in ("A", "B"):
                creator.new_button.click()
                creator.body.setPlainText(f"Café ở Huế — phương án {suffix}")
                creator.claim.setCurrentIndex(creator.claim.findData(claim_id))
                creator.add_claim_button.click()
                creator.start.setValue(0)
                creator.end.setValue(len("Café ở Huế"))
                creator.assertion_claim.setCurrentIndex(
                    creator.assertion_claim.findData(claim_id)
                )
                creator.assertion_state.setCurrentText("SUPPORTED")
                creator.add_assertion_button.click()
                self.assertTrue(creator.save_button.isEnabled())
                creator.save_button.click()
                self._wait(creator)
                drafts.append(creator.selected_version_id())
                creator.review_action.setCurrentIndex(
                    creator.review_action.findData("ACCEPT")
                )
                creator.review_button.click()
                self._wait(creator)
            self.assertIsNotNone(drafts[1])
            self.assertNotEqual(*drafts)

            selection = self._dialog(SelectionDialog)
            selection.project.setCurrentIndex(selection.project.findData(project_id))
            for row in range(selection.candidates.count()):
                item = selection.candidates.item(row)
                if item.data(Qt.ItemDataRole.UserRole) in drafts:
                    item.setCheckState(Qt.CheckState.Checked)
            selection.selected.setCurrentIndex(selection.selected.findData(drafts[1]))
            selection.save_button.click()
            self._wait(selection)

            publication = self._dialog(PublicationDialog)
            publication.project.setCurrentIndex(
                publication.project.findData(project_id)
            )
            publication.draft.setCurrentIndex(publication.draft.findData(drafts[1]))
            publication.file.setCurrentIndex(publication.file.findData(image_id))
            publication.add_button.click()
            self.assertEqual(publication.media.count(), 1)
            publication.create_button.click()
            self._wait(publication)
            package_id = publication.selected_package_id()
            self.assertIsNotNone(package_id)
            publication.check_button.click()
            self._wait(publication)
            self.assertFalse(publication.review_confirmed.isChecked())
            self.assertFalse(publication.approve_button.isEnabled())
            publication.actor.setText("reviewer Huế")
            publication.review_confirmed.setChecked(True)
            self.assertTrue(
                publication.approve_button.isEnabled(),
                publication.eligibility.toPlainText(),
            )
            publication.approve_button.click()
            self._wait(publication)
            publication.check_button.click()
            self._wait(publication)
            publication.prepare_button.click()
            self._wait(publication)
            self.assertEqual(publication.selected_post().status, "PENDING")
            publication.check_button.click()
            self._wait(publication)
            publication.published_at.setText("2026-10-05T08:00:00+07:00")
            self.assertFalse(publication.publish_button.isEnabled())
            publication.post_confirmed.setChecked(True)
            publication.publish_button.click()
            self._wait(publication)
            post_id = publication.selected_post().post_id
            self.assertEqual(publication.selected_post().status, "PUBLISHED")

            observation = self._dialog(ObservationDialog, post_id)
            observation.observed_at.setText("2026-10-05T09:00:00+07:00")
            for key, value in (("views", "0"), ("reach", "")):
                observation.add_button.click()
                row = observation.metrics.rowCount() - 1
                for col, value in ((0, key), (2, value), (4, "fixture-v1")):
                    observation.metrics.setItem(row, col, QTableWidgetItem(value))
            observation.save_button.click()
            self._wait(observation)
            self.assertFalse(observation.save_button.isEnabled())
            publication.reason.setText("Rút duyệt sau khi đã đăng; giữ lịch sử")
            publication.revoke_button.click()
            self._wait(publication)
            publication.check_button.click()
            self._wait(publication)
            self.assertFalse(publication.prepare_button.isEnabled())

            with closing(open_readonly(self.path)) as db:
                retained = {
                    key: (self.root / key).read_bytes()
                    for (key,) in db.execute("SELECT storage_key FROM asset_files")
                }
                snapshot = package_snapshot(db, package_id)
                self.assertEqual(snapshot.draft_version_id, drafts[1])
                self.assertEqual(snapshot.items[0][2], "Café ở Huế — phương án B")
                decision = list_selections(db, project_id=project_id)[0]
                self.assertEqual(set(decision.candidate_version_ids), set(drafts))
                self.assertEqual(decision.selected_draft_version_id, drafts[1])
                measurement = list_observations(db, post_id)[0]
                self.assertEqual(measurement.value_for("views"), 0)
                self.assertIsNone(measurement.value_for("reach"))
                self.assertEqual(
                    [a.decision for a in list_approvals(db, package_id)],
                    ["APPROVED", "REVOKED"],
                )
                self.assertEqual(list_posts(db, package_id)[0].status, "PUBLISHED")
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            for window in (
                observation,
                publication,
                selection,
                creator,
                project,
                claim,
                library,
            ):
                self.assertTrue(window.close())
            reopened = self._dialog(LibraryWindow)
            self.assertEqual(reopened.table.rowCount(), 3)
            self.assertEqual(reopened.evidence_table.item(0, 3).text(), "ACCEPT")
            read_package = self._dialog(PublicationDialog)
            read_package.project.setCurrentIndex(
                read_package.project.findData(project_id)
            )
            read_package.packages.setCurrentIndex(
                read_package.packages.findData(package_id)
            )
            self.assertEqual(read_package.posts.rowCount(), 1)
            read_package.posts.selectRow(0)
            self.assertEqual(read_package.selected_post().post_id, post_id)
            read_observation = self._dialog(ObservationDialog, post_id)
            read_observation.history.setCurrentIndex(
                read_observation.history.findData(measurement.observation_id)
            )
            self.assertEqual(read_observation.metrics.rowCount(), 1)
            self.assertEqual(float(read_observation.metrics.item(0, 2).text()), 0)
            self.assertEqual(read_observation.metrics.item(0, 5).text(), "0")
            self.assertFalse(read_observation.save_button.isEnabled())
            for key, content in retained.items():
                self.assertEqual(
                    hashlib.sha256((self.root / key).read_bytes()).digest(),
                    hashlib.sha256(content).digest(),
                )
            for window in (read_observation, read_package, reopened):
                self.assertTrue(window.close())
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
