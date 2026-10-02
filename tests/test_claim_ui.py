"""Exercise Claim editing/review on actual Qt workers and persisted SQLite data."""

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
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claim_ui import ClaimDialog
    from creator_loop.claims import create_claim
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.evidence_correction import correct_text_evidence
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.text_evidence import create_text_evidence
    from creator_loop.text_intake import intake_text_original


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ClaimDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Claim dữ liệu Huế"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        source = Path(self.temp.name) / "Nguồn.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        self.imported = intake_text_original(source, root=self.root)
        with closing(_connect_write(self.db_path)) as db:
            self.evidence = create_text_evidence(
                db,
                file_id=self.imported.file_id,
                data_root=self.root,
                start=0,
                end=4,
                actor="creator",
            )

    def _dialog(self):
        dialog = ClaimDialog(self.root)
        self.addCleanup(dialog.close)
        return dialog

    def _wait(self, dialog):
        deadline = time.monotonic() + 8
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker, "Claim worker did not finish")
        self.app.processEvents()

    def _add(self, dialog, relation="SUPPORTS", version=None):
        index = dialog.evidence.findData(version or self.evidence.evidence_version_id)
        self.assertGreaterEqual(index, 0)
        dialog.evidence.setCurrentIndex(index)
        dialog.relation.setCurrentIndex(dialog.relation.findData(relation))
        self.assertTrue(dialog.add_button.isEnabled())
        dialog.add_button.click()

    def _create(self, dialog):
        self._add(dialog)
        dialog.statement.setPlainText("Café dans nguồn 🏞")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.save_button.click()
        self._wait(dialog)
        self.assertIsNotNone(dialog.selected_version_id())
        return dialog.selected_version_id()

    def test_create_three_relations_correct_review_and_reopen_history(self):
        dialog = self._dialog()
        for relation in ("SUPPORTS", "CONTRADICTS", "CONTEXT", "CONTEXT"):
            self._add(dialog, relation)
        self.assertEqual(dialog.citations.rowCount(), 3)
        dialog.statement.setPlainText("Nhận định tại Huế 🏞")
        dialog.save_button.click()
        self._wait(dialog)
        first = dialog.selected_version_id()
        dialog.citations.setCurrentCell(1, 0)
        dialog.remove_button.click()
        dialog.statement.setPlainText("Nhận định mới")
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.reason.setText("Bổ sung ngữ cảnh")
        dialog.save_button.click()
        self._wait(dialog)
        second = dialog.selected_version_id()
        self.assertNotEqual(first, second)
        for action, reason in (
            ("ACCEPT", ""),
            ("REQUEST_CHANGES", "Cần đối chiếu"),
            ("REOPEN", "Đã xem lại"),
            ("ACCEPT", ""),
        ):
            dialog.review_action.setCurrentIndex(dialog.review_action.findData(action))
            dialog.reason.setText(reason)
            self.assertTrue(dialog.review_button.isEnabled())
            dialog.review_button.click()
            self._wait(dialog)
        self.assertEqual(dialog.history.rowCount(), 4)
        self.assertIn("ACCEPT", dialog.summary.text())
        dialog.version.setCurrentIndex(dialog.version.findData(first))
        self.assertEqual(dialog.statement.toPlainText(), "Nhận định tại Huế 🏞")
        self.assertEqual(dialog.citations.rowCount(), 3)
        self.assertEqual(dialog.history.item(0, 0).text(), "CORRECT")
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM claim_version_seals").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
            )
        dialog.close()
        reopened = self._dialog()
        reopened.version.setCurrentIndex(reopened.version.findData(second))
        self.assertEqual(reopened.statement.toPlainText(), "Nhận định mới")
        self.assertEqual(reopened.citations.rowCount(), 2)
        self.assertEqual(reopened.history.rowCount(), 4)

    def test_stale_evidence_remains_exact_and_open_request_keeps_old_version(self):
        dialog = self._dialog()
        version_id = self._create(dialog)
        with closing(_connect_write(self.db_path)) as db:
            corrected = correct_text_evidence(
                db,
                evidence_version_id=self.evidence.evidence_version_id,
                data_root=self.root,
                start=0,
                end=10,
                actor="editor",
                reason="Wider",
            )
        self.assertTrue(dialog.reload(version_id))
        self.assertIn("STALE 1", dialog.summary.text())
        self.assertIn("STALE", dialog.citations.item(0, 0).text())
        self.assertGreaterEqual(dialog.evidence.findData(corrected.new_version_id), 0)
        opened = []
        dialog.evidence_requested.connect(opened.append)
        dialog.citations.setCurrentCell(0, 0)
        dialog.open_button.click()
        self.assertEqual(opened, [self.evidence.evidence_version_id])

    def test_library_opens_claim_editor_and_reopens_exact_old_text_citation(self):
        window = LibraryWindow(self.root)
        self.addCleanup(window.close)

        def use_claim_dialog(dialog):
            version_id = self._create(dialog)
            with closing(_connect_write(self.db_path)) as db:
                correct_text_evidence(
                    db,
                    evidence_version_id=self.evidence.evidence_version_id,
                    data_root=self.root,
                    start=0,
                    end=10,
                    actor="editor",
                    reason="Wider",
                )
            dialog.reload(version_id)
            dialog.citations.setCurrentCell(0, 0)
            dialog.open_button.click()
            return ClaimDialog.DialogCode.Accepted

        with (
            patch.object(ClaimDialog, "exec", use_claim_dialog),
            patch("creator_loop.library_ui.QMessageBox.information") as preview,
        ):
            window.choose_claim()
            self._wait(window)
        preview.assert_called_once_with(window, "Đoạn Evidence", "Café")
        self.assertEqual(
            window.evidence_table.item(window.evidence_table.currentRow(), 2).text(),
            "1",
        )
        with patch("creator_loop.library_ui.QMessageBox.warning") as warning:
            window.open_claim_evidence("missing")
        warning.assert_called_once()

    def test_deleted_claim_and_evidence_history_are_readonly_and_not_new_targets(self):
        dialog = self._dialog()
        version_id = self._create(dialog)
        with closing(_connect_write(self.db_path)) as db:
            db.execute("UPDATE claims SET deleted_at='now'")
            db.execute("UPDATE evidences SET deleted_at='now'")
            db.commit()
        dialog.reload(version_id)
        self.assertEqual(dialog.citations.rowCount(), 1)
        self.assertIn("đã xóa mềm", dialog.citations.item(0, 0).text())
        self.assertEqual(dialog.evidence.count(), 0)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())

    def test_missing_anchor_blocks_accept_and_preserves_pending_state(self):
        dialog = self._dialog()
        self._create(dialog)
        self.root.joinpath(*self.imported.storage_key.split("/")).unlink()
        with patch("creator_loop.claim_ui.QMessageBox.warning") as warning:
            dialog.review_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertIn("missing file", dialog.status.text())
        self.assertIn("PENDING", dialog.summary.text())
        self.assertEqual(dialog.history.rowCount(), 0)

    def test_failed_correction_keeps_editor_input_and_persisted_old_version(self):
        dialog = self._dialog()
        version_id = self._create(dialog)
        with closing(_connect_write(self.db_path)) as db:
            db.execute(
                "CREATE TRIGGER reject_claim_event BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT,'review blocked'); END"
            )
            db.commit()
        dialog.statement.setPlainText("Bản sửa chưa ghi")
        dialog.reason.setText("Cần bổ sung")
        with patch("creator_loop.claim_ui.QMessageBox.warning") as warning:
            dialog.save_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertEqual(dialog.selected_version_id(), version_id)
        self.assertEqual(dialog.statement.toPlainText(), "Bản sửa chưa ghi")
        self.assertEqual(dialog.reason.text(), "Cần bổ sung")
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM claim_versions").fetchone()[0], 1
            )

    def test_read_failure_after_commit_does_not_report_success_or_enable_writes(self):
        dialog = self._dialog()
        self._add(dialog)
        dialog.statement.setPlainText("Đã ghi nhưng đọc lỗi")
        with (
            patch(
                "creator_loop.claim_ui.open_readonly",
                side_effect=OSError("read unavailable"),
            ),
            patch("creator_loop.claim_ui.QMessageBox.warning") as warning,
        ):
            dialog.save_button.click()
            self._wait(dialog)
        warning.assert_called_once()
        self.assertIn("Không thể đọc Claim", dialog.status.text())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.review_button.isEnabled())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT statement FROM claim_versions").fetchone()[0],
                "Đã ghi nhưng đọc lỗi",
            )
        self.assertTrue(dialog.reload())
        self.assertEqual(dialog.version.count(), 2)

    def test_owned_worker_disables_writes_and_close_without_blocking_ui(self):
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
            return create_claim(db, **kwargs)

        try:
            with patch("creator_loop.claim_ui.create_claim", delayed_create):
                self._add(dialog)
                dialog.statement.setPlainText("Worker đang ghi")
                dialog.save_button.click()
                self.assertTrue(entered.wait(2))
                self.assertFalse(dialog.save_button.isEnabled())
                self.assertFalse(dialog.version.isEnabled())
                self.assertFalse(dialog.open_button.isEnabled())
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
