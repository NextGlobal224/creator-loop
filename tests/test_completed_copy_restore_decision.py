"""Completed copies never reuse interrupted-copy consent after source loss."""

import json
import os
import subprocess
import sys
import unittest

import test_corrupt_restore_copy as copy_fixture
from creator_loop.corrupt_copy_resume import resume_corrupt_copy
from creator_loop.corrupt_copy_review import review_corrupt_copy
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Actual native completed copy/refusal/Qt")
class CompletedCopyRestoreDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation
        self.copy = self.fixture.copy()

    def files(self):
        return {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def test_completed_missing_main_cannot_use_interrupted_loss_consent(self):
        self.fixture.source.unlink()  # Own private closed copied DB, never user data.
        proof = inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(proof["actual_state"], "SOURCE_RETAINED_LIVE_MISSING_GUARDED")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "fresh restore decision"):
            resume_corrupt_copy(
                self.root,
                self.copy,
                self.installation,
                reviewed_inspection=proof["inspection_identity"],
                confirm_lost_changes=True,
                confirm_keep_partial=True,
                confirm_preserve_unknown=True,
            )
        self.assertEqual(self.files(), before)
        self.assertFalse(self.fixture.source.exists())

    def test_completed_empty_main_cannot_use_interrupted_empty_consent(self):
        self.fixture.source.write_bytes(b"")
        proof = inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(proof["actual_state"], "EMPTY_CURRENT_DATABASE_GUARDED")
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "fresh restore decision"):
            resume_corrupt_copy(
                self.root,
                self.copy,
                self.installation,
                reviewed_inspection=proof["inspection_identity"],
                confirm_lost_changes=True,
                confirm_keep_partial=True,
                confirm_preserve_unknown=True,
            )
        self.assertEqual(self.files(), before)
        self.assertEqual(self.fixture.source.read_bytes(), b"")

    def test_actual_cli_completed_missing_or_empty_refuses_without_new_files(self):
        for mode in ("empty", "missing"):
            with self.subTest(mode=mode):
                if mode == "empty":
                    self.fixture.source.write_bytes(b"")
                else:
                    self.fixture.source.unlink()
                proof = inspect_corrupt_copy(self.root, self.copy)
                before = self.files()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "creator_loop",
                        "--resume-corrupt-copy",
                        str(self.copy),
                        "--installation-root",
                        str(self.installation),
                        "--reviewed-inspection",
                        proof["inspection_identity"],
                        "--confirm-lost-changes",
                        "--confirm-keep-partial",
                        "--confirm-preserve-unknown",
                    ],
                    env={
                        **os.environ,
                        "PYTHONPATH": "app",
                        "CREATOR_LOOP_DATA_ROOT": str(self.root),
                    },
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertNotIn(b"PRIVATE", result.stdout + result.stderr)
                self.assertEqual(self.files(), before)

    def test_actual_readonly_review_disables_ui_completed_missing_or_empty(self):
        self.app = QApplication.instance() or QApplication([])
        window = MaintenanceWindow(self.root, self.installation)
        self.addCleanup(window.close)
        window.journal.setText(str(self.copy))
        for mode in ("empty", "missing"):
            with self.subTest(mode=mode):
                if mode == "empty":
                    self.fixture.source.write_bytes(b"")
                else:
                    self.fixture.source.unlink()
                before = self.files()
                review = review_corrupt_copy(self.root, self.copy, self.installation)
                self.assertTrue(review["requires_fresh_restore_decision"])
                self.assertFalse(review["continuation_allowed"])
                self.assertFalse(review["unknown_continuation_allowed"])
                window.operation = "review-corrupt-copy"
                window.pending_selection = window._selection()
                window._finished(0, json.dumps(review), "")
                self.assertIsNotNone(window.copy_review, window.status.text())
                self.assertIn(
                    "cần quyết định khôi phục mới", window.review.toPlainText()
                )
                self.assertFalse(window.loss_consent.isEnabled())
                self.assertFalse(window.empty_consent.isEnabled())
                window.loss_consent.setChecked(True)
                window.empty_consent.setChecked(True)
                window.unknown_consent.setChecked(True)
                self.assertFalse(window.buttons["resume-corrupt-copy"].isEnabled())
                self.assertFalse(window.buttons["recover-corrupt-copy"].isEnabled())
                self.assertEqual(self.files(), before)
