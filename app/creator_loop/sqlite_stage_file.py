"""Reserve a new owned SQLite target while allowing the SQLite VFS to write it."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from creator_loop.corrupt_database import _regular
from creator_loop.windows_owned_file import _INVALID_HANDLE_VALUE, _kernel32


@contextmanager
def hold_new_sqlite_target(path: Path) -> Iterator[BinaryIO]:
    """CREATE_NEW pins the inode, permitting SQLite RW but denying replacement.

    The caller owns and retains the canonical parent directory and app lock.
    No DELETE access or sharing is granted; close never deletes partial evidence.
    This is not an SQLite writer lock or a sandbox against arbitrary file writes.
    Portable CI can only prove exclusive creation and path identity checks.
    """
    selected = path.absolute()
    parent = selected.parent
    if (
        parent.resolve(strict=True) != parent
        or parent.is_symlink()
        or parent.is_junction()
        or not parent.is_dir()
    ):
        raise ValueError("A real owned SQLite target parent is required")
    if sys.platform == "win32":
        import msvcrt

        kernel = _kernel32()
        # Read/write, no DELETE; share R/W for SQLite, no DELETE sharing.
        native = kernel.CreateFileW(str(selected), 0xC0000000, 3, None, 1, 0x80, None)
        if native == _INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            if code in (80, 183):
                raise FileExistsError("SQLite stage target already exists")
            raise ctypes.WinError(code)
        try:
            fd = msvcrt.open_osfhandle(native, os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            kernel.CloseHandle(native)
            raise
        try:
            stream = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise
    else:
        stream = selected.open("x+b")
    with stream:
        created = os.fstat(stream.fileno())

        def identity() -> None:
            actual = _regular(selected)
            held = os.fstat(stream.fileno())
            if (
                actual is None
                or (actual.st_dev, actual.st_ino) != (created.st_dev, created.st_ino)
                or (held.st_dev, held.st_ino) != (created.st_dev, created.st_ino)
                or held.st_nlink != 1
            ):
                raise RuntimeError("Reserved SQLite target path identity changed")

        identity()
        yield stream
        identity()
