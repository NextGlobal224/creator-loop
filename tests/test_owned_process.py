"""Native owned tree: quoting, failure-before-execution, timeout and owner crash."""

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

from creator_loop.owned_process import OwnedWindowsProcess, _api


@unittest.skipUnless(sys.platform == "win32", "requires native Windows Job Objects")
class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "Owned cây Huế"
        self.base.mkdir()
        self.exe = Path(sys.executable)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        self.env["PYTHONIOENCODING"] = "utf-8"

    def _wait_file(self, path):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if path.is_file():
                try:
                    return json.loads(path.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        self.fail("Owned fixture did not become ready")

    def _hold_child(self, pid):
        kernel = _api()
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x00101000, False, pid)
        self.assertTrue(handle)
        self.addCleanup(kernel.CloseHandle, handle)
        return kernel, handle

    def _worker_script(self):
        return """import json,os,subprocess,sys,time
from pathlib import Path
grand=Path(sys.argv[1]+'.grand')
code="import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
child=subprocess.Popen([sys.executable,'-c',code,str(grand)])
deadline=time.monotonic()+5
while not grand.exists() and time.monotonic()<deadline: time.sleep(.01)
Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid,'grand':int(grand.read_text())}))
print('owned stdout',flush=True)
print('owned stderr',file=sys.stderr,flush=True)
time.sleep(60)
"""

    def _start_tree(self, ready, log_name):
        return OwnedWindowsProcess(
            self.exe,
            ["-u", "-c", self._worker_script(), str(ready)],
            self.base / log_name,
            component_version="fake-1",
            environment=self.env,
        )

    def test_normal_exit_captures_unicode_arguments_and_both_streams(self):
        arguments = ["Huế có dấu", 'quote" trailing\\', ""]
        script = "import json,sys; print(json.dumps(sys.argv[1:],ensure_ascii=False)); print('stderr',file=sys.stderr); sys.exit(7)"
        with OwnedWindowsProcess(
            self.exe,
            ["-u", "-c", script, *arguments],
            self.base / "normal",
            component_version="fake-1",
            environment=self.env,
        ) as process:
            outcome = process.wait(10)
            record = process.record.copy()
        self.assertEqual(outcome.exit_code, 7)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(
            json.loads(process.stdout_path.read_text(encoding="utf-8")), arguments
        )
        self.assertIn("stderr", process.stderr_path.read_text())
        self.assertEqual(record["component_version"], "fake-1")
        self.assertEqual(record["parent_pid"], os.getpid())
        self.assertEqual(record["creation_identity_format"], "WIN32_FILETIME")
        self.assertGreater(int(record["creation_identity"]), 0)
        self.assertEqual(
            json.loads((self.base / "normal/ownership.json").read_text()), record
        )

    def test_timeout_stops_its_tree_and_keeps_unrelated_sentinel(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], env=self.env
        )
        try:
            ready = self.base / "timeout-ready.json"
            with self._start_tree(ready, "timeout") as process:
                ids = self._wait_file(ready)
                kernel, child = self._hold_child(ids["child"])
                _, grand = self._hold_child(ids["grand"])
                outcome = process.wait(0.1)
                self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0)
                self.assertEqual(kernel.WaitForSingleObject(grand, 5000), 0)
            self.assertTrue(outcome.timed_out)
            self.assertEqual(outcome.exit_code, 124)
            self.assertIsNone(sentinel.poll())
            self.assertIn("owned stdout", process.stdout_path.read_text())
            self.assertIn("owned stderr", process.stderr_path.read_text())
        finally:
            if sentinel.poll() is None:
                sentinel.terminate()
            sentinel.wait(timeout=5)

    def test_cancel_and_context_exception_close_only_its_job(self):
        ready = self.base / "cancel-ready.json"
        with self.assertRaisesRegex(RuntimeError, "fixture exception"):
            with self._start_tree(ready, "cancel") as process:
                ids = self._wait_file(ready)
                kernel, child = self._hold_child(ids["child"])
                _, grand = self._hold_child(ids["grand"])
                raise RuntimeError("fixture exception")
        self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0)
        self.assertEqual(kernel.WaitForSingleObject(grand, 5000), 0)
        process.close()
        with self.assertRaises(RuntimeError):
            process.wait(1)

    def test_owner_crash_closes_noninherited_job_and_kills_tree(self):
        ready = self.base / "crash-ready.json"
        script = """import json,os,sys
from pathlib import Path
from creator_loop.owned_process import OwnedWindowsProcess
p=OwnedWindowsProcess(Path(sys.argv[1]),['-u','-c',sys.argv[2],sys.argv[3]],Path(sys.argv[4]),component_version='fake-1')
print(json.dumps(p.record),flush=True)
sys.stdin.readline()
os._exit(17)
"""
        owner = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                script,
                str(self.exe),
                self._worker_script(),
                str(ready),
                str(self.base / "crash"),
            ],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            live_record = json.loads(owner.stdout.readline())
            ids = self._wait_file(ready)
            kernel, launcher = self._hold_child(live_record["pid"])
            kernel, worker = self._hold_child(ids["parent"])
            _, child = self._hold_child(ids["child"])
            _, grand = self._hold_child(ids["grand"])
            _, errors = owner.communicate("crash\n", timeout=5)
            self.assertEqual(owner.returncode, 17, errors)
            persisted = json.loads((self.base / "crash/ownership.json").read_text())
            self.assertEqual(persisted, live_record)
            # A venv redirector may launch the actual interpreter as a child.
            self.assertEqual(kernel.WaitForSingleObject(launcher, 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(worker, 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(grand, 5000), 0)
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.communicate(timeout=5)
            for stream in (owner.stdin, owner.stdout, owner.stderr):
                if stream is not None:
                    stream.close()

    def test_assignment_failure_keeps_child_suspended_and_never_runs_code(self):
        marker = self.base / "must-not-run.txt"
        script = (
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')"
        )
        kernel = _api()
        with (
            patch("creator_loop.owned_process._api", return_value=kernel),
            patch.object(kernel, "AssignProcessToJobObject", return_value=False),
        ):
            with self.assertRaises(OSError):
                OwnedWindowsProcess(
                    self.exe,
                    ["-c", script, str(marker)],
                    self.base / "failed-assignment",
                    component_version="fake-1",
                    environment=self.env,
                )
        self.assertFalse(marker.exists())

    def test_invalid_deadline_leaves_cleanup_to_owned_context(self):
        with OwnedWindowsProcess(
            self.exe,
            ["-c", "import time; time.sleep(60)"],
            self.base / "deadline",
            component_version="fake-1",
            environment=self.env,
        ) as process:
            for value in (0, -1, float("nan"), float("inf"), 86401):
                with self.assertRaises(ValueError):
                    process.wait(value)
