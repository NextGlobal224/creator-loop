"""Actual pending Windows overlapped I/O retains buffers through completion."""

import ctypes
import sys
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from creator_loop.private_pipe import PrivatePipe, _Overlapped


@unittest.skipUnless(sys.platform == "win32", "requires overlapped Windows named pipe")
class PrivatePipeAsyncTests(unittest.TestCase):
    def pending(self, pipe):
        buffer = ctypes.create_string_buffer(32)
        operation = _Overlapped()
        operation.event = pipe.event
        result = pipe.kernel.ReadFile(
            pipe.read_handle, buffer, 32, None, ctypes.byref(operation)
        )
        self.assertFalse(result)
        self.assertEqual(ctypes.get_last_error(), 997)
        pipe._pending = (buffer, operation)
        return buffer, operation

    def test_pending_read_returns_without_wait_and_keeps_native_memory(self):
        with PrivatePipe() as pipe:
            buffer, operation = self.pending(pipe)
            self.assertTrue(pipe.read_pending)
            started = time.monotonic()
            self.assertEqual(pipe.read_available(), b"")
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertIs(pipe._pending[0], buffer)
            self.assertIs(pipe._pending[1], operation)
            payload = b"\x00\xff\r\nactual pending read"
            pipe.writer.write(payload)
            received = bytearray()
            deadline = time.monotonic() + 5
            while len(received) < len(payload) and time.monotonic() < deadline:
                part = pipe.read_available(3)
                self.assertLessEqual(len(part), 3)
                received.extend(part)
                time.sleep(0.005)
            self.assertEqual(received, payload)
            self.assertIsNone(pipe._pending)
            self.assertFalse(pipe.read_pending)

    def test_cancel_pending_read_waits_for_native_completion_before_freeing(self):
        pipe = PrivatePipe()
        self.pending(pipe)
        started = time.monotonic()
        closed = pipe.try_close()
        self.assertLess(time.monotonic() - started, 0.2)
        if not closed:
            self.assertFalse(pipe.reader.closed)
            self.assertIsNotNone(pipe.event)
            self.assertIsNotNone(pipe._pending)
        deadline = time.monotonic() + 5
        while not closed and time.monotonic() < deadline:
            closed = pipe.try_close()
            time.sleep(0.005)
        self.assertTrue(closed)
        self.assertIsNone(pipe._pending)
        self.assertIsNone(pipe.event)
        self.assertTrue(pipe.reader.closed)
        self.assertTrue(pipe.writer.closed)

    def test_async_read_uses_only_native_completion_byte_count(self):
        payload = b"\x00\xff\r\ncompletion count only" * 3
        with PrivatePipe() as pipe:
            native_read = pipe.kernel.ReadFile
            native_completed = pipe.kernel.GetOverlappedResult

            def read(handle, buffer, size, synchronous_count, operation):
                self.assertIsNone(synchronous_count)
                self.assertIsNotNone(operation)
                return native_read(handle, buffer, size, synchronous_count, operation)

            with (
                patch.object(pipe.kernel, "ReadFile", side_effect=read) as reads,
                patch.object(
                    pipe.kernel, "GetOverlappedResult", wraps=native_completed
                ) as completions,
            ):
                pipe.writer.write(payload)
                received = bytearray()
                deadline = time.monotonic() + 5
                while len(received) < len(payload) and time.monotonic() < deadline:
                    part = pipe.read_available(7)
                    self.assertLessEqual(len(part), 7)
                    received.extend(part)
                    time.sleep(0.001)
                self.assertEqual(received, payload)
                self.assertGreater(reads.call_count, 0)
                self.assertGreaterEqual(completions.call_count, reads.call_count)
                self.assertIsNone(pipe._pending)
                self.assertFalse(pipe.read_pending)
