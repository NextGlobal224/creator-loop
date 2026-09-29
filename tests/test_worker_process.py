import tempfile
import unittest
from pathlib import Path

from creator_loop.worker_process import (
    ProcessIdentity,
    WorkerOwnership,
    load_ownership,
    owns_process,
    save_ownership,
)


class WorkerOwnershipTests(unittest.TestCase):
    def test_owned_worker_matches_exact_identity(self):
        record = WorkerOwnership(
            run_id="run-1",
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )
        actual = ProcessIdentity(
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )

        self.assertTrue(owns_process(record, actual))

    def test_unowned_worker_is_rejected(self):
        record = WorkerOwnership(
            run_id="run-1",
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )
        actual = ProcessIdentity(
            pid=5678,
            executable=r"C:\Other\worker.exe",
            creation_identity="2026-09-29T13:05:00Z",
        )

        self.assertFalse(owns_process(record, actual))

    def test_pid_reuse_is_rejected(self):
        record = WorkerOwnership(
            run_id="run-1",
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )
        reused_pid = ProcessIdentity(
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T14:00:00Z",
        )

        self.assertFalse(owns_process(record, reused_pid))

    def test_ownership_record_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime" / "worker.json"
            record = WorkerOwnership(
                run_id="run-unicode",
                pid=4321,
                executable=r"C:\Creator Loop\công cụ\worker.exe",
                creation_identity="2026-09-29T15:00:00Z",
            )

            save_ownership(path, record)

            self.assertEqual(load_ownership(path), record)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_invalid_ownership_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "worker.json"
            path.write_text(
                '{"run_id":"run-1","pid":1234}',
                encoding="utf-8",
            )

            self.assertIsNone(load_ownership(path))

    def test_invalid_pid_type_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "worker.json"
            path.write_text(
                '{"creation_identity":"x","executable":"worker.exe",'
                '"pid":true,"run_id":"run-1"}',
                encoding="utf-8",
            )

            self.assertIsNone(load_ownership(path))


if __name__ == "__main__":
    unittest.main()
