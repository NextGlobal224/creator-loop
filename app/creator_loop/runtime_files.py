"""Retained Windows handles for deletion of verified owned runtime entries."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes as w
from pathlib import Path
from types import TracebackType
from typing import Any

from creator_loop.windows_owned_file import (
    _INVALID_HANDLE_VALUE,
    _kernel32,
    _mark_for_deletion,
)


class _Information(ctypes.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("written", w.FILETIME),
        ("volume", w.DWORD),
        ("size_high", w.DWORD),
        ("size_low", w.DWORD),
        ("links", w.DWORD),
        ("index_high", w.DWORD),
        ("index_low", w.DWORD),
    ]


class RuntimeHandle:
    """Deny replacement while checking identity; never traverse/delete recursively."""

    kernel: Any
    path: Path
    info: _Information
    handle: int | None
    _can_discard: bool

    def __init__(
        self, path: Path, *, directory: bool = False, allow_child_writes: bool = False
    ) -> None:
        if allow_child_writes and not directory:
            raise ValueError("Child writes are only meaningful for a directory handle")
        if sys.platform != "win32":
            raise OSError("Owned runtime deletion requires Windows handles")
        self.kernel = _kernel32()
        self.path = path.absolute()
        self.handle: int | None = None
        self.info = _Information()
        self._can_discard = not allow_child_writes
        self.kernel.GetFileInformationByHandle.argtypes = (
            w.HANDLE,
            ctypes.POINTER(_Information),
        )
        self.kernel.GetFileInformationByHandle.restype = w.BOOL
        self.kernel.GetFinalPathNameByHandleW.argtypes = (
            w.HANDLE,
            w.LPWSTR,
            w.DWORD,
            w.DWORD,
        )
        self.kernel.GetFinalPathNameByHandleW.restype = w.DWORD
        self.kernel.ReadFile.argtypes = (
            w.HANDLE,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
            ctypes.c_void_p,
        )
        self.kernel.ReadFile.restype = w.BOOL
        # OPEN_REPARSE_POINT: inspect the actual entry, never follow a link.
        flags = 0x00200000 | (0x02000000 if directory else 0x80)
        access = 0x10000 | (0x81 if directory else 0x80000000)
        if allow_child_writes:
            # No DELETE access: the OS opens the rename destination parent with
            # sharing that excludes DELETE. Sharing WRITE allows adding children;
            # sharing DELETE remains denied to protect this directory itself.
            access = 0x81
        # A retained publication directory must allow the OS to open its parent
        # for adding/renaming children. DELETE sharing remains denied, so the
        # directory itself cannot be replaced while the handle is retained.
        sharing = 3 if allow_child_writes else 1
        handle = self.kernel.CreateFileW(
            str(self.path), access, sharing, None, 3, flags, None
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        try:
            if not self.kernel.GetFileInformationByHandle(
                handle, ctypes.byref(self.info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if (
                self.info.attributes & 0x400
                or bool(self.info.attributes & 0x10) != directory
            ):
                raise OSError("Runtime entry is linked or has the wrong type")
            if not directory and self.info.links != 1:
                raise OSError("Runtime entry is hardlinked")
            buffer = ctypes.create_unicode_buffer(32768)
            length = self.kernel.GetFinalPathNameByHandleW(
                handle, buffer, len(buffer), 0
            )
            if not 0 < length < len(buffer):
                raise ctypes.WinError(ctypes.get_last_error())
            final = buffer.value
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[8:]
            elif final.startswith("\\\\?\\"):
                final = final[4:]
            if Path(final) != self.path or self.path.resolve(strict=True) != self.path:
                raise OSError("Runtime handle target does not match the owned path")
        except BaseException:
            self.close()
            raise

    def read_json(self, *, reject_duplicates: bool = False) -> dict[str, Any]:
        if sys.platform != "win32":
            raise OSError("Runtime metadata reads require Windows handles")
        size = (self.info.size_high << 32) | self.info.size_low
        if not 0 < size <= 16384:
            raise ValueError("Runtime ownership metadata exceeds budget")
        buffer = ctypes.create_string_buffer(size)
        count = w.DWORD()
        if not self.kernel.ReadFile(
            self.handle, buffer, size, ctypes.byref(count), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value != size:
            raise OSError("Runtime ownership metadata read was incomplete")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Runtime metadata has duplicate fields")
                result[key] = value
            return result

        body = json.loads(
            buffer.raw.decode("utf-8"),
            object_pairs_hook=unique if reject_duplicates else None,
        )
        if not isinstance(body, dict):
            raise ValueError("Invalid runtime ownership metadata")
        return body

    def discard(self) -> None:
        if not self._can_discard:
            raise RuntimeError(
                "Publication directory handle cannot delete its directory"
            )
        if self.handle is None:
            raise RuntimeError("Runtime handle is already closed")
        _mark_for_deletion(self.kernel, self.handle)

    def close(self) -> None:
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> RuntimeHandle:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
