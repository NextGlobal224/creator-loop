"""Cancel actual owned decoder from Library and close only after cleanup."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.database import initialize
    from creator_loop.library_ui import (
        LibraryWindow,
        OriginalImportWorker,
        VideoEvidenceWorker,
    )
    from creator_loop.media_intake import intake_video_original
    from creator_loop.owned_process import OwnedWindowsProcess, _api
    from PySide6.QtWidgets import QApplication, QMessageBox


@unittest.skipUnless(sys.platform == "win32", "native Qt Library/Job cancellation")
class LibraryMediaCancelTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Cancel Hue space"
        self.root.mkdir()
        initialize(self.root / "creator_loop.sqlite3")
        self.fixture = Path(__file__).parent / "fixtures/video-red-blue.mp4"
        self.original = intake_video_original(self.fixture, root=self.root)
        self.window = LibraryWindow(self.root)
        self.addCleanup(self.window.close)
        self.window.show()
        self.kernel = _api()
        self.monitor = None
        self.process = None

    def wait_until(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def start_sleeping_decoder(self, executable, arguments, logs, **kwargs):
        process = OwnedWindowsProcess(
            executable, ["-c", "import time; time.sleep(30)"], logs, **kwargs
        )
        self.monitor = self.kernel.OpenProcess(0x00101000, False, process.record["pid"])
        self.assertTrue(self.monitor)
        self.addCleanup(self.kernel.CloseHandle, self.monitor)
        self.process = process
        return process

    def run_cancel(self, close, import_video=False):
        before_files = {
            file.name: file.read_bytes()
            for file in (self.root / "storage/originals").iterdir()
        }
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )

        def close_sentinel():
            if sentinel.poll() is None:
                sentinel.terminate()
            sentinel.wait(timeout=5)

        self.addCleanup(close_sentinel)
        worker = VideoEvidenceWorker(
            "create",
            self.original.file_id,
            self.root,
            start_ms=0,
            end_ms=500,
            content="cancel fixture",
            actor="fixture",
        )
        if import_video:
            worker = OriginalImportWorker("VIDEO", self.fixture, self.root)
        self.addCleanup(
            lambda: (
                (worker.requestInterruption(), worker.wait(10000))
                if worker.isRunning()
                else None
            )
        )
        after = []
        worker.failed.connect(self.window._on_failed)
        with (
            patch(
                "creator_loop.isolated_decode.OwnedWindowsProcess",
                self.start_sleeping_decoder,
            ),
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.window._start_worker(worker, "Decode fixture", lambda: after.append(1))
            self.wait_until(lambda: self.process is not None)
            self.assertTrue(self.window.cancel_media_button.isEnabled())
            if close:
                self.assertFalse(self.window.close())
                self.assertTrue(self.window.isVisible())
            else:
                self.window.cancel_media_button.click()
                self.assertFalse(self.window.cancel_media_button.isEnabled())
            self.wait_until(lambda: self.window._worker is None)
            warning.assert_not_called()
        self.assertEqual(self.kernel.WaitForSingleObject(self.monitor, 0), 0)
        self.assertIsNone(sentinel.poll())
        self.assertIsNone(self.process.job)
        self.assertEqual(list((self.root / "runtime").glob("decode-*")), [])
        with closing(sqlite3.connect(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0
            )
            key = db.execute(
                "SELECT storage_key FROM asset_files WHERE file_id=?",
                (self.original.file_id,),
            ).fetchone()[0]
        self.assertEqual((self.root / key).read_bytes(), self.fixture.read_bytes())
        self.assertEqual(
            {
                file.name: file.read_bytes()
                for file in (self.root / "storage/originals").iterdir()
            },
            before_files,
        )
        self.assertEqual(after, [] if close else [1])
        self.assertEqual(self.window.isVisible(), not close)

    def test_cancel_button_stops_owned_decoder_without_evidence_or_original_change(
        self,
    ):
        self.run_cancel(False)

    def test_window_close_requests_cancel_and_waits_for_cleanup_without_result_dialog(
        self,
    ):
        self.run_cancel(True)

    def test_cancel_video_intake_discards_only_unregistered_owned_copy(self):
        self.run_cancel(False, import_video=True)

    def test_close_waits_for_real_native_thread_exit_after_finished_signal(self):
        # Keep native Python callbacks at OS thread teardown out of the suite
        # host. The child retains the real FLS/native/GUI assertions.
        fixture = Path(__file__).parent / "fixtures/check_thread_tail.py"
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-u", str(fixture)],
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Native thread exit gate PASS", result.stdout)
