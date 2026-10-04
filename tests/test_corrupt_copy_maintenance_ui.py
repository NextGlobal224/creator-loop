"""Real owned CLI review/resume through Qt maintenance and explicit consents."""

import json
import os
import time
import unittest
from unittest.mock import patch

import test_corrupt_copy_resume as resume_fixture
from creator_loop.restore_assessment import assessment_identity

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Qt/native maintenance")
class CorruptCopyMaintenanceUITests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.fixture = resume_fixture.CorruptCopyResumeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window = MaintenanceWindow(self.fixture.root, self.fixture.installation)
        self.addCleanup(self.window.close)

    def select(self, phase):
        self.fixture.crash_copy(phase)
        self.window.journal.setText(str(self.fixture.copy))

    def wait(self):
        deadline = time.monotonic() + 20
        while self.window.command.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.command.busy, self.window.status.text())
        self.app.processEvents()

    def review(self):
        self.window.buttons["review-corrupt-copy"].click()
        self.wait()
        self.assertIsNotNone(self.window.copy_review, self.window.status.text())

    def test_actual_review_missing_source_shows_backup_time_and_no_default_consent(
        self,
    ):
        self.select("CORRUPT_SOURCE_RETAINED")
        self.review()
        review = self.window.copy_review
        self.assertIn(review["backup_created_at"], self.window.review.toPlainText())
        self.assertIn(review["backup_id"], self.window.review.toPlainText())
        self.assertFalse(self.fixture.fixture.source.exists())
        for checkbox in (
            self.window.loss_consent,
            self.window.media_consent,
            self.window.empty_consent,
            self.window.unknown_consent,
            self.window.health_consent,
        ):
            self.assertFalse(checkbox.isChecked())
        self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
        self.assertFalse(self.window.buttons["recover-corrupt-copy"].isEnabled())
        self.window.loss_consent.setChecked(True)
        self.assertTrue(self.window.buttons["resume-corrupt-copy"].isEnabled())
        with patch.object(self.window.command, "start") as start:
            self.window.buttons["resume-corrupt-copy"].click()
        args = start.call_args.args[0]
        self.assertIn(review["inspection"]["inspection_identity"], args)
        self.assertIn("--confirm-lost-changes", args)
        self.assertNotIn("--confirm-recovery", args)
        self.assertIsNone(self.window.copy_review)
        self.assertFalse(self.window.loss_consent.isChecked())
        self.fixture.assert_guarded()

    def test_real_empty_resume_retains_target_then_requires_fresh_review_and_health_consent(
        self,
    ):
        self.select("CORRUPT_COPY_STARTED")
        self.review()
        self.window.loss_consent.setChecked(True)
        self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
        self.window.empty_consent.setChecked(True)
        self.assertTrue(self.window.buttons["resume-corrupt-copy"].isEnabled())
        self.window.buttons["resume-corrupt-copy"].click()
        self.wait()
        self.assertIsNone(self.window.copy_review)
        receipt = json.loads(self.window.review.toPlainText())
        self.assertTrue(receipt["guard_retained"])
        record = json.loads(
            (self.fixture.root / "manifests" / receipt["journal_name"]).read_text()
        )
        self.assertEqual(
            (
                self.fixture.root
                / "backups"
                / record["retained_partial_directory"]
                / "creator_loop.sqlite3"
            ).read_bytes(),
            b"",
        )
        self.review()
        self.assertEqual(
            self.window.copy_review["inspection"]["actual_state"],
            "VALIDATED_COPY_GUARDED",
        )
        self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
        self.assertFalse(self.window.buttons["recover-corrupt-copy"].isEnabled())
        self.window.health_consent.setChecked(True)
        with patch.object(self.window.command, "start") as start:
            self.window.buttons["recover-corrupt-copy"].click()
        self.assertIn("--confirm-recovery", start.call_args.args[0])
        self.assertNotIn("--confirm-lost-changes", start.call_args.args[0])
        self.assertFalse(self.window.health_consent.isChecked())
        self.fixture.assert_guarded()

    def test_unknown_bundle_needs_separate_consent_and_actual_cli_keeps_every_file(
        self,
    ):
        bundle = self.fixture.unknown_bundle()
        self.window.journal.setText(str(self.fixture.copy))
        self.review()
        self.window.loss_consent.setChecked(True)
        self.window.empty_consent.setChecked(True)
        self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
        self.window.unknown_consent.setChecked(True)
        self.window.buttons["resume-corrupt-copy"].click()
        self.wait()
        receipt = json.loads(self.window.review.toPlainText())
        record = json.loads(
            (self.fixture.root / "manifests" / receipt["journal_name"]).read_text()
        )
        directory = self.fixture.root / "backups" / record["retained_partial_directory"]
        self.assertEqual(
            {path.name: path.read_bytes() for path in directory.iterdir()}, bundle
        )
        self.assertFalse(record["consistent_partial_backup"])
        self.fixture.assert_guarded()

    def test_changed_selection_and_invalid_output_clear_proof_and_every_consent(self):
        self.select("CORRUPT_COPY_STARTED")
        self.review()
        review = self.window.copy_review
        self.window.loss_consent.setChecked(True)
        self.window.empty_consent.setChecked(True)
        self.window.journal.setText(str(self.fixture.copy) + ".foreign")
        self.assertIsNone(self.window.copy_review)
        self.assertFalse(self.window.loss_consent.isChecked())
        self.assertFalse(self.window.empty_consent.isChecked())
        self.window.journal.setText(str(self.fixture.copy))
        for bad in (
            "not json",
            "[]",
            '{"inspection":[]}',
            json.dumps({**review, "assessment_identity": "0" * 64}),
        ):
            self.window.operation = "review-corrupt-copy"
            self.window.pending_selection = self.window._selection()
            self.window._finished(0, bad, "")
            self.assertIsNone(self.window.copy_review)
            self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
        foreign = {**review, "data_root_identity": "f" * 64}
        foreign["assessment_identity"] = assessment_identity(foreign)
        self.window.operation = "review-corrupt-copy"
        self.window.pending_selection = self.window._selection()
        self.window._finished(0, json.dumps(foreign), "")
        self.assertIsNone(self.window.copy_review)
        self.fixture.assert_guarded()

    def test_media_issue_blocks_health_even_after_earlier_loss_acknowledgement(self):
        self.select("CORRUPT_COPY_STARTED")
        media = self.fixture.fixture.fixture.fixture.media
        original = media.read_bytes()
        media.unlink()
        try:
            self.review()
            self.assertEqual(self.window.copy_review["media_issue_count"], 1)
            self.window.loss_consent.setChecked(True)
            self.window.empty_consent.setChecked(True)
            self.assertFalse(self.window.buttons["resume-corrupt-copy"].isEnabled())
            self.window.media_consent.setChecked(True)
            self.assertTrue(self.window.buttons["resume-corrupt-copy"].isEnabled())
            self.window.buttons["resume-corrupt-copy"].click()
            self.wait()
            self.review()
            self.window.health_consent.setChecked(True)
            self.assertFalse(self.window.buttons["recover-corrupt-copy"].isEnabled())
            self.assertFalse(media.exists())
        finally:
            media.write_bytes(original)
        self.fixture.assert_guarded()
