"""Exercise real Qt selection writes, historical reopening and exact navigation."""

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
    from creator_loop.creator_ui import CreatorDialog
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.drafts import append_draft_version, create_draft
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.projects import archive_project, create_project
    from creator_loop.selection_ui import SelectionDialog
    from creator_loop.selections import list_selections, select_draft


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class SelectionDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Selection dữ liệu Huế"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        with closing(_connect_write(self.path)) as db:
            self.project = create_project(db, title="Huế").project_id
            self.other = create_project(db, title="Other").project_id
            self.versions = tuple(
                create_draft(
                    db,
                    project_id=self.project,
                    body_text=body,
                    format="POST",
                    actor="author",
                ).draft_version_id
                for body in ("A — góc kể", "B — Café 🏞", "C — góc khác")
            )
            self.foreign = create_draft(
                db,
                project_id=self.other,
                body_text="foreign",
                format="POST",
                actor="author",
            ).draft_version_id

    def _dialog(self):
        dialog = SelectionDialog(self.root)
        self.addCleanup(dialog.close)
        dialog.project.setCurrentIndex(dialog.project.findData(self.project))
        return dialog

    def _fill(self, dialog):
        for i in range(dialog.candidates.count()):
            item = dialog.candidates.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in self.versions:
                item.setCheckState(Qt.CheckState.Checked)
        dialog.selected.setCurrentIndex(dialog.selected.findData(self.versions[1]))
        dialog.actor.setText("editor Huế")
        dialog.reason.setText("Chọn B — giữ góc kể 🏞")
        self.assertTrue(dialog.save_button.isEnabled())

    def _wait(self, dialog):
        deadline = time.monotonic() + 8
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker, "Selection worker did not finish")
        self.app.processEvents()

    def test_save_a_b_c_and_reopen_exact_history_after_draft_correction(self):
        dialog = self._dialog()
        self._fill(dialog)
        dialog.save_button.click()
        self._wait(dialog)
        with closing(open_readonly(self.path)) as db:
            decision = list_selections(db, project_id=self.project)[0]
            self.assertEqual(decision.selected_draft_version_id, self.versions[1])
            self.assertEqual(set(decision.candidate_version_ids), set(self.versions))
            self.assertEqual(
                (decision.actor_id, decision.reason_text),
                ("editor Huế", "Chọn B — giữ góc kể 🏞"),
            )
        with closing(_connect_write(self.path)) as db:
            append_draft_version(
                db,
                draft_version_id=self.versions[1],
                body_text="B sửa mới",
                format="POST",
                actor="editor",
            )
        reopened = self._dialog()
        reopened.history.selectRow(0)
        self.assertEqual(reopened.selected.currentData(), self.versions[1])
        self.assertEqual(set(reopened.candidate_ids()), set(self.versions))
        self.assertEqual(reopened.preview.toPlainText(), "B — Café 🏞")
        self.assertEqual(reopened.reason.text(), decision.reason_text)
        self.assertFalse(reopened.save_button.isEnabled())
        self.assertFalse(reopened.candidates.isEnabled())
        reopened.new_button.click()
        self.assertTrue(reopened.candidates.isEnabled())
        self.assertFalse(reopened.candidate_ids())

    def test_only_checked_same_project_candidates_and_actor_allow_saving(self):
        dialog = self._dialog()
        ids = {
            dialog.candidates.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(dialog.candidates.count())
        }
        self.assertNotIn(self.foreign, ids)
        self.assertFalse(dialog.save_button.isEnabled())
        self._fill(dialog)
        dialog.actor.clear()
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.save()
        with closing(open_readonly(self.path)) as db:
            self.assertFalse(list_selections(db, project_id=self.project))
        dialog.actor.setText("editor")
        for i in range(dialog.candidates.count()):
            item = dialog.candidates.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == self.versions[1]:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(dialog.selected.findData(self.versions[1]), -1)
        self.assertEqual(dialog.selected.count(), 2)

    def test_archived_project_and_draft_history_remains_readable_without_new_write(
        self,
    ):
        with closing(_connect_write(self.path)) as db:
            select_draft(
                db,
                project_id=self.project,
                candidate_version_ids=self.versions,
                selected_draft_version_id=self.versions[1],
                actor="editor",
                reason=None,
            )
            db.execute(
                "UPDATE drafts SET status='ARCHIVED' WHERE project_id=?",
                (self.project,),
            )
            db.commit()
            archive_project(db, project_id=self.project)
        dialog = self._dialog()
        dialog.history.selectRow(0)
        self.assertEqual(dialog.preview.toPlainText(), "B — Café 🏞")
        self.assertEqual(set(dialog.candidate_ids()), set(self.versions))
        self.assertEqual(dialog.reason.text(), "")
        self.assertFalse(dialog.new_button.isEnabled())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(dialog.open_button.isEnabled())

    def test_write_failure_rolls_back_and_preserves_candidate_choice_actor_reason(self):
        dialog = self._dialog()
        self._fill(dialog)
        with closing(_connect_write(self.path)) as db:
            db.execute(
                "CREATE TRIGGER injected_failure BEFORE INSERT ON selection_event_seals BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with patch("creator_loop.selection_ui.QMessageBox.warning"):
            dialog.save_button.click()
            self._wait(dialog)
        with closing(open_readonly(self.path)) as db:
            for table in (
                "selection_events",
                "selection_candidates",
                "selection_event_seals",
            ):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )
        self.assertEqual(set(dialog.candidate_ids()), set(self.versions))
        self.assertEqual(dialog.selected.currentData(), self.versions[1])
        self.assertEqual(dialog.actor.text(), "editor Huế")
        self.assertEqual(dialog.reason.text(), "Chọn B — giữ góc kể 🏞")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIn("injected", dialog.status.text())

    def test_real_worker_uses_own_thread_and_busy_dialog_cannot_close(self):
        dialog = self._dialog()
        dialog.show()
        self._fill(dialog)
        started, release = threading.Event(), threading.Event()
        observed = {}

        def delayed(db, **kwargs):
            observed["thread"] = threading.get_ident()
            started.set()
            if not release.wait(5):
                raise RuntimeError("test gate timeout")
            return select_draft(db, **kwargs)

        with patch("creator_loop.selection_ui.select_draft", side_effect=delayed):
            try:
                dialog.save_button.click()
                self.assertTrue(started.wait(2))
                self.assertNotEqual(observed["thread"], threading.get_ident())
                self.assertFalse(dialog.project.isEnabled())
                self.assertFalse(dialog.candidates.isEnabled())
                dialog.close()
                dialog.reject()
                self.assertTrue(dialog.isVisible())
            finally:
                release.set()
                self._wait(dialog)
        self.assertEqual(dialog.history.rowCount(), 1)

    def test_read_failure_after_commit_disables_writes_until_successful_reload(self):
        dialog = self._dialog()
        self._fill(dialog)
        with (
            patch(
                "creator_loop.selection_ui.open_readonly",
                side_effect=OSError("read unavailable"),
            ),
            patch("creator_loop.selection_ui.QMessageBox.warning"),
        ):
            dialog.save_button.click()
            self._wait(dialog)
        self.assertFalse(dialog._loaded)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog.open_button.isEnabled())
        self.assertIn("Đã ghi event", dialog.status.text())
        self.assertNotIn("Đã lưu quyết định", dialog.status.text())
        with closing(open_readonly(self.path)) as db:
            decisions = list_selections(db, project_id=self.project)
        self.assertEqual(len(decisions), 1)
        self.assertTrue(dialog.reload(decisions[0].selection_event_id))
        self.assertEqual(dialog.history.rowCount(), 1)

    def test_library_selection_opens_exact_historical_creator_version(self):
        with closing(_connect_write(self.path)) as db:
            select_draft(
                db,
                project_id=self.project,
                candidate_version_ids=self.versions,
                selected_draft_version_id=self.versions[1],
                actor="editor",
            )
            append_draft_version(
                db,
                draft_version_id=self.versions[1],
                body_text="B corrected",
                format="POST",
                actor="editor",
            )
        observed = []

        def choose(dialog):
            dialog.project.setCurrentIndex(dialog.project.findData(self.project))
            dialog.history.selectRow(0)
            dialog.open_button.click()
            return 0

        def creator(dialog):
            observed.append(
                (
                    dialog.project.currentData(),
                    dialog.selected_version_id(),
                    dialog.current_body(),
                    dialog.body.isReadOnly(),
                )
            )
            return 0

        window = LibraryWindow(self.root)
        self.addCleanup(window.close)
        with (
            patch.object(SelectionDialog, "exec", choose),
            patch.object(CreatorDialog, "exec", creator),
        ):
            window.choose_selection()
        self.assertEqual(
            observed, [(self.project, self.versions[1], "B — Café 🏞", True)]
        )


if __name__ == "__main__":
    unittest.main()
