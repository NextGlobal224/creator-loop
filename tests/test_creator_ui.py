"""Exercise real Qt Creator snapshot authoring, assertion offsets and history."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claim_ui import ClaimDialog
    from creator_loop.claims import EvidenceLink, create_claim
    from creator_loop.creator_ui import CreatorDialog
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.drafts import DraftAssertion, create_draft
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.projects import create_project
    from creator_loop.text_evidence import create_text_evidence
    from creator_loop.text_intake import intake_text_original


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class CreatorDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Creator dữ liệu Huế"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        source = Path(self.temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.anchor = self.root.joinpath(*imported.storage_key.split("/"))
        with closing(_connect_write(self.path)) as db:
            evidence = create_text_evidence(
                db,
                file_id=imported.file_id,
                data_root=self.root,
                start=0,
                end=4,
                actor="creator",
            )
            self.claim = create_claim(
                db,
                claim_type="FACTUAL",
                statement="Có Café",
                actor="editor",
                links=[EvidenceLink(evidence.evidence_version_id, "SUPPORTS")],
            )
            self.project = create_project(db, title="Huế").project_id
            self.other_project = create_project(db, title="Khác").project_id
            self.foreign = create_draft(
                db,
                project_id=self.other_project,
                body_text="Another Project",
                format="POST",
                actor="editor",
            )

    def _dialog(self):
        dialog = CreatorDialog(self.root)
        self.addCleanup(dialog.close)
        dialog.project.setCurrentIndex(dialog.project.findData(self.project))
        return dialog

    def _wait(self, dialog):
        deadline = time.monotonic() + 8
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker, "Creator worker did not finish")
        self.app.processEvents()

    def _link(self, dialog):
        dialog.claim.setCurrentIndex(dialog.claim.findData(self.claim.claim_version_id))
        dialog.use_type.setCurrentIndex(dialog.use_type.findData("ASSERTED"))
        self.assertTrue(dialog.add_claim_button.isEnabled())
        dialog.add_claim_button.click()

    def _mark(self, dialog, start, end, state="UNREVIEWED", claim=None):
        dialog.start.setValue(start)
        dialog.end.setValue(end)
        dialog.assertion_claim.setCurrentIndex(dialog.assertion_claim.findData(claim))
        dialog.assertion_state.setCurrentIndex(dialog.assertion_state.findData(state))
        self.assertTrue(dialog.add_assertion_button.isEnabled())
        dialog.add_assertion_button.click()

    def _create(self, dialog, body="Café 🏞 A"):
        dialog.new_button.click()
        dialog.body.setPlainText(body)
        self._link(dialog)
        self._mark(dialog, 0, 4, claim=self.claim.claim_version_id)
        self._mark(dialog, 5, 6, "EDITORIAL")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.save_button.click()
        self._wait(dialog)
        self.assertIsNotNone(dialog.selected_version_id())
        return dialog.selected_version_id()

    def test_create_a_b_combine_correct_review_and_reopen_history(self):
        dialog = self._dialog()
        a = self._create(dialog)
        b = self._create(dialog, "Café 🏞 B")
        self.assertNotEqual(a, b)
        candidate_ids = [
            dialog.candidates.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog.candidates.count())
        ]
        self.assertNotIn(self.foreign.draft_version_id, candidate_ids)
        dialog.new_button.click()
        dialog.body.setPlainText("Café 🏞 A+B")
        self._link(dialog)
        self._mark(dialog, 0, 4, claim=self.claim.claim_version_id)
        for index in range(dialog.candidates.count()):
            if dialog.candidates.item(index).data(Qt.ItemDataRole.UserRole) in (a, b):
                dialog.candidates.item(index).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(dialog.combine_button.isEnabled())
        dialog.combine_button.click()
        self._wait(dialog)
        combined = dialog.selected_version_id()
        self.assertEqual(dialog.parents.rowCount(), 2)
        self.assertEqual({dialog.parents.item(i, 0).text() for i in range(2)}, {a, b})
        dialog.body.setPlainText("Café 🏞 A+B — sửa")
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.reason.setText("Sửa phần cuối")
        dialog.save_button.click()
        self._wait(dialog)
        corrected = dialog.selected_version_id()
        self.assertNotEqual(corrected, combined)
        self.assertEqual(dialog.parents.item(0, 0).text(), combined)
        self.assertEqual(dialog.parents.item(0, 1).text(), "EDIT")
        for action, reason in (
            ("ACCEPT", ""),
            ("REQUEST_CHANGES", "Kiểm lại"),
            ("REOPEN", "Đã xem lại"),
            ("ACCEPT", ""),
        ):
            dialog.review_action.setCurrentIndex(dialog.review_action.findData(action))
            dialog.reason.setText(reason)
            dialog.review_button.click()
            self._wait(dialog)
        self.assertEqual(dialog.history.rowCount(), 4)
        self.assertIn("ACCEPT", dialog.summary.text())
        dialog.version.setCurrentIndex(dialog.version.findData(combined))
        self.assertEqual(dialog.current_body(), "Café 🏞 A+B")
        self.assertEqual(dialog.history.item(0, 0).text(), "CORRECT")
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())
        dialog.close()
        reopened = self._dialog()
        self.assertTrue(reopened.reload(corrected))
        self.assertEqual(reopened.current_body(), "Café 🏞 A+B — sửa")
        self.assertEqual(reopened.history.rowCount(), 4)

    def test_unicode_codepoint_assertions_detect_changed_spans_before_save(self):
        dialog = self._dialog()
        version = self._create(dialog)
        self.assertEqual(dialog.assertions.item(1, 2).text(), "🏞")
        dialog.body.setPlainText("XX Café 🏞 A")
        dialog.reason.setText("Chèn phần đầu")
        self.assertIn("CẦN ĐÁNH DẤU LẠI", dialog.assertions.item(0, 2).text())
        self.assertFalse(dialog.save_button.isEnabled())
        while dialog.assertions.rowCount():
            dialog.assertions.setCurrentCell(0, 0)
            dialog.remove_assertion_button.click()
        self._mark(dialog, 3, 7, claim=self.claim.claim_version_id)
        self._mark(dialog, 8, 9, "EDITORIAL")
        dialog.save_button.click()
        self._wait(dialog)
        self.assertNotEqual(dialog.selected_version_id(), version)
        with closing(open_readonly(self.path)) as db:
            rows = db.execute(
                "SELECT text_start,text_end,asserted_text FROM draft_assertions WHERE draft_version_id=? ORDER BY text_start",
                (dialog.selected_version_id(),),
            ).fetchall()
        self.assertEqual(rows, [(3, 7, "Café"), (8, 9, "🏞")])

    def test_reading_crlf_and_nbsp_snapshot_preserves_exact_body_and_offsets(self):
        body = "Café\r\n🏞\u00a0Huế"
        with closing(_connect_write(self.path)) as db:
            saved = create_draft(
                db,
                project_id=self.project,
                body_text=body,
                format="POST",
                actor="editor",
                assertions=[DraftAssertion(6, 7, None, "EDITORIAL")],
            )
        dialog = self._dialog()
        self.assertTrue(dialog.reload(saved.draft_version_id))
        self.assertEqual(dialog.current_body(), body)
        self.assertEqual(dialog.assertions.item(0, 2).text(), "🏞")
        dialog.start.setValue(6)
        dialog.end.setValue(7)
        self.assertEqual(dialog.preview.text(), "🏞")
        dialog.reason.setText("Không có thay đổi")
        with patch("creator_loop.creator_ui.QMessageBox.warning") as warning:
            dialog.save_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertIn("No Draft snapshot changes", dialog.status.text())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT body_text FROM draft_versions WHERE draft_version_id=?",
                    (saved.draft_version_id,),
                ).fetchone()[0],
                body,
            )

    def test_removing_claim_retains_assertion_as_needs_source_and_null_target(self):
        dialog = self._dialog()
        first = self._create(dialog)
        dialog.citations.setCurrentCell(0, 0)
        dialog.remove_claim_button.click()
        self.assertEqual(dialog.citations.rowCount(), 0)
        self.assertEqual(dialog.assertions.item(0, 3).text(), "")
        self.assertEqual(dialog.assertions.item(0, 4).text(), "NEEDS_SOURCE")
        dialog.reason.setText("Bỏ Claim để kiểm lại nguồn")
        dialog.save_button.click()
        self._wait(dialog)
        self.assertNotEqual(dialog.selected_version_id(), first)
        self.assertIn("NEEDS_SOURCE 1", dialog.summary.text())
        dialog.review_button.click()
        self._wait(dialog)
        self.assertIn("ACCEPT", dialog.summary.text())
        self.assertIn("NEEDS_SOURCE 1", dialog.summary.text())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
            )

    def test_archived_project_is_readonly_and_foreign_parents_never_mix(self):
        dialog = self._dialog()
        saved = self._create(dialog)
        with closing(_connect_write(self.path)) as db:
            db.execute(
                "UPDATE projects SET status='ARCHIVED' WHERE project_id=?",
                (self.project,),
            )
            db.commit()
        self.assertTrue(dialog.reload(saved))
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.combine_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())
        dialog.project.setCurrentIndex(dialog.project.findData(self.other_project))
        self.assertEqual(dialog.version.findData(saved), -1)
        self.assertGreaterEqual(
            dialog.version.findData(self.foreign.draft_version_id), 0
        )

    def test_missing_anchor_blocks_accept_and_failure_preserves_snapshot(self):
        dialog = self._dialog()
        saved = self._create(dialog)
        self.anchor.unlink()
        with patch("creator_loop.creator_ui.QMessageBox.warning") as warning:
            dialog.review_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertEqual(dialog.selected_version_id(), saved)
        self.assertEqual(dialog.history.rowCount(), 0)
        self.assertIn("missing file", dialog.status.text())
        self.assertIn("PENDING", dialog.summary.text())

    def test_event_failure_preserves_body_reason_and_old_database_snapshot(self):
        dialog = self._dialog()
        saved = self._create(dialog)
        with closing(_connect_write(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_creator_event BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT,'event blocked'); END"
            )
            db.commit()
        dialog.body.setPlainText("Café 🏞 A — sửa")
        dialog.reason.setText("Sửa bản này")
        with patch("creator_loop.creator_ui.QMessageBox.warning") as warning:
            dialog.save_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertEqual(dialog.current_body(), "Café 🏞 A — sửa")
        self.assertEqual(dialog.reason.text(), "Sửa bản này")
        self.assertEqual(dialog.selected_version_id(), saved)
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM draft_versions WHERE draft_id=(SELECT draft_id FROM draft_versions WHERE draft_version_id=?)",
                    (saved,),
                ).fetchone()[0],
                1,
            )

    def test_read_failure_after_commit_fails_closed_without_false_success(self):
        dialog = self._dialog()
        dialog.body.setPlainText("Đã ghi")
        with (
            patch(
                "creator_loop.creator_ui.list_projects",
                side_effect=OSError("read unavailable"),
            ),
            patch("creator_loop.creator_ui.QMessageBox.warning") as warning,
        ):
            dialog.save_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertIn("Không thể đọc Creator", dialog.status.text())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.combine_button.isEnabled())
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT body_text FROM draft_versions WHERE body_text=?",
                    ("Đã ghi",),
                ).fetchone()[0],
                "Đã ghi",
            )
        self.assertTrue(dialog.reload())

    def test_busy_worker_owns_connection_keeps_ui_alive_and_blocks_close(self):
        dialog = self._dialog()
        dialog.show()
        self.app.processEvents()
        entered, release = threading.Event(), threading.Event()
        workers = []
        ui_thread = threading.get_ident()

        def delayed_create(db, **kwargs):
            workers.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test release did not arrive")
            return create_draft(db, **kwargs)

        try:
            with patch("creator_loop.creator_ui.create_draft", delayed_create):
                dialog.body.setPlainText("Worker đang ghi")
                dialog.save_button.click()
                self.assertTrue(entered.wait(2))
                self.assertFalse(dialog.save_button.isEnabled())
                self.assertFalse(dialog.project.isEnabled())
                self.assertFalse(dialog.close())
                dialog.reject()
                self.assertTrue(dialog.isVisible())
                dialog.save_version()
                self.app.processEvents()
                release.set()
                self._wait(dialog)
        finally:
            release.set()
            if dialog._worker is not None:
                self._wait(dialog)
        self.assertEqual(len(workers), 1)
        self.assertNotEqual(workers[0], ui_thread)
        self.assertTrue(dialog.close())

    def test_library_opens_creator_then_exact_claim_version(self):
        window = LibraryWindow(self.root)
        self.addCleanup(window.close)
        opened = []

        def use_creator(dialog):
            dialog.project.setCurrentIndex(dialog.project.findData(self.project))
            self._create(dialog)
            dialog.citations.setCurrentCell(0, 0)
            dialog.open_claim_button.click()
            return CreatorDialog.DialogCode.Accepted

        def read_claim(dialog):
            opened.append(dialog.selected_version_id())
            self.assertEqual(dialog.statement.toPlainText(), "Có Café")
            return ClaimDialog.DialogCode.Rejected

        with (
            patch.object(CreatorDialog, "exec", use_creator),
            patch.object(ClaimDialog, "exec", read_claim),
        ):
            window.choose_creator()
        self.assertEqual(opened, [self.claim.claim_version_id])
