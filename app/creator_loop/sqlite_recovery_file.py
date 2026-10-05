"""Pin an existing recovery DB inode while permitting the SQLite VFS to lock it."""

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
def hold_recovery_database(path: Path) -> Iterator[BinaryIO]:
    """OPEN_EXISTING, share RW for SQLite, deny DELETE; never create or remove.

    Caller owns its app lock and actual SQLite writer reservation during health.
    Sharing RW is not a sandbox against arbitrary raw file writes; final bytes
    and logical identity must still be checked before clearing the guard.
    """
    if sys.platform != "win32":
        raise OSError("Recovery inode ownership requires Windows")
    import msvcrt

    selected = path.absolute()
    if selected.resolve(strict=True) != selected:
        raise ValueError("Canonical recovery database required")
    before = _regular(selected)
    if before is None:
        raise ValueError("Existing unaliased recovery database required")
    kernel = _kernel32()
    native = kernel.CreateFileW(
        str(selected), 0xC0000000, 3, None, 3, 0x00200000 | 0x80, None
    )
    if native == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
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
    with stream:

        def verify() -> None:
            actual = _regular(selected)
            held = os.fstat(stream.fileno())
            identity = before.st_dev, before.st_ino
            if (
                actual is None
                or (actual.st_dev, actual.st_ino) != identity
                or (held.st_dev, held.st_ino) != identity
                or held.st_nlink != 1
            ):
                raise RuntimeError("Recovery database inode changed")

        verify()
        yield stream
        verify()
