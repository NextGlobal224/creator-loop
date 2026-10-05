"""Actual source owned CLI initial damaged restore workflow from Qt."""

import json
import os
import time
import unittest

import test_corrupt_restore_copy as copy_fixture
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy
from creator_loop.restore_assessment import assessment_identity

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.damaged_restore_ui import DamagedRestoreWindow
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Actual Qt/native damaged workflow")
class DamagedRestoreUITests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.installation = self.fixture.fixture.fixture.installation
        self.window = DamagedRestoreWindow(
            self.fixture.root,
            str(self.installation),
            str(self.fixture.fixture.candidate),
            self.fixture.fixture.backup_id,
        )
        self.addCleanup(self.window.close)

    def wait(self):
        deadline = time.monotonic() + 30
        while self.window.command.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.command.busy, self.window.status.text())
        self.app.processEvents()

    def review(self):
        self.window.buttons["review"].click()
        self.wait()
        self.assertIsNotNone(self.window.proof, self.window.status.text())

    def test_actual_review_preserve_stage_copy_pipeline_keeps_bytes_and_returns_guarded_journal(
        self,
    ):
        self.review()
        assessment = self.window.proof["assessment"]
        self.assertIn(assessment["backup_created_at"], self.window.review.toPlainText())
        self.assertFalse(self.window.loss.isChecked())
        self.assertFalse(self.window.buttons["prepare"].isEnabled())
        self.assertFalse(self.window.buttons["copy"].isEnabled())
        self.window.buttons["preserve"].click()
        self.wait()
        raw = self.window.raw.text()
        self.assertTrue(raw)
        self.assertIsNone(self.window.proof)
        self.review()
        self.window.loss.setChecked(True)
        self.window.buttons["prepare"].click()
        self.wait()
        self.assertTrue(self.window.preparation.text(), self.window.status.text())
        self.assertIsNone(self.window.proof)
        self.assertFalse(self.window.loss.isChecked())
        self.review()
        self.assertTrue(self.window.proof["copy_supported"])
        ready = []
        self.window.copy_ready.connect(
            lambda journal, installation: ready.append((journal, installation))
        )
        self.window.loss.setChecked(True)
        self.window.buttons["copy"].click()
        self.wait()
        self.assertEqual(len(ready), 1, self.window.status.text())
        from pathlib import Path

        self.assertEqual(ready[0][1], str(self.installation))
        copy = Path(ready[0][0])
        self.assertEqual(
            inspect_corrupt_copy(self.fixture.root, copy)["actual_state"],
            "VALIDATED_COPY_GUARDED",
        )
        self.assertTrue(
            (self.fixture.root / "runtime/restore-in-progress.json").exists()
        )
        self.fixture.assert_retained_or_original()

    def test_stale_stage_review_stays_copy_disabled_even_with_loss_consent(self):
        prep = json.loads(self.fixture.manifest.read_text())
        self.window.raw.setText(str(self.fixture.root / prep["raw_manifest"]))
        self.window.preparation.setText(str(self.fixture.manifest))
        self.fixture.source.write_bytes(b"PRIVATE NEW DAMAGED SOURCE")
        self.review()
        self.assertFalse(self.window.proof["copy_supported"])
        self.window.loss.setChecked(True)
        self.assertFalse(self.window.buttons["copy"].isEnabled())
        self.assertFalse(self.window.buttons["prepare"].isEnabled())
        self.assertEqual(
            self.fixture.source.read_bytes(), b"PRIVATE NEW DAMAGED SOURCE"
        )

    def test_selection_invalidates_proof_and_missing_bad_or_foreign_review_never_enables_copy(
        self,
    ):
        self.review()
        proof = self.window.proof
        self.window.loss.setChecked(True)
        self.window.candidate.setText(self.window.candidate.text() + ".foreign")
        self.assertIsNone(self.window.proof)
        self.assertFalse(self.window.loss.isChecked())
        self.window.candidate.setText(str(self.fixture.fixture.candidate))
        foreign = {**proof, "data_root_identity": "f" * 64}
        foreign["assessment_identity"] = assessment_identity(foreign)
        for bad in ("[]", "not json", json.dumps(foreign)):
            self.window.operation = "review"
            self.window.pending_selection = self.window._selection()
            self.window._finished(0, bad, "")
            self.assertIsNone(self.window.proof)
            self.assertFalse(self.window.buttons["copy"].isEnabled())

    def test_parent_maintenance_opens_dialog_with_explicit_selection_without_db_init(
        self,
    ):
        before = self.fixture.source.read_bytes()
        parent = MaintenanceWindow(self.fixture.root, self.installation)
        self.addCleanup(parent.close)
        parent.candidate.setText(str(self.fixture.fixture.candidate))
        parent.backup_id.setText(self.fixture.fixture.backup_id)
        parent.buttons["damaged-restore"].click()
        self.app.processEvents()
        dialog = parent.damaged_window
        self.assertIsInstance(dialog, DamagedRestoreWindow)
        self.assertEqual(dialog.backup_id.text(), self.fixture.fixture.backup_id)
        self.assertIsNone(dialog.proof)
        self.assertFalse(dialog.command.busy)
        self.assertEqual(self.fixture.source.read_bytes(), before)

    def test_malformed_media_review_and_escaped_receipt_fail_closed_without_bytes_change(
        self,
    ):
        import copy

        self.review()
        good = self.window.proof
        before = self.fixture.source.read_bytes()
        for media in (
            None,
            [None],
            [{"status": "valid"}],
            [{"status": "missing", "file_id": "x", "role": "ORIGINAL"}],
        ):
            bad = copy.deepcopy(good)
            bad["assessment"]["media_assessment"] = media
            bad["assessment"]["assessment_identity"] = assessment_identity(
                bad["assessment"]
            )
            bad["assessment_identity"] = assessment_identity(bad)
            self.window.operation = "review"
            self.window.pending_selection = self.window._selection()
            self.window._finished(0, json.dumps(bad), "")
            self.assertIsNone(self.window.proof)
        for path in (
            "D:backups/raw-source-" + "a" * 32 + "/raw-source-manifest.json",
            "backups/../foreign.json",
            "manifests/corrupt-restore-" + "a" * 32 + ".json",
        ):
            self.window.operation = "preserve"
            self.window.pending_selection = self.window._selection()
            self.window._finished(
                0,
                json.dumps(
                    {
                        "raw_source_preserved": True,
                        "consistent_backup": False,
                        "restored": False,
                        "restore_authorized": False,
                        "raw_source_manifest": path,
                    }
                ),
                "",
            )
            self.assertEqual(self.window.raw.text(), "")
            self.assertIsNone(self.window.proof)
        self.assertEqual(self.fixture.source.read_bytes(), before)

    def test_missing_media_requires_separate_consent_and_never_recreates_original(self):
        preparation = json.loads(self.fixture.manifest.read_text())
        self.window.raw.setText(str(self.fixture.root / preparation["raw_manifest"]))
        media = self.fixture.root / "storage/originals/text.txt"
        original = media.read_bytes()
        media.unlink()  # Only our private fixture; restore it for existing checks.
        try:
            self.review()
            self.assertGreater(self.window.proof["assessment"]["media_issue_count"], 0)
            self.window.loss.setChecked(True)
            self.assertFalse(self.window.buttons["prepare"].isEnabled())
            self.window.media.setChecked(True)
            self.assertTrue(self.window.buttons["prepare"].isEnabled())
            self.assertFalse(media.exists())
            self.window.raw.clear()
            self.assertFalse(self.window.loss.isChecked())
            self.assertFalse(self.window.media.isChecked())
        finally:
            media.write_bytes(original)

    def test_parent_close_cancels_retained_child_command_without_source_mutation(self):
        before = self.fixture.source.read_bytes()
        parent = MaintenanceWindow(self.fixture.root, self.installation)
        self.addCleanup(parent.close)
        parent.candidate.setText(str(self.fixture.fixture.candidate))
        parent.backup_id.setText(self.fixture.fixture.backup_id)
        parent.buttons["damaged-restore"].click()
        dialog = parent.damaged_window
        dialog.buttons["review"].click()
        self.assertTrue(dialog.command.busy)
        parent.close()
        self.app.processEvents()
        self.assertIsNone(dialog.command.process)
        self.assertIsNone(dialog.proof)
        self.assertEqual(self.fixture.source.read_bytes(), before)
