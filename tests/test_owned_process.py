"""Native owned tree: quoting, failure-before-execution, timeout and owner crash."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.owned_process import OwnedWindowsProcess, _api
from creator_loop.windows_owned_file import OwnedWindowsFile


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
        diagnostics = []
        diagnostic_root = (
            Path(__file__).resolve().parents[1]
            / ".local-test-logs"
            / f"owned-fixture-failure-{uuid4().hex}"
        )
        diagnostic_root.mkdir(parents=True)
        for log in self.base.rglob("*.log"):
            destination = diagnostic_root / log.relative_to(self.base)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(log, destination)
            diagnostics.append(
                f"{log.name}: {log.read_text(encoding='utf-8', errors='replace')[-4000:]}"
            )
        self.fail(
            f"Owned fixture did not become ready; logs={diagnostic_root}\n"
            + "\n".join(diagnostics)
        )

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

    def test_exclusive_input_is_readable_but_native_write_is_denied(self):
        path = self.base / "exclusive original.mp4"
        with OwnedWindowsFile.create_new(path).stream as source:
            source.write(b"verified input bytes")
            source.flush()
            source.seek(0)
            self.assertFalse(os.get_inheritable(source.fileno()))
            # A path reopen is forbidden; the child must use only the duplicate.
            with self.assertRaises(OSError):
                path.open("rb")
            script = """import ctypes,json,sys
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.GetStdHandle.argtypes=(w.DWORD,); k.GetStdHandle.restype=w.HANDLE
k.WriteFile.argtypes=(w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p)
k.WriteFile.restype=w.BOOL
h=k.GetStdHandle(0xfffffff6); count=w.DWORD()
ok=k.WriteFile(h,b'X',1,ctypes.byref(count),None)
error=ctypes.get_last_error()
print(json.dumps({'bytes':sys.stdin.buffer.read().decode(),'write':bool(ok),'error':error}))
"""
            with OwnedWindowsProcess(
                self.exe,
                ["-u", "-c", script],
                self.base / "input",
                component_version="fake-1",
                environment=self.env,
                stdin_source=source,
            ) as process:
                outcome = process.wait(10)
            self.assertEqual(outcome.exit_code, 0)
            result = json.loads(process.stdout_path.read_text())
            self.assertEqual(result["bytes"], "verified input bytes")
            self.assertFalse(result["write"])
            self.assertEqual(result["error"], 5)  # ERROR_ACCESS_DENIED
            self.assertFalse(source.closed)
            self.assertFalse(os.get_inheritable(source.fileno()))
            source.seek(0)
            self.assertEqual(source.read(), b"verified input bytes")

    def test_input_launch_failure_keeps_borrowed_source_open(self):
        path = self.base / "failure input"
        with OwnedWindowsFile.create_new(path).stream as source:
            source.write(b"preserved")
            source.flush()
            source.seek(0)
            with self.assertRaises(FileNotFoundError):
                OwnedWindowsProcess(
                    self.exe,
                    ["-c", "raise AssertionError('must not run')"],
                    self.base / "bad-input-launch",
                    component_version="fake-1",
                    cwd=self.base / "missing",
                    stdin_source=source,
                )
            self.assertFalse(source.closed)
            self.assertFalse(os.get_inheritable(source.fileno()))
            self.assertEqual(source.read(), b"preserved")

    def test_native_job_memory_limit_refuses_oversized_allocation(self):
        script = """import sys
try:
    value=bytearray(128*1024*1024)
except MemoryError:
    sys.exit(13)
sys.exit(99)
"""
        with OwnedWindowsProcess(
            self.exe,
            ["-c", script],
            self.base / "memory-budget",
            component_version="fake-1",
            memory_limit_bytes=64 * 1024 * 1024,
        ) as process:
            outcome = process.wait(10)
        self.assertEqual(outcome.exit_code, 13)
        self.assertFalse(outcome.timed_out)

    def test_before_resume_can_persist_binding_before_child_code_runs(self):
        binding = self.base / "binding.json"

        def bind(record):
            self.assertFalse((self.base / "child-ran").exists())
            binding.write_text(json.dumps(record), encoding="utf-8")

        code = "import json,sys; from pathlib import Path; p=Path(sys.argv[1]); assert json.loads((p/'binding.json').read_text())['pid']; (p/'child-ran').write_text('ran')"
        with OwnedWindowsProcess(
            self.exe,
            ["-c", code, str(self.base)],
            self.base / "binding-log",
            component_version="fake-1",
            before_resume=bind,
        ) as process:
            outcome = process.wait(10)
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue((self.base / "child-ran").exists())

    def test_before_resume_failure_never_executes_owned_child(self):
        target = self.base / "must-not-run"

        def refuse(_record):
            raise ValueError("binding failed")

        with self.assertRaisesRegex(ValueError, "binding failed"):
            OwnedWindowsProcess(
                self.exe,
                [
                    "-c",
                    "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('bad')",
                    str(target),
                ],
                self.base / "failed-binding",
                component_version="fake-1",
                before_resume=refuse,
            )
        self.assertFalse(target.exists())

    def test_input_timeout_closes_duplicate_keeps_source_and_exclusive_lock(self):
        path = self.base / "timeout input"
        with OwnedWindowsFile.create_new(path).stream as source:
            source.write(b"preserved")
            source.flush()
            source.seek(0)
            with OwnedWindowsProcess(
                self.exe,
                ["-c", "import time; time.sleep(60)"],
                self.base / "input-timeout",
                component_version="fake-1",
                stdin_source=source,
            ) as process:
                outcome = process.wait(0.2)
            self.assertTrue(outcome.timed_out)
            self.assertFalse(source.closed)
            with self.assertRaises(OSError):
                path.open("rb")
            source.seek(0)
            self.assertEqual(source.read(), b"preserved")

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

    def test_poll_does_not_stop_live_session_and_normal_exit_cleans_descendants(self):
        ready = self.base / "poll-ready.json"
        finish = self.base / "finish.txt"
        script = self._worker_script().replace(
            "time.sleep(60)\n",
            "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n",
        )
        with OwnedWindowsProcess(
            self.exe,
            ["-u", "-c", script, str(ready), str(finish)],
            self.base / "poll",
            component_version="fake-1",
            environment=self.env,
        ) as process:
            ids = self._wait_file(ready)
            kernel, child = self._hold_child(ids["child"])
            _, grand = self._hold_child(ids["grand"])
            self.assertIsNone(process.poll())
            self.assertEqual(kernel.WaitForSingleObject(child, 0), 0x102)
            finish.write_text("exit")
            deadline = time.monotonic() + 5
            outcome = process.poll()
            while outcome is None and time.monotonic() < deadline:
                time.sleep(0.01)
                outcome = process.poll()
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(grand, 5000), 0)

    def test_private_ui_output_is_discarded_while_ownership_is_persisted(self):
        with OwnedWindowsProcess(
            self.exe,
            ["-c", "import sys; print('private'); print('private',file=sys.stderr)"],
            self.base / "private",
            component_version="fake-1",
            environment=self.env,
            capture_output=False,
        ) as process:
            self.assertEqual(process.wait(10).exit_code, 0)
        self.assertFalse(process.stdout_path.exists())
        self.assertFalse(process.stderr_path.exists())
        self.assertTrue((self.base / "private/ownership.json").exists())
