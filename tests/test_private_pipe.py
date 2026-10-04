"""Binary stream stays out of logs; borrowed handles and empty reads are safe."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from creator_loop.owned_process import OwnedWindowsProcess

if sys.platform == "win32":
    import msvcrt

    from creator_loop.private_pipe import PrivatePipe


@unittest.skipUnless(sys.platform == "win32", "native private pipe/output handles")
class PrivatePipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.executable = Path(sys.executable)

    def test_binary_owned_child_output_is_bounded_and_never_written_to_logs(self):
        payload = bytes(range(256)) * 4096
        logs = self.root / "private-log"
        with PrivatePipe() as pipe:
            started = time.monotonic()
            self.assertEqual(pipe.read_available(), b"")
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(os.get_handle_inheritable(pipe.read_handle))
            output_handle = msvcrt.get_osfhandle(pipe.writer.fileno())
            self.assertFalse(os.get_handle_inheritable(output_handle))
            script = "import os,msvcrt; msvcrt.setmode(1,os.O_BINARY); data=bytes(range(256))*4096; view=memoryview(data);\nwhile view:\n n=os.write(1,view); view=view[n:]"
            received = bytearray()
            with OwnedWindowsProcess(
                self.executable,
                ["-u", "-c", script],
                logs,
                component_version="private-stream-fixture",
                capture_output=False,
                stdout_sink=pipe.writer,
            ) as child:
                self.assertFalse(os.get_handle_inheritable(output_handle))
                pipe.writer.close()
                deadline = time.monotonic() + 10
                while len(received) < len(payload) and time.monotonic() < deadline:
                    chunk = pipe.read_available(65536)
                    self.assertLessEqual(len(chunk), 65536)
                    received.extend(chunk)
                    time.sleep(0.001)
                self.assertEqual(received, payload)
                outcome = child.wait(5)
                self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(pipe.read_available(), b"")
            self.assertTrue(pipe.eof)
        self.assertEqual([file.name for file in logs.iterdir()], ["ownership.json"])

    def test_borrowed_output_is_not_closed_or_made_inheritable(self):
        path = self.root / "caller-output"
        with path.open("x+b", buffering=0) as target:
            handle = msvcrt.get_osfhandle(target.fileno())
            original = os.get_handle_inheritable(handle)
            with OwnedWindowsProcess(
                self.executable,
                ["-c", "import os; os.write(1,b'caller bytes')"],
                self.root / "borrowed-log",
                component_version="borrowed-fixture",
                capture_output=False,
                stdout_sink=target,
            ) as child:
                self.assertEqual(child.wait(5).exit_code, 0)
            self.assertFalse(target.closed)
            self.assertEqual(os.get_handle_inheritable(handle), original)
            target.seek(0)
            self.assertEqual(target.read(), b"caller bytes")

    def test_invalid_budget_or_readonly_output_refuses_without_child(self):
        with PrivatePipe() as pipe:
            for value in (0, -1, True, 4 * 1024 * 1024 + 1):
                with self.assertRaises(ValueError):
                    pipe.read_available(value)
        source = self.root / "readonly"
        source.write_bytes(b"preserve")
        with source.open("rb") as stream, self.assertRaises(ValueError):
            OwnedWindowsProcess(
                self.executable,
                ["-c", "raise AssertionError('must not run')"],
                self.root / "refused-log",
                component_version="refused-fixture",
                stdout_sink=stream,
            )
        self.assertEqual(source.read_bytes(), b"preserve")
