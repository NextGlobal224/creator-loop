"""Real inherited private stdout, nonblocking reads and settled cancellation."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.private_pipe import PrivatePipe


@unittest.skipUnless(sys.platform == "win32", "native overlapped Windows IPC")
class PrivatePipeOwnershipTests(unittest.TestCase):
    def settle(self, pipe):
        deadline = time.perf_counter() + 2
        while not pipe.try_close() and time.perf_counter() < deadline:
            time.sleep(0.001)
        self.assertTrue(pipe.reader.closed)
        self.assertIsNone(pipe.event)
        self.assertFalse(pipe.read_pending)

    def test_idle_read_and_cancellation_are_nonblocking_and_native_handles_settle(self):
        pipe = PrivatePipe(eager_reads=True)
        try:
            self.assertIsNotNone(pipe.writer)
            self.assertFalse(os.get_inheritable(pipe.writer.fileno()))
            for _ in range(20):
                started = time.perf_counter()
                self.assertEqual(pipe.read_available(), b"")
                self.assertLess(time.perf_counter() - started, 0.1)
            self.assertTrue(pipe.read_pending)
        finally:
            self.settle(pipe)

    def test_exact_large_private_content_goes_through_owned_child_without_log_copy(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder) / "owned logs"
            pipe = PrivatePipe(eager_reads=True)
            expected = ("PRIVATE_MACHINE_TEXT_á中🙂\n" * 12000).encode("utf-8")
            try:
                script = "import sys;sys.stderr.write('PRIVATE_STDERR_MUST_NOT_BE_LOGGED');sys.stdout.buffer.write(('PRIVATE_MACHINE_TEXT_á中🙂\\n'*12000).encode('utf-8'));sys.stdout.buffer.flush()"
                with OwnedWindowsProcess(
                    Path(sys.executable),
                    ["-c", script],
                    logs,
                    component_version="test/private-content",
                    capture_output=False,
                    stdout_sink=pipe.writer,
                    memory_limit_bytes=128 * 1024**2,
                ) as child:
                    child.streams[1].close()  # child owns its inherited writer now
                    pipe.writer.close()
                    received = bytearray()
                    deadline = time.perf_counter() + 10
                    while (
                        not (pipe.eof and child.tree_finished())
                        and time.perf_counter() < deadline
                    ):
                        started = time.perf_counter()
                        received.extend(pipe.read_available())
                        self.assertLess(time.perf_counter() - started, 0.1)
                        time.sleep(0.001)
                    self.assertTrue(pipe.eof)
                    self.assertTrue(child.tree_finished())
                    self.assertEqual(child.poll().exit_code, 0)
                    self.assertEqual(received, expected)
                self.assertEqual({p.name for p in logs.iterdir()}, {"ownership.json"})
                self.assertNotIn(b"PRIVATE_", (logs / "ownership.json").read_bytes())
            finally:
                self.settle(pipe)

    def test_pending_read_on_owned_child_termination_settles_without_unrelated_pid_kill(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pipe = PrivatePipe(eager_reads=True)
            try:
                with OwnedWindowsProcess(
                    Path(sys.executable),
                    ["-c", "import time;time.sleep(30)"],
                    root / "sentinel",
                    component_version="test/unrelated-sentinel",
                ) as sentinel:
                    with OwnedWindowsProcess(
                        Path(sys.executable),
                        ["-c", "import time;time.sleep(30)"],
                        root / "owned",
                        component_version="test/private-hang",
                        capture_output=False,
                        stdout_sink=pipe.writer,
                    ) as child:
                        child.streams[1].close()
                        pipe.writer.close()
                        self.assertEqual(pipe.read_available(), b"")
                        self.assertTrue(pipe.read_pending)
                        child.request_stop(124)
                        deadline = time.perf_counter() + 5
                        while (
                            not child.tree_finished() and time.perf_counter() < deadline
                        ):
                            time.sleep(0.005)
                        self.assertTrue(child.tree_finished())
                        self.settle(pipe)
                        self.assertEqual(
                            sentinel.kernel.WaitForSingleObject(sentinel.process, 0),
                            258,
                        )
            finally:
                self.settle(pipe)


if __name__ == "__main__":
    unittest.main()
