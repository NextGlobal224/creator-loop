"""Native dead/live identity and handle-targeted orphan recovery safety."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock
from creator_loop.paths import ensure_data_root

if sys.platform == "win32":
    from creator_loop.runtime_files import RuntimeHandle
    from creator_loop.runtime_ownership import create_workspace_marker
    from creator_loop.runtime_recovery import recover_runtime_startup


@unittest.skipUnless(
    sys.platform == "win32", "requires native Win32 file/process identities"
)
class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Runtime Hue space"
        ensure_data_root(self.root)
        # Hosted Windows temp may contain an 8.3 alias (RUNNER~1). Production
        # recovery resolves its root; direct handle fixtures must do the same.
        self.root = self.root.resolve(strict=True)
        self.environment = os.environ.copy()
        self.environment["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[1] / "app"
        )

    def prepare(self, name="decode-dead0001", child=True):
        workspace = self.root / "runtime" / name
        workspace.mkdir()
        code = """import sys
from pathlib import Path
from creator_loop.runtime_ownership import create_workspace_marker,bind_workspace_child
from creator_loop.owned_process import OwnedWindowsProcess
root=Path(sys.argv[1]); work=Path(sys.argv[2]); component='qt-decoder/fixture/Qt-test'
marker=create_workspace_marker(root,work,component)
(work/'request.json').write_text('{}')
if sys.argv[3]=='child':
    with OwnedWindowsProcess(Path(sys.executable),['-c','pass'],root/'logs'/work.name,
        component_version=component,before_resume=lambda info:bind_workspace_child(work,marker,info)) as owned:
        outcome=owned.wait(10)
        assert outcome.exit_code==0
    (work/'response.json').write_text('{}')
    (work/'frame.png').write_bytes(b'owned private frame fixture')
"""
        finished = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.root),
                str(workspace),
                "child" if child else "prepared",
            ],
            env=self.environment,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return workspace

    def recover(self):
        with AppDataLock(self.root) as lock:
            return recover_runtime_startup(self.root, lock)

    def test_dead_bound_parent_and_child_cleanup_keeps_original_and_ownership_logs(
        self,
    ):
        original = self.root / "storage/originals/user-file"
        original.write_bytes(b"keep user original")
        workspace = self.prepare()
        result = self.recover()
        self.assertEqual(result.cleaned, (workspace.name,))
        self.assertEqual(result.preserved, ())
        self.assertFalse(workspace.exists())
        self.assertEqual(original.read_bytes(), b"keep user original")
        self.assertTrue(
            (self.root / "logs" / workspace.name / "ownership.json").exists()
        )
        self.assertEqual(self.recover().cleaned, ())

    def test_prepared_parent_crash_before_child_assignment_can_recover(self):
        workspace = self.prepare(child=False)
        self.assertEqual(self.recover().cleaned, (workspace.name,))
        self.assertFalse(workspace.exists())

    def test_live_parent_unknown_workspace_and_extra_foreign_file_are_preserved(self):
        live = self.root / "runtime/decode-live0001"
        live.mkdir()
        create_workspace_marker(self.root, live, "qt-decoder/fixture/Qt-test")
        unknown = self.root / "runtime/decode-unkn0001"
        unknown.mkdir()
        (unknown / "private-user-file").write_bytes(b"keep unknown")
        extra = self.prepare("decode-extr0001")
        (extra / "unowned-notes.txt").write_bytes(b"keep external")
        result = self.recover()
        self.assertEqual(result.cleaned, ())
        self.assertEqual(
            {item[0] for item in result.preserved},
            {live.name, unknown.name, extra.name},
        )
        self.assertTrue((live / "ownership.json").exists())
        self.assertEqual((unknown / "private-user-file").read_bytes(), b"keep unknown")
        self.assertEqual((extra / "unowned-notes.txt").read_bytes(), b"keep external")

    def test_foreign_root_and_malformed_child_binding_preserve_all_payload(self):
        workspace = self.prepare()
        marker_path = workspace / "ownership.json"
        marker = json.loads(marker_path.read_text())
        marker["data_root"] = str(self.root.parent / "foreign")
        marker_path.write_text(json.dumps(marker))
        self.assertEqual(self.recover().cleaned, ())
        self.assertTrue((workspace / "frame.png").exists())
        marker["data_root"] = str(self.root)
        marker_path.write_text(json.dumps(marker))
        (workspace / "child-ownership.json").write_text("{")
        self.assertEqual(self.recover().cleaned, ())
        self.assertTrue((workspace / "frame.png").exists())

    def test_hardlinked_payload_is_not_deleted(self):
        workspace = self.prepare()
        original = self.root / "storage/originals/user-link"
        original.write_bytes(b"private original")
        (workspace / "frame.png").unlink()
        os.link(original, workspace / "frame.png")
        self.assertEqual(self.recover().cleaned, ())
        self.assertEqual(original.read_bytes(), b"private original")
        self.assertEqual((workspace / "frame.png").read_bytes(), b"private original")

    def test_live_child_is_preserved_after_parent_exit_and_pid_reuse_never_kills(self):
        from creator_loop.owned_process import _api
        from creator_loop.runtime_ownership import process_identity

        workspace = self.prepare()
        path = workspace / "child-ownership.json"
        child = json.loads(path.read_text())
        kernel = _api()
        identity = process_identity(kernel, kernel.GetCurrentProcess())
        child.update(identity)
        child["pid"] = os.getpid()
        path.write_text(json.dumps(child))
        self.assertEqual(self.recover().cleaned, ())
        self.assertTrue((workspace / "frame.png").exists())
        # Same live PID, different creation FILETIME: the recorded child is gone.
        child["creation_identity"] = str(int(identity["creation_identity"]) - 1)
        path.write_text(json.dumps(child))
        self.assertEqual(self.recover().cleaned, (workspace.name,))
        self.assertEqual(
            kernel.WaitForSingleObject(kernel.GetCurrentProcess(), 0), 0x102
        )

    def test_access_denied_is_not_treated_as_a_dead_process(self):
        from unittest.mock import Mock

        workspace = self.prepare()
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        with (
            patch("creator_loop.runtime_recovery._api", return_value=kernel),
            patch(
                "creator_loop.runtime_recovery.ctypes.get_last_error", return_value=5
            ),
        ):
            result = self.recover()
        self.assertEqual(result.cleaned, ())
        self.assertTrue((workspace / "frame.png").exists())

    def test_native_directory_and_file_handles_deny_replacement_and_delete_by_handle(
        self,
    ):
        directory = self.root / "runtime/handle-test"
        directory.mkdir()
        path = directory / "owned-file"
        path.write_bytes(b"owned")
        with self.assertRaises(OSError):
            RuntimeHandle(directory / ".." / directory.name, directory=True)
        with RuntimeHandle(directory, directory=True) as folder:
            with self.assertRaises(OSError):
                directory.rename(self.root / "runtime/replaced")
            with RuntimeHandle(path) as held:
                with self.assertRaises(OSError):
                    path.rename(directory / "replacement")
                held.discard()
            self.assertFalse(path.exists())
            folder.discard()
        self.assertFalse(directory.exists())

    def test_released_or_foreign_lock_refuses_recovery(self):
        self.prepare()
        with self.assertRaises(RuntimeError):
            recover_runtime_startup(self.root, AppDataLock(self.root))
        other = self.root.parent / "other"
        ensure_data_root(other)
        with AppDataLock(other) as lock:
            with self.assertRaises(RuntimeError):
                recover_runtime_startup(self.root, lock)
