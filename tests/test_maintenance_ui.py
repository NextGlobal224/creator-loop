"""Real Qt review/default consent, guarded startup and app-lock handoff."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import test_update_preparation as preparation_fixture
    from creator_loop.app_lock import AppDataLock
    from creator_loop.maintenance_ui import MaintenanceWindow
    from creator_loop.restore_assessment import assess_restore, assessment_identity
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Requires Windows and Qt")
class MaintenanceUITests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepared = self.fixture._prepare()
        self.record = json.loads(prepared.read_text())
        self.window = MaintenanceWindow(self.fixture.root, self.fixture.installation)
        self.window.candidate.setText(self.record["candidate_directory"])
        self.window.backup_id.setText(self.record["backup_id"])
        self.addCleanup(self.window.close)

    def wait(self):
        deadline = time.monotonic() + 15
        while self.window.command.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.command.busy)
        self.app.processEvents()

    def inspect(self):
        self.window.buttons["inspect-restore"].click()
        self.wait()
        self.assertIsNotNone(self.window.assessment, self.window.status.text())

    def test_real_inspection_shows_backup_time_and_never_defaults_to_consent(self):
        self.inspect()
        self.assertIn(
            self.window.assessment["backup_created_at"],
            self.window.review.toPlainText(),
        )
        self.assertFalse(self.window.loss_consent.isChecked())
        self.assertFalse(self.window.media_consent.isChecked())
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.loss_consent.setChecked(True)
        self.assertTrue(self.window.apply_button.isEnabled())
        selected_proof = self.window.assessment["assessment_identity"]
        with patch.object(self.window.command, "start") as start:
            self.window.apply_button.click()
        args = start.call_args.args[0]
        self.assertIn("--confirm-lost-changes", args)
        self.assertIn(selected_proof, args)
        self.assertNotIn("--confirm-media-issues", args)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertIsNone(self.window.assessment)

    def test_media_issues_need_separate_explicit_acknowledgement(self):
        self.fixture.media.unlink()
        self.inspect()
        self.assertEqual(self.window.assessment["media_issue_count"], 1)
        self.window.loss_consent.setChecked(True)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.media_consent.setChecked(True)
        self.assertTrue(self.window.apply_button.isEnabled())
        with patch.object(self.window.command, "start") as start:
            self.window.apply_button.click()
        self.assertIn("--confirm-media-issues", start.call_args.args[0])

    def test_changed_selection_invalidates_review_and_both_consents(self):
        self.inspect()
        self.window.loss_consent.setChecked(True)
        self.window.candidate.setText(self.record["candidate_directory"] + "-other")
        self.assertIsNone(self.window.assessment)
        self.assertFalse(self.window.loss_consent.isChecked())
        self.assertFalse(self.window.media_consent.isChecked())
        self.assertFalse(self.window.apply_button.isEnabled())

    def test_foreign_root_or_bad_output_does_not_grant_review(self):
        review = assess_restore(
            self.fixture.root,
            self.record["backup_id"],
            self.fixture.installation,
            Path(self.record["candidate_directory"]),
        )
        review["data_root_identity"] = "f" * 64
        review["assessment_identity"] = assessment_identity(review)
        self.window.operation = "inspect-restore"
        self.window.pending_selection = self.window._selection()
        self.window._finished(0, json.dumps(review), "")
        self.assertIsNone(self.window.assessment)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.operation = "inspect-restore"
        self.window._finished(0, "not JSON", "")
        self.assertIsNone(self.window.assessment)
        self.window._finished(4, "", "x" * 5000)
        self.assertLess(len(self.window.status.text()), 1000)
        self.assertFalse(self.window.apply_button.isEnabled())

    def test_update_and_resume_require_upgrade_consent_and_changing_path_resets_it(
        self,
    ):
        self.window.zip_path.setText("selected.zip")
        self.window.manifest_path.setText("selected.json")
        self.window.journal.setText("selected-update.json")
        self.assertFalse(self.window.buttons["prepare-update"].isEnabled())
        self.assertFalse(self.window.buttons["resume-update"].isEnabled())
        self.window.upgrade_consent.setChecked(True)
        self.assertTrue(self.window.buttons["prepare-update"].isEnabled())
        with patch.object(self.window.command, "start") as start:
            self.window.buttons["prepare-update"].click()
        self.assertEqual(
            start.call_args.args[0][:2], ["--prepare-update", "selected.zip"]
        )
        self.assertFalse(self.window.upgrade_consent.isChecked())
        self.window.upgrade_consent.setChecked(True)
        self.window.zip_path.setText("different.zip")
        self.assertFalse(self.window.upgrade_consent.isChecked())

    def test_window_close_stops_only_its_retained_command(self):
        with patch.object(sys, "frozen", True, create=True):
            self.window.command.start(["-c", "import time; time.sleep(60)"])
        process = self.window.command.process
        self.window.close()
        self.assertFalse(self.window.command.busy)
        self.assertIsNone(process.job)

    def test_guarded_corrupt_db_can_open_maintenance_without_initialize(self):
        path = self.fixture.path
        path.write_bytes(b"corrupt fixture preserved")
        marker = self.fixture.root / "runtime/restore-in-progress.json"
        marker.write_text("{}")
        env = os.environ.copy()
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.fixture.root)
        for flags, expected in (
            (["--maintenance", "--ui-smoke"], 0),
            (["--maintenance", "--backup"], 2),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "creator_loop", *flags],
                env=env,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(path.read_bytes(), b"corrupt fixture preserved")
        self.assertTrue(marker.exists())

    def test_library_to_maintenance_releases_app_lock_before_opening_window(self):
        from creator_loop.__main__ import MAINTENANCE_REQUESTED, main

        def maintenance(root, installation):
            with AppDataLock(root):
                return 0

        with (
            patch.dict(os.environ, {"CREATOR_LOOP_DATA_ROOT": str(self.fixture.root)}),
            patch.object(sys, "argv", ["creator-loop", "--smoke"]),
            patch("creator_loop.__main__._run", return_value=MAINTENANCE_REQUESTED),
            patch(
                "creator_loop.maintenance_ui.run_maintenance", side_effect=maintenance
            ) as opened,
        ):
            self.assertEqual(main(), 0)
            opened.assert_called_once()

    def test_real_native_library_event_loop_hands_off_to_maintenance(self):
        code = """
import sys
from unittest.mock import patch
from PySide6.QtCore import QTimer
from creator_loop.app_lock import AppDataLock
from creator_loop.library_ui import LibraryWindow
from creator_loop.maintenance_ui import MaintenanceWindow
from creator_loop.__main__ import main

class AutoLibrary(LibraryWindow):
    def __init__(self, root):
        super().__init__(root)
        QTimer.singleShot(100, self.handoff)
    def handoff(self):
        if self._worker is not None:
            QTimer.singleShot(50, self.handoff)
        else:
            self.maintenance_button.click()

class AutoMaintenance(MaintenanceWindow):
    def __init__(self, root, installation=None):
        with AppDataLock(root):
            pass
        super().__init__(root, installation)
        assert not self.loss_consent.isChecked()
        QTimer.singleShot(200, self.close)

sys.argv = ['creator-loop']
with patch('creator_loop.library_ui.LibraryWindow', AutoLibrary), patch('creator_loop.maintenance_ui.MaintenanceWindow', AutoMaintenance):
    raise SystemExit(main())
"""
        env = os.environ.copy()
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.fixture.root)
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_library_button_closes_before_request_and_refuses_running_worker(self):
        from unittest.mock import Mock

        from creator_loop.library_ui import LibraryWindow

        library = LibraryWindow(self.fixture.root)
        self.addCleanup(library.close)
        deadline = time.monotonic() + 10
        while library._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(library._worker)
        library._worker = Mock()
        library._worker.isRunning.return_value = True
        with patch("creator_loop.library_ui.QMessageBox.information"):
            library.choose_maintenance()
        self.assertFalse(library.maintenance_requested)
        library._worker = None
        library.maintenance_button.click()
        self.assertTrue(library.maintenance_requested)
