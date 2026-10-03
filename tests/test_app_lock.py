"""Real process contention, launcher refusal and OS crash release on Unicode roots."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.paths import ensure_data_root


class AppDataLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Data root Huế"
        ensure_data_root(self.root)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        self.env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)

    def _smoke(self):
        return subprocess.run(
            [sys.executable, "-m", "creator_loop", "--smoke"],
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    def test_same_root_blocks_second_holder_and_lock_file_survives_release(self):
        lock = AppDataLock(self.root)
        with lock:
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    self.fail("Second holder entered")
            with self.assertRaisesRegex(RuntimeError, "already held"):
                lock.__enter__()
            self.assertTrue(lock.path.exists())
        lock.close()
        with AppDataLock(self.root):
            self.assertTrue(lock.path.exists())

    def test_launcher_refuses_before_initializing_db_then_succeeds_after_release(self):
        with AppDataLock(self.root):
            result = self._smoke()
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("in use", result.stderr)
            self.assertFalse((self.root / "creator_loop.sqlite3").exists())
        result = self._smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema OK", result.stdout)
        with AppDataLock(self.root):
            pass

    def test_other_root_is_independent_and_exception_releases_handle(self):
        other = self.root.parent / "Other root"
        ensure_data_root(other)
        with self.assertRaisesRegex(RuntimeError, "owned fixture"):
            with AppDataLock(self.root), AppDataLock(other):
                raise RuntimeError("owned fixture")
        with AppDataLock(self.root), AppDataLock(other):
            pass

    def test_crashed_process_releases_lock_without_pid_probe_or_file_delete(self):
        script = """import os,sys
from pathlib import Path
from creator_loop.app_lock import AppDataLock
with AppDataLock(Path(sys.argv[1])):
    print('READY',flush=True)
    sys.stdin.readline()
    os._exit(13)
"""
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.root)],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            # Parent communicates only with the process it created. Outer test
            # runner provides the hard deadline if the readiness read hangs.
            self.assertEqual(child.stdout.readline().strip(), "READY")
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    self.fail("Crash fixture lock not held")
            _, errors = child.communicate("exit\n", timeout=5)
            self.assertEqual(child.returncode, 13, errors)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        path = self.root / "runtime" / "app-data.lock"
        self.assertTrue(path.exists())
        with AppDataLock(self.root):
            pass
        self.assertTrue(path.exists())

    def test_missing_runtime_or_linked_lock_fails_without_creating_fallback(self):
        missing = self.root.parent / "Missing root"
        with self.assertRaises(FileNotFoundError):
            AppDataLock(missing)
        self.assertFalse(missing.exists())
        target = self.root / "user-file.txt"
        target.write_text("Keep me", encoding="utf-8")
        path = self.root / "runtime" / "app-data.lock"
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Symlink privilege unavailable")
        with self.assertRaisesRegex(OSError, "linked"):
            with AppDataLock(self.root):
                self.fail("Linked lock entered")
        self.assertEqual(target.read_text(encoding="utf-8"), "Keep me")
