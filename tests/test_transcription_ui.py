"""Actual Qt task UI, cooperative close and app-lock handoff/reopen."""

import json
import sys
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    import test_transcription_task as task_fixture
    from creator_loop import __main__ as launcher
    from creator_loop.app_lock import AppDataLock, DataRootBusy
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.transcription_ui import LIBRARY_REQUESTED, TranscriptionWindow


@unittest.skipUnless(sys.platform == "win32", "native Qt/I/O worker integration")
class TranscriptionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        task_fixture.TranscriptionTaskTests.setUpClass.__func__(cls)

    query = (
        task_fixture.TranscriptionTaskTests.query if sys.platform == "win32" else None
    )
    select = (
        task_fixture.TranscriptionTaskTests.select if sys.platform == "win32" else None
    )
    until = (
        task_fixture.TranscriptionTaskTests.until if sys.platform == "win32" else None
    )
    engine_started = (
        task_fixture.TranscriptionTaskTests.engine_started
        if sys.platform == "win32"
        else None
    )

    def setUp(self):
        task_fixture.TranscriptionTaskTests.setUp(self)
        self.window = TranscriptionWindow(
            self.root, self.original.file_id, "Video thử nghiệm"
        )
        self.addCleanup(self.window.command.shutdown)
        self.addCleanup(self.window.close)
        self.command = self.window.command
        self.command.finished.connect(
            lambda code, text: self.results.append((code, text))
        )
        self.command.failed.connect(self.failures.append)

    def test_actual_window_run_and_reopen_request_keeps_machine_text_unreviewed(self):
        self.select()
        self.window.show()
        self.window._start()
        self.assertFalse(self.window.library_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.until(lambda: not self.command.busy)
        self.assertIn("chép lời máy đã lưu", self.window.status.text())
        self.assertIn("Chưa tạo hoặc duyệt Evidence", self.window.status.text())
        self.assertEqual(self.query("SELECT COUNT(*) FROM evidence_versions"), [(0,)])
        self.window._library()
        self.assertTrue(self.window.library_requested)
        self.assertFalse(self.window.isVisible())

    def test_actual_window_close_cooperatively_cancels_before_hiding(self):
        self.select(hang=True)
        self.window.show()
        self.window._start()
        self.until(self.engine_started)
        started = time.perf_counter()
        self.assertFalse(self.window.close())
        self.assertLess(time.perf_counter() - started, 0.2)
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.close_pending)
        self.until(lambda: not self.command.busy and not self.window.isVisible())
        self.assertIn("đã hủy", self.window.status.text())
        self.assertFalse(self.window.library_requested)
        self.assertEqual(
            self.query(
                "SELECT status FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("CANCELLED",)],
        )

    def test_worker_budget_mismatch_refuses_before_creating_processing_run(self):
        self.select()
        self.window.worker_budget.setCurrentIndex(0)
        self.window._start()
        self.until(lambda: not self.command.busy)
        self.assertIn("budget", self.window.status.text())
        self.assertEqual(self.query("SELECT COUNT(*) FROM processing_runs"), [(0,)])

    def test_invalid_or_stale_result_never_reports_success(self):
        self.command.request_id = "a" * 32
        self.window._finished(
            0,
            json.dumps(
                {
                    "format": 1,
                    "event": "result",
                    "request_id": "b" * 32,
                    "status": "SUCCEEDED",
                }
            ),
        )
        self.assertIn("Chưa xác nhận", self.window.status.text())
        self.assertNotIn("chép lời máy đã lưu", self.window.status.text())

    def test_library_selection_closes_without_starting_worker_under_library_lock(self):
        with AppDataLock(self.root):
            window = LibraryWindow(self.root)
            self.addCleanup(window.close)
            window.show()
            window.table.selectRow(0)
            window.choose_transcription()
            self.assertEqual(window.transcription_selection[0], self.original.file_id)
            self.assertIsNone(window._worker)
            self.assertFalse(window.isVisible())
        self.assertEqual(self.query("SELECT COUNT(*) FROM processing_runs"), [(0,)])

    def test_launcher_releases_lock_for_task_then_reacquires_for_library(self):
        calls = []

        def library(args, root, *, coordination):
            self.assertTrue(coordination.held)
            with self.assertRaises(DataRootBusy):
                with AppDataLock(root):
                    pass
            calls.append("library")
            if len(calls) == 1:
                args.transcription_selection = (
                    self.original.file_id,
                    "Video thử nghiệm",
                )
                return launcher.TRANSCRIPTION_REQUESTED
            return 0

        def task(root, file_id, name):
            with AppDataLock(root) as lock:
                self.assertTrue(lock.held)
            self.assertEqual(file_id, self.original.file_id)
            calls.append("task")
            return LIBRARY_REQUESTED

        with (
            patch.object(sys, "argv", ["CreatorLoop"]),
            patch.object(launcher, "data_root", return_value=self.root),
            patch.object(launcher, "_run", side_effect=library),
            patch("creator_loop.transcription_ui.run_transcription", side_effect=task),
        ):
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(calls, ["library", "task", "library"])
