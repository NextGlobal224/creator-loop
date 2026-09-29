import json
import subprocess
import unittest
from unittest.mock import patch

from creator_loop.windows_process import read_process_identity


class WindowsProcessTests(unittest.TestCase):
    def test_valid_probe_returns_identity(self):
        payload = {
            "pid": 1234,
            "executable": r"C:\Creator Loop\fake-worker.exe",
            "creation_identity": "2026-09-29T13:00:00.0000000Z",
            "parent_pid": 4321,
        }

        completed = subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        with patch(
            "creator_loop.windows_process.subprocess.run", return_value=completed
        ):
            actual = read_process_identity(1234)

        self.assertIsNotNone(actual)
        self.assertEqual(actual.pid, 1234)
        self.assertEqual(actual.executable, r"C:\Creator Loop\fake-worker.exe")
        self.assertEqual(
            actual.creation_identity,
            "2026-09-29T13:00:00.0000000Z",
        )

    def test_powershell_failure_fails_closed(self):
        with patch(
            "creator_loop.windows_process.subprocess.run",
            side_effect=subprocess.SubprocessError("probe failed"),
        ):
            self.assertIsNone(read_process_identity(1234))

    def test_invalid_json_fails_closed(self):
        completed = subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout="{not-json",
            stderr="",
        )

        with patch(
            "creator_loop.windows_process.subprocess.run", return_value=completed
        ):
            self.assertIsNone(read_process_identity(1234))

    def test_wrong_pid_fails_closed(self):
        payload = {
            "pid": 9999,
            "executable": r"C:\Creator Loop\fake-worker.exe",
            "creation_identity": "2026-09-29T13:00:00.0000000Z",
            "parent_pid": 4321,
        }

        completed = subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        with patch(
            "creator_loop.windows_process.subprocess.run", return_value=completed
        ):
            self.assertIsNone(read_process_identity(1234))

    def test_missing_identity_fields_fail_closed(self):
        payload = {
            "pid": 1234,
            "executable": "",
            "creation_identity": "",
            "parent_pid": 4321,
        }

        completed = subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        with patch(
            "creator_loop.windows_process.subprocess.run", return_value=completed
        ):
            self.assertIsNone(read_process_identity(1234))


if __name__ == "__main__":
    unittest.main()
