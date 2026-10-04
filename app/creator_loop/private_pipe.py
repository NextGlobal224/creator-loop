"""Private overlapped read end: no synchronous pipe I/O on the Qt GUI thread.

https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-peeknamedpipe
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes as w
from types import TracebackType
from typing import Any, BinaryIO, cast
from uuid import uuid4

from creator_loop.owned_process import _api


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", w.DWORD),
        ("offset_high", w.DWORD),
        ("event", w.HANDLE),
    ]


def _stream(kernel: Any, handle: int, flags: int, mode: str) -> BinaryIO:
    if sys.platform != "win32":
        raise OSError("Private worker pipes require Windows")
    import msvcrt

    try:
        fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return cast(BinaryIO, os.fdopen(fd, mode, buffering=0))
    except BaseException:
        os.close(fd)
        raise


class PrivatePipe:
    kernel: Any
    reader: BinaryIO
    writer: BinaryIO
    _ready: bytes

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Private worker pipes require Windows")
        import msvcrt

        self.kernel = kernel = _api()
        pointer = ctypes.POINTER(_Overlapped)
        signatures = {
            "CreateNamedPipeW": (
                (
                    w.LPCWSTR,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    ctypes.c_void_p,
                ),
                w.HANDLE,
            ),
            "ConnectNamedPipe": ((w.HANDLE, pointer), w.BOOL),
            "CreateEventW": ((ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR), w.HANDLE),
            "ResetEvent": ((w.HANDLE,), w.BOOL),
            "ReadFile": (
                (w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), pointer),
                w.BOOL,
            ),
            "GetOverlappedResult": (
                (w.HANDLE, pointer, ctypes.POINTER(w.DWORD), w.BOOL),
                w.BOOL,
            ),
            "CancelIoEx": ((w.HANDLE, pointer), w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(kernel, name)
            method.argtypes, method.restype = arguments, result
        self.eof = False
        self._pending: tuple[Any, _Overlapped] | None = None
        self._ready = b""
        self.event: int | None = None
        name = rf"\\.\pipe\CreatorLoop-{uuid4().hex}"
        # One preconnected local client, first instance, noninheritable server.
        server = kernel.CreateNamedPipeW(
            name, 1 | 0x40000000 | 0x80000, 8, 1, 0, 65536, 0, None
        )
        if server in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        reader = _stream(kernel, server, os.O_RDONLY, "rb")
        writer = None
        try:
            self.event = kernel.CreateEventW(None, True, False, None)
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            client = kernel.CreateFileW(name, 0x40000000, 0, None, 3, 0x80, None)
            if client in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            writer = _stream(kernel, client, os.O_WRONLY, "wb")
            connection = _Overlapped()
            connection.event = self.event
            # The client connects before this call: ERROR_PIPE_CONNECTED
            # proves a good connection with no outstanding connect operation.
            if not kernel.ConnectNamedPipe(server, ctypes.byref(connection)):
                code = ctypes.get_last_error()
                if code != 535:
                    raise ctypes.WinError(code)
        except BaseException:
            if writer is not None:
                writer.close()
            reader.close()
            if self.event is not None:
                kernel.CloseHandle(self.event)
                self.event = None
            raise
        self.reader, self.writer = reader, writer
        self.read_handle = msvcrt.get_osfhandle(reader.fileno())

    def _poll_read(self) -> bytes | None:
        if sys.platform != "win32":
            raise OSError("Private worker pipes require Windows")
        if self._pending is None:
            return b""
        buffer, operation = self._pending
        count = w.DWORD()
        if not self.kernel.GetOverlappedResult(
            self.read_handle, ctypes.byref(operation), ctypes.byref(count), False
        ):
            code = ctypes.get_last_error()
            if code == 996:
                return None
            self._pending = None
            if code in (109, 995):
                self.eof = code == 109
                return b""
            raise ctypes.WinError(code)
        self._pending = None
        return bytes(buffer.raw[: count.value])

    @property
    def read_pending(self) -> bool:
        return self._pending is not None or bool(self._ready)

    def read_available(self, limit: int = 128 * 1024) -> bytes:
        if sys.platform != "win32":
            raise OSError("Private worker pipes require Windows")
        if type(limit) is not int or not 0 < limit <= 4 * 1024 * 1024:
            raise ValueError("Pipe read limit must be between 1 byte and 4 MiB")
        if self._pending is not None:
            completed = self._poll_read()
            if completed is None:
                return b""
            self._ready = completed
        if self._ready:
            data, self._ready = self._ready[:limit], self._ready[limit:]
            return data
        available = w.DWORD()
        if not self.kernel.PeekNamedPipe(
            self.read_handle, None, 0, None, ctypes.byref(available), None
        ):
            code = ctypes.get_last_error()
            if code == 109:
                self.eof = True
                return b""
            raise ctypes.WinError(code)
        if not available.value:
            return b""
        count = min(limit, available.value)
        buffer = ctypes.create_string_buffer(count)
        operation = _Overlapped()
        operation.event = self.event
        if not self.kernel.ResetEvent(self.event):
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = w.DWORD()
        if self.kernel.ReadFile(
            self.read_handle,
            buffer,
            count,
            ctypes.byref(transferred),
            ctypes.byref(operation),
        ):
            return buffer.raw[: transferred.value]
        code = ctypes.get_last_error()
        if code == 997:
            self._pending = (buffer, operation)
            return b""
        if code == 109:
            self.eof = True
            return b""
        raise ctypes.WinError(code)

    def try_close(self) -> bool:
        if sys.platform != "win32":
            raise OSError("Private worker pipes require Windows")
        self.writer.close()
        if self._pending is not None:
            operation = self._pending[1]
            if not self.kernel.CancelIoEx(self.read_handle, ctypes.byref(operation)):
                code = ctypes.get_last_error()
                if code != 1168:
                    raise ctypes.WinError(code)
            if self._poll_read() is None:
                return False
        self.reader.close()
        if self.event is not None:
            self.kernel.CloseHandle(self.event)
            self.event = None
        self._ready = b""
        return True

    def close(self) -> None:
        if not self.try_close():
            raise RuntimeError("Private pipe cancellation is still pending")

    def __enter__(self) -> PrivatePipe:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
