"""Exercise Project workspace references, history and real Qt worker behavior."""

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
        from PySide6.QtWidgets import QApplication, QDialogButtonBox
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claims import EvidenceLink, append_claim_version, create_claim
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.project_ui import ProjectDialog
    from creator_loop.projects import create_project
    from creator_loop.source_association import SourceDetails, create_source_for_asset
    from creator_loop.text_evidence import create_text_evidence
    from creator_loop.text_intake import intake_text_original


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ProjectDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Project dữ liệu Huế"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        source = Path(self.temp.name) / "Tin tại Huế.txt"
        source.write_text("Một tư liệu ở Huế", encoding="utf-8")
        self.imported = intake_text_original(source, root=self.root)
        with closing(_connect_write(self.db_path)) as db:
            evidence = create_text_evidence(
                db,
                file_id=self.imported.file_id,
                data_root=self.root,
                start=0,
                end=10,
                actor="creator",
            )
            self.claim = create_claim(
                db,
                claim_type="FACTUAL",
                statement="Nhận định phiên bản đầu",
                actor="editor",
                links=[EvidenceLink(evidence.evidence_version_id, "SUPPORTS")],
            )
            self.second_claim = append_claim_version(
                db,
                claim_id=self.claim.claim_id,
                statement="Nhận định phiên bản hai",
                actor="editor",
                links=[EvidenceLink(evidence.evidence_version_id, "CONTEXT")],
            )
            self.source_id = create_source_for_asset(
                db,
                asset_id=self.imported.asset_id,
                details=SourceDetails(platform="LOCAL", rights_status="REFERENCE_ONLY"),
                relationship_type="UNKNOWN",
            )

    def _dialog(self):
        dialog = ProjectDialog(self.root)
        self.addCleanup(dialog.close)
        return dialog

    def _wait_worker(self, dialog) -> None:
        deadline = time.monotonic() + 8
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker, "Project worker did not finish")
        self.app.processEvents()

    def _create_project(self, dialog, title="  Chuyện Huế 🏞  ") -> str:
        dialog.title.setText(title)
        self.assertTrue(dialog.create_button.isEnabled())
        dialog.create_button.click()
        self.assertIsNotNone(dialog._worker)
        self._wait_worker(dialog)
        self.assertIsNotNone(dialog.selected_project_id())
        return dialog.selected_project_id()

    def _add(self, dialog, target_type: str, target_id: str, usage: str) -> None:
        dialog.target_type.setCurrentIndex(dialog.target_type.findData(target_type))
        self.assertGreaterEqual(dialog.target.findData(target_id), 0)
        dialog.target.setCurrentIndex(dialog.target.findData(target_id))
        dialog.usage.setCurrentIndex(dialog.usage.findData(usage))
        self.assertTrue(dialog.add_button.isEnabled())
        dialog.add_button.click()
        self._wait_worker(dialog)

    def test_create_three_references_archive_and_reopen_exact_history(self) -> None:
        dialog = self._dialog()
        project_id = self._create_project(dialog)
        self.assertIn("Chuyện Huế 🏞", dialog.project.currentText())
        self.assertEqual(dialog.title.text(), "")
        self._add(dialog, "asset_id", self.imported.asset_id, "RESEARCH")
        self._add(dialog, "source_id", self.source_id, "REUSE_MEDIA")
        self.assertIn("chưa biết URL/ID", dialog.references.item(1, 1).text())
        self._add(dialog, "claim_version_id", self.claim.claim_version_id, "QUOTE")
        self.assertIn("v1", dialog.references.item(2, 1).text())
        self.assertIn("Nhận định phiên bản đầu", dialog.references.item(2, 1).text())
        self.assertEqual(
            dialog.references.item(2, 2).text(), self.claim.claim_version_id
        )
        self.assertEqual(dialog.references.rowCount(), 3)
        dialog.archive_button.click()
        self._wait_worker(dialog)
        self.assertTrue(dialog.include_archived.isChecked())
        self.assertEqual(dialog.selected_project_id(), project_id)
        self.assertIn("ARCHIVED", dialog.project.currentText())
        self.assertFalse(dialog.archive_button.isEnabled())
        self.assertFalse(dialog.add_button.isEnabled())
        self.assertEqual(dialog.references.rowCount(), 3)
        dialog.include_archived.setChecked(False)
        self.assertEqual(dialog.project.count(), 0)
        self.assertEqual(dialog.references.rowCount(), 0)
        dialog.close()

        reopened = self._dialog()
        self.assertEqual(reopened.project.count(), 0)
        reopened.include_archived.setChecked(True)
        self.assertEqual(reopened.selected_project_id(), project_id)
        self.assertEqual(reopened.references.rowCount(), 3)
        self.assertEqual(
            reopened.references.item(2, 2).text(), self.claim.claim_version_id
        )
        self.assertFalse(reopened.add_button.isEnabled())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT title,status FROM projects").fetchone(),
                ("Chuyện Huế 🏞", "ARCHIVED"),
            )
            self.assertEqual(
                db.execute(
                    "SELECT asset_id,claim_version_id,source_id,usage_intent FROM project_references ORDER BY rowid"
                ).fetchall(),
                [
                    (self.imported.asset_id, None, None, "RESEARCH"),
                    (None, None, self.source_id, "REUSE_MEDIA"),
                    (None, self.claim.claim_version_id, None, "QUOTE"),
                ],
            )
            self.assertEqual(
                db.execute("SELECT rights_status FROM sources").fetchone()[0],
                "REFERENCE_ONLY",
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
            )

    def test_live_target_choices_and_soft_deleted_reference_history(self) -> None:
        dialog = self._dialog()
        self._create_project(dialog)
        self._add(dialog, "asset_id", self.imported.asset_id, "RESEARCH")
        self._add(dialog, "claim_version_id", self.claim.claim_version_id, "QUOTE")
        self.assertGreaterEqual(
            dialog.target.findData(self.second_claim.claim_version_id), 0
        )
        self.assertIn(
            "v2",
            dialog.target.itemText(
                dialog.target.findData(self.second_claim.claim_version_id)
            ),
        )
        with closing(_connect_write(self.db_path)) as db:
            db.execute(
                "UPDATE assets SET deleted_at='2026-10-02T00:00:00Z' WHERE asset_id=?",
                (self.imported.asset_id,),
            )
            db.execute(
                "UPDATE claims SET deleted_at='2026-10-02T00:00:00Z' WHERE claim_id=?",
                (self.claim.claim_id,),
            )
            db.execute(
                "INSERT INTO claims(claim_id,claim_type,created_at) VALUES('unfinished','FACTUAL','2026-10-02T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_id,version_no,statement,created_at) VALUES('unsealed','unfinished',1,'Unfinished','2026-10-02T00:00:00Z')"
            )
            db.commit()
        dialog.reload()
        self.assertEqual(dialog.references.rowCount(), 2)
        self.assertIn("đã xóa mềm", dialog.references.item(0, 1).text())
        self.assertIn("đã xóa mềm", dialog.references.item(1, 1).text())
        self.assertEqual(dialog.target.count(), 0)
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.target_type.setCurrentIndex(dialog.target_type.findData("asset_id"))
        self.assertEqual(dialog.target.count(), 0)
        dialog.target_type.setCurrentIndex(dialog.target_type.findData("source_id"))
        self.assertEqual(dialog.target.currentData(), self.source_id)
        self.assertIn("chưa biết URL/ID", dialog.target.currentText())
        self.assertTrue(dialog.add_button.isEnabled())

    def test_invalid_title_and_stale_target_failure_restore_controls(self) -> None:
        dialog = self._dialog()
        self.assertFalse(dialog.create_button.isEnabled())
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.title.setText(" \n ")
        dialog.create_new_project()
        self.assertIsNone(dialog._worker)
        self._create_project(dialog)
        with closing(_connect_write(self.db_path)) as db:
            db.execute(
                "UPDATE assets SET deleted_at='2026-10-02T00:00:00Z' WHERE asset_id=?",
                (self.imported.asset_id,),
            )
            db.commit()
        with patch("creator_loop.project_ui.QMessageBox.warning") as warning:
            dialog.add_button.click()
            self._wait_worker(dialog)
        warning.assert_called_once()
        self.assertIn("Live Asset is required", dialog.status.text())
        self.assertEqual(dialog.references.rowCount(), 0)
        self.assertTrue(dialog.project.isEnabled())
        self.assertTrue(dialog.archive_button.isEnabled())
        self.assertFalse(dialog.add_button.isEnabled())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM project_references").fetchone()[0], 0
            )

    def test_database_write_failure_warns_and_preserves_title(self) -> None:
        with closing(_connect_write(self.db_path)) as db:
            db.execute(
                "CREATE TRIGGER reject_ui_project BEFORE INSERT ON projects BEGIN SELECT RAISE(ABORT,'project blocked'); END"
            )
            db.commit()
        dialog = self._dialog()
        dialog.title.setText("Project thất bại")
        with patch("creator_loop.project_ui.QMessageBox.warning") as warning:
            dialog.create_button.click()
            self._wait_worker(dialog)
        warning.assert_called_once()
        self.assertIn("project blocked", dialog.status.text())
        self.assertEqual(dialog.title.text(), "Project thất bại")
        self.assertTrue(dialog.create_button.isEnabled())
        self.assertIsNone(dialog.selected_project_id())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM projects").fetchone()[0], 0
            )

    def test_read_failure_after_commit_is_not_reported_as_success(self) -> None:
        dialog = self._dialog()
        dialog.title.setText("Project đã ghi")
        with (
            patch(
                "creator_loop.project_ui.list_projects",
                side_effect=OSError("read unavailable"),
            ),
            patch("creator_loop.project_ui.QMessageBox.warning") as warning,
        ):
            dialog.create_button.click()
            self._wait_worker(dialog)
        warning.assert_called_once()
        self.assertIn("Không thể đọc Project", dialog.status.text())
        self.assertIn("read unavailable", dialog.status.text())
        self.assertIsNone(dialog.selected_project_id())
        self.assertFalse(dialog.add_button.isEnabled())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT title FROM projects").fetchone()[0], "Project đã ghi"
            )
        self.assertTrue(dialog.reload())
        self.assertIn("Project đã ghi", dialog.project.currentText())

    def test_worker_disables_mutation_and_prevents_close_until_finished(self) -> None:
        dialog = self._dialog()
        dialog.show()
        self.app.processEvents()
        entered, release = threading.Event(), threading.Event()
        worker_threads: list[int] = []
        ui_thread = threading.get_ident()

        def delayed_create(db, *, title):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release did not arrive")
            return create_project(db, title=title)

        try:
            with patch("creator_loop.project_ui.create_project", delayed_create):
                dialog.title.setText("Project đang ghi")
                dialog.create_button.click()
                self.assertTrue(entered.wait(2))
                self.assertIsNotNone(dialog._worker)
                for widget in (
                    dialog.title,
                    dialog.create_button,
                    dialog.project,
                    dialog.include_archived,
                    dialog.archive_button,
                    dialog.target_type,
                    dialog.target,
                    dialog.usage,
                    dialog.add_button,
                    dialog.buttons.button(QDialogButtonBox.StandardButton.Close),
                ):
                    self.assertFalse(widget.isEnabled())
                dialog.create_new_project()
                self.assertFalse(dialog.close())
                dialog.reject()
                self.assertTrue(dialog.isVisible())
                self.assertIn("Chờ thao tác", dialog.status.text())
                release.set()
                self._wait_worker(dialog)
        finally:
            release.set()
            if dialog._worker is not None:
                self._wait_worker(dialog)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], ui_thread)
        self.assertTrue(dialog.close())
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM projects").fetchone()[0], 1
            )
