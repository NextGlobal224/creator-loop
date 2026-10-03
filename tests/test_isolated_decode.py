"""Actual inherited media handles and native deadline/Qt shutdown behavior."""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.isolated_decode import decode_isolated
    from creator_loop.owned_process import OwnedWindowsProcess, _api
    from creator_loop.windows_owned_file import OwnedWindowsFile
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "requires native Windows Qt decoder")
class IsolatedDecodeTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Decoder Hue space"
        self.root.mkdir()
        self.path = self.root / "locked.mp4"
        self.fixture = Path(__file__).parent / "fixtures/video-red-blue.mp4"
        self.process = None
        self.monitor = None
        self.kernel = _api()
        self.kernel.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self.kernel.OpenProcess.restype = wintypes.HANDLE

    def fake_worker(self, script):
        def start(executable, arguments, logs, **kwargs):
            process = OwnedWindowsProcess(
                executable, ["-u", "-c", script, arguments[-1]], logs, **kwargs
            )
            self.process = process
            self.monitor = self.kernel.OpenProcess(
                0x00101000, False, process.record["pid"]
            )
            self.assertTrue(self.monitor)
            self.addCleanup(self.kernel.CloseHandle, self.monitor)
            return process

        return patch("creator_loop.isolated_decode.OwnedWindowsProcess", start)

    def assert_stopped(self):
        self.assertEqual(self.kernel.WaitForSingleObject(self.monitor, 0), 0)
        self.assertIsNone(self.process.process)
        self.assertIsNone(self.process.job)
        self.assertEqual(list((self.root / "runtime").iterdir()), [])

    def test_real_frame_from_exclusive_file_restores_source_position_and_cleans_runtime(
        self,
    ):
        with OwnedWindowsFile.create_new(self.path).stream as source:
            source.write(self.fixture.read_bytes())
            source.flush()
            source.seek(7)
            beats = []
            timer = QTimer()
            timer.setInterval(20)
            timer.timeout.connect(lambda: beats.append(1))
            timer.start()
            try:
                body, image = decode_isolated(
                    self.path, self.root, mode="video", start_ms=600, source=source
                )
            finally:
                timer.stop()
            self.assertGreaterEqual(body["frame_time_ms"], 600)
            self.assertGreater(
                image.pixelColor(0, 0).blue(), image.pixelColor(0, 0).red()
            )
            self.assertGreater(len(beats), 2)
            self.assertEqual(source.tell(), 7)
            self.assertFalse(source.closed)
            with self.assertRaises(OSError):
                self.path.open("rb")
        self.assertEqual(list((self.root / "runtime").iterdir()), [])
        self.assertFalse((self.root / "creator_loop.sqlite3").exists())
        logs = list((self.root / "logs").iterdir())
        self.assertEqual(len(logs), 1)
        self.assertEqual([item.name for item in logs[0].iterdir()], ["ownership.json"])
        record = json.loads((logs[0] / "ownership.json").read_text())
        self.assertTrue(record["component_version"].startswith("qt-decoder/"))
        self.assertEqual(record["parent_pid"], os.getpid())

    def test_process_deadline_includes_cleanup_after_a_result_exists(self):
        self.path.write_bytes(self.fixture.read_bytes())
        script = """import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]).parent
(p/'response.json').write_text(json.dumps({'format':1,'duration_ms':1000,'error':'fixture result before native hang'}))
time.sleep(60)
"""
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            started = time.monotonic()
            with (
                self.fake_worker(script),
                self.assertRaisesRegex(TimeoutError, "process deadline"),
            ):
                decode_isolated(
                    self.path, self.root, mode="video", start_ms=0, timeout_seconds=1
                )
            self.assertLess(time.monotonic() - started, 8)
            self.assert_stopped()
            self.assertIsNone(sentinel.poll())
        finally:
            if sentinel.poll() is None:
                sentinel.terminate()
            sentinel.wait(timeout=5)

    def test_shutdown_signal_cancels_only_owned_decoder_and_releases_input(self):
        self.path.write_bytes(self.fixture.read_bytes())
        with self.fake_worker("import time; time.sleep(60)"):
            QTimer.singleShot(100, self.app.aboutToQuit.emit)
            with self.assertRaisesRegex(InterruptedError, "shutdown"):
                decode_isolated(self.path, self.root, mode="video", start_ms=0)
        self.assert_stopped()
        with self.path.open("r+b") as source:
            self.assertEqual(source.read(), self.fixture.read_bytes())

    def test_invalid_child_result_is_refused_after_terminal_exit(self):
        self.path.write_bytes(self.fixture.read_bytes())
        script = """import json,sys
from pathlib import Path
(Path(sys.argv[1]).parent/'response.json').write_text(json.dumps({'format':1,'duration_ms':True}))
"""
        with self.fake_worker(script), self.assertRaisesRegex(ValueError, "duration"):
            decode_isolated(self.path, self.root, mode="audio", start_ms=0, end_ms=100)
        self.assert_stopped()

    def test_invalid_deadline_does_not_start_a_worker(self):
        for value in (0, -1, float("nan"), float("inf"), 121):
            with self.assertRaises(ValueError):
                decode_isolated(
                    self.path,
                    self.root,
                    mode="video",
                    start_ms=0,
                    timeout_seconds=value,
                )
        self.assertFalse((self.root / "runtime").exists())

    def test_owner_crash_kills_decoder_and_releases_inherited_original_handle(self):
        self.path.write_bytes(self.fixture.read_bytes())
        ready = self.root / "owned-ready.json"
        release = self.root / "crash-owner"
        script = """import json,os,sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QTimer
from creator_loop.isolated_decode import decode_isolated
from creator_loop.owned_process import OwnedWindowsProcess
root=Path(sys.argv[1]); app=QCoreApplication([])
def start(executable,arguments,logs,**kwargs):
    code="import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    p=OwnedWindowsProcess(executable,['-c',code,str(root/'actual-worker.pid')],logs,**kwargs)
    (root/'owned-ready.json').write_text(json.dumps({'fixture_owner_pid':os.getpid(),**p.record}))
    return p
timer=QTimer(); timer.setInterval(20)
def crash():
    if (root/'crash-owner').exists(): os._exit(17)
timer.timeout.connect(crash); timer.start()
with patch('creator_loop.isolated_decode.OwnedWindowsProcess',start):
    decode_isolated(root/'locked.mp4',root,mode='video',start_ms=0,timeout_seconds=30)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        owner = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.root)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 8
            record = None
            while time.monotonic() < deadline and owner.poll() is None:
                if ready.exists():
                    try:
                        record = json.loads(ready.read_text())
                        break
                    except json.JSONDecodeError:
                        pass
                time.sleep(0.02)
            self.assertIsNotNone(record, "Decoder owner did not become ready")
            # Windows venv python.exe can be a redirector. The actual fixture
            # owner records its identity before the test allows it to crash.
            self.assertEqual(record["parent_pid"], record["fixture_owner_pid"])
            self.assertEqual(Path(record["executable"]), Path(sys.executable).resolve())
            handle = self.kernel.OpenProcess(0x00101000, False, record["pid"])
            self.assertTrue(handle)
            self.addCleanup(self.kernel.CloseHandle, handle)
            times = [wintypes.FILETIME() for _ in range(4)]
            self.assertTrue(
                self.kernel.GetProcessTimes(
                    handle, *(ctypes.byref(value) for value in times)
                )
            )
            self.assertEqual(
                str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
                record["creation_identity"],
            )
            worker_pid_file = self.root / "actual-worker.pid"
            deadline = time.monotonic() + 8
            while not worker_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            worker_handle = self.kernel.OpenProcess(
                0x00101000, False, int(worker_pid_file.read_text())
            )
            self.assertTrue(worker_handle)
            self.addCleanup(self.kernel.CloseHandle, worker_handle)
            with self.assertRaises(OSError):
                self.path.open("r+b")
            release.write_text("crash this fixture owner")
            self.assertEqual(owner.wait(timeout=5), 17)
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)
            self.assertEqual(self.kernel.WaitForSingleObject(worker_handle, 5000), 0)
            with self.path.open("r+b") as source:
                self.assertEqual(source.read(), self.fixture.read_bytes())
            # Startup uses dead parent/child identities, never a bare PID.
            self.assertTrue(list((self.root / "runtime").glob("decode-*/request.json")))
            from creator_loop.app_lock import AppDataLock
            from creator_loop.runtime_recovery import recover_runtime_startup

            with AppDataLock(self.root) as lock:
                recovered = recover_runtime_startup(self.root, lock)
            self.assertEqual(len(recovered.cleaned), 1)
            self.assertEqual(recovered.preserved, ())
            self.assertEqual(list((self.root / "runtime").glob("decode-*")), [])
        finally:
            if owner.poll() is None:
                release.write_text("crash this fixture owner")
                try:
                    owner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    owner.terminate()
            owner.wait(timeout=5)
