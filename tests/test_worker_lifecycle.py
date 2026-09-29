import unittest
from unittest.mock import Mock

from creator_loop.worker_process import ProcessIdentity, WorkerOwnership


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.record = WorkerOwnership(
            run_id="run-1",
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )
        self.matching_identity = ProcessIdentity(
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T13:00:00Z",
        )

    def test_owned_worker_may_be_stopped(self):
        from creator_loop.worker_lifecycle import stop_owned_worker

        probe = Mock(return_value=self.matching_identity)
        terminate = Mock(return_value=True)

        self.assertTrue(stop_owned_worker(self.record, probe, terminate))
        terminate.assert_called_once_with(1234)

    def test_pid_reuse_is_never_stopped(self):
        from creator_loop.worker_lifecycle import stop_owned_worker

        reused = ProcessIdentity(
            pid=1234,
            executable=r"C:\Creator Loop\fake-worker.exe",
            creation_identity="2026-09-29T14:00:00Z",
        )
        terminate = Mock()

        self.assertFalse(
            stop_owned_worker(self.record, Mock(return_value=reused), terminate)
        )
        terminate.assert_not_called()

    def test_probe_failure_fails_closed(self):
        from creator_loop.worker_lifecycle import stop_owned_worker

        terminate = Mock()

        self.assertFalse(
            stop_owned_worker(self.record, Mock(return_value=None), terminate)
        )
        terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
