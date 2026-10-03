"""Qt maintenance controller with real native process deadlines and cancellation."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.database import initialize
    from creator_loop.maintenance_process import MAX_OUTPUT_BYTES, MaintenanceCommand
    from creator_loop.paths import ensure_data_root
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Requires Windows and Qt")
class MaintenanceProcessTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "User data Huế"
        ensure_data_root(self.root)
        initialize(self.root / "creator_loop.sqlite3")
        self.command = MaintenanceCommand(self.root)
        self.addCleanup(self.command.close)
        self.results = []
        self.failures = []
        self.command.finished.connect(lambda *values: self.results.append(values))
        self.command.failed.connect(self.failures.append)

    def wait(self):
        deadline = time.monotonic() + 15
        while self.command.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.command.busy)
        self.app.processEvents()

    def test_real_readonly_health_command_completes_without_freezing_events(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        self.addCleanup(timer.stop)
        self.command.start(["--health-check"])
        self.wait()
        self.assertTrue(ticks)
        self.assertFalse(self.failures)
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0][0], 0)
        self.assertIn('"schema_version": 6', self.results[0][1])
        self.assertTrue((self.command.log_directory / "ownership.json").is_file())

    def test_real_timeout_and_cancel_close_retained_native_job(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.failures.clear()
                with patch.object(sys, "frozen", True, create=True):
                    self.command.start(
                        [
                            "-c",
                            "import time; print('ready', flush=True); time.sleep(60)",
                        ],
                        timeout_seconds=0.5 if not cancel else 60,
                    )
                process = self.command.process
                if cancel:
                    QTimer.singleShot(100, self.command.cancel)
                self.wait()
                self.assertIsNone(process.job)
                self.assertIsNone(process.process)
                self.assertEqual(len(self.failures), 1)
                self.assertIn("cancelled" if cancel else "timed out", self.failures[0])
                self.assertFalse(self.results)

    def test_refusal_and_bounded_output_do_not_report_success(self):
        self.command.start(["--apply-restore", "not-a-confirmed-backup"])
        self.wait()
        self.assertEqual(self.results[0][0], 2)
        self.results.clear()
        with patch.object(sys, "frozen", True, create=True):
            self.command.start(
                [
                    "-c",
                    f"import time; print('x'*{MAX_OUTPUT_BYTES + 1}, flush=True); time.sleep(60)",
                ]
            )
        self.wait()
        self.assertFalse(self.results)
        self.assertTrue(self.failures)

    def test_invalid_deadline_and_duplicate_start_do_not_create_another_process(self):
        for timeout in (0, -1, float("inf"), 601):
            with self.assertRaises(ValueError):
                self.command.start(["--health-check"], timeout_seconds=timeout)
        self.assertEqual(list((self.root / "logs").iterdir()), [])
        self.command.start(["--health-check"])
        process = self.command.process
        with self.assertRaises(RuntimeError):
            self.command.start(["--health-check"])
        self.assertIs(self.command.process, process)
        self.wait()
