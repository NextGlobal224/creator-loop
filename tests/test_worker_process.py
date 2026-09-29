import unittest

from creator_loop.worker_process import (
    ProcessIdentity,
    WorkerOwnership,
    owns_process,
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


if __name__ == "__main__":
    unittest.main()
