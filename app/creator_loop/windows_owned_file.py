"""Exclusive Windows original-file creation and handle-targeted rollback."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if sys.platform == "win32":
    _WindowsDLL = ctypes.WinDLL
else:
    _WindowsDLL = ctypes.CDLL


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


def _kernel32() -> _WindowsDLL:
    if sys.platform != "win32":
        raise OSError("TEXT original intake requires Windows handle semantics")
    if os.name != "nt":
        raise OSError("TEXT original intake requires Windows handle semantics")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _mark_for_deletion(kernel32: _WindowsDLL, handle: int) -> None:
    if sys.platform != "win32":
        raise OSError("TEXT original intake requires Windows handle semantics")
    disposition = _FileDispositionInfo(1)
    if not kernel32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


class OwnedWindowsFile:
    """The stream owns the one native handle until success or disposition."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream

    @classmethod
    def create_new(cls, path: Path) -> OwnedWindowsFile:
        if sys.platform != "win32":
            raise OSError("TEXT original intake requires Windows handle semantics")
        import msvcrt

        kernel32 = _kernel32()
        handle = kernel32.CreateFileW(
            str(path),
            _GENERIC_READ | _GENERIC_WRITE | _DELETE,
            0,
            None,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise FileExistsError(error, os.strerror(error), str(path))
            raise ctypes.WinError(error)

        # Until open_osfhandle succeeds, the raw handle is ours. Afterwards
        # the fd (and then fdopen's stream) is its only owner.
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_NOINHERIT)
        except BaseException as exc:
            try:
                _mark_for_deletion(kernel32, handle)
            except BaseException as cleanup_error:
                exc.add_note(f"Owned-file cleanup failed: {cleanup_error}")
            finally:
                kernel32.CloseHandle(handle)
            raise
        try:
            stream = os.fdopen(fd, "r+b", buffering=0)
        except BaseException as exc:
            try:
                _mark_for_deletion(kernel32, msvcrt.get_osfhandle(fd))
            except BaseException as cleanup_error:
                exc.add_note(f"Owned-file cleanup failed: {cleanup_error}")
            finally:
                os.close(fd)
            raise
        return cls(stream)

    def discard(self) -> None:
        """Mark this still-open file, rather than its pathname, for deletion."""
        if sys.platform != "win32":
            raise OSError("TEXT original intake requires Windows handle semantics")
        import msvcrt

        _mark_for_deletion(_kernel32(), msvcrt.get_osfhandle(self.stream.fileno()))

    def close(self) -> None:
        self.stream.close()
