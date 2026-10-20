"""Access denial stays closed unless a complete fresh native snapshot proves exit."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from creator_loop.backend.runtime import runtime_recovery as recovery


class NativeSnapshot:
    def __init__(self, ids, *, written=None, success=True):
        self.ids, self.written, self.success = ids, written, success

    def __call__(self, target, capacity, output):
        for index, pid in enumerate(self.ids):
            target[index] = pid
        output._obj.value = len(self.ids) * 4 if self.written is None else self.written
        return self.success


class ProcessSnapshotTests(unittest.TestCase):
    def snapshot(self, function):
        return patch.object(
            recovery.ctypes,
            "WinDLL",
            return_value=SimpleNamespace(EnumProcesses=function),
            create=True,
        )

    def test_absence_requires_complete_successful_snapshot_with_current_pid(self):
        current = os.getpid()
        with self.snapshot(NativeSnapshot([current, 123])):
            self.assertTrue(recovery._pid_absent_from_snapshot(456))
            self.assertFalse(recovery._pid_absent_from_snapshot(123))
        for function in (
            NativeSnapshot([current], success=False),
            NativeSnapshot([current], written=32768 * 4),
            NativeSnapshot([current], written=0),
            NativeSnapshot([current], written=5),
            NativeSnapshot([123 if current != 123 else 456]),
        ):
            with (
                self.subTest(function=function),
                self.snapshot(function),
                self.assertRaises(OSError),
            ):
                recovery._pid_absent_from_snapshot(999)

    def test_access_denied_still_refuses_live_unknown_or_incomplete_owner(self):
        identity = {
            "pid": 123,
            "creation_identity_format": "WIN32_FILETIME",
            "creation_identity": "123456",
            "executable": str(Path(sys.executable).resolve()),
        }
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with (
            patch.object(recovery.sys, "platform", "win32"),
            patch.object(recovery, "_api", return_value=kernel),
            patch.object(
                recovery.ctypes, "get_last_error", return_value=5, create=True
            ),
            patch.object(
                recovery.ctypes,
                "WinError",
                side_effect=lambda code: PermissionError(code, "Access denied"),
                create=True,
            ),
        ):
            with patch.object(
                recovery, "_pid_absent_from_snapshot", return_value=True
            ) as check:
                self.assertTrue(recovery._identity_dead(identity))
                check.assert_called_once_with(123)
            for value in (False, OSError("incomplete"), AttributeError("missing API")):
                with (
                    self.subTest(value=value),
                    patch.object(
                        recovery,
                        "_pid_absent_from_snapshot",
                        side_effect=value if isinstance(value, Exception) else None,
                        return_value=value,
                    ),
                ):
                    with self.assertRaises(PermissionError) as caught:
                        recovery._identity_dead(identity)
                    self.assertEqual(caught.exception.errno, 5)
            with (
                patch.object(recovery, "_pid_absent_from_snapshot") as check,
                self.assertRaises(ValueError),
            ):
                recovery._identity_dead({**identity, "creation_identity": "invalid"})
            check.assert_not_called()

    def test_non_access_error_never_uses_snapshot(self):
        identity = {
            "pid": 123,
            "creation_identity_format": "WIN32_FILETIME",
            "creation_identity": "123456",
            "executable": str(Path(sys.executable).resolve()),
        }
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with (
            patch.object(recovery.sys, "platform", "win32"),
            patch.object(recovery, "_api", return_value=kernel),
            patch.object(
                recovery.ctypes, "get_last_error", return_value=6, create=True
            ),
            patch.object(
                recovery.ctypes,
                "WinError",
                side_effect=lambda code: OSError(code, "native error"),
                create=True,
            ),
            patch.object(recovery, "_pid_absent_from_snapshot") as check,
        ):
            with self.assertRaises(OSError):
                recovery._identity_dead(identity)
            check.assert_not_called()
