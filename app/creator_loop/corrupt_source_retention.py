"""Exclusive original DB/sidecar handles for guarded Windows source retention."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from creator_loop.corrupt_database import _NAMES, _identity, _regular
from creator_loop.corrupt_source_preservation import _rename_by_handle
from creator_loop.windows_owned_file import _INVALID_HANDLE_VALUE, _kernel32


class CorruptSourcePin:
    """Read-only Python stream; native WRITE right permits post-rename fsync.

    Caller owns AppDataLock and retains canonical root/backups/target directory
    handles. Hold every present original source file before journaling/renaming.
    This does not itself create a runtime guard or authorize data replacement.
    Close only releases the handle; no discard/delete/cleanup is performed.
    """

    def __init__(self, path: Path, stream: BinaryIO, observed: os.stat_result) -> None:
        self.source = path
        self.stream = stream
        self.observed = observed
        self.retained_path: Path | None = None

    def verify(self) -> None:
        selected = self.retained_path or self.source
        actual = _regular(selected)
        if (
            actual is None
            or _identity(actual) != _identity(self.observed)
            or _identity(os.fstat(self.stream.fileno())) != _identity(self.observed)
        ):
            raise RuntimeError("Pinned damaged-source identity or bytes changed")

    def retain(self, directory: Path) -> Path:
        """Rename this exact inode without replacing any existing destination.

        A guard must already be durable. On a failure after native rename the
        destination may exist despite no returned path; preserve both locations
        and resolve the outcome by actual identity/bytes, not exception/phase.
        """
        if self.retained_path is not None:
            raise RuntimeError("Original damaged file already retained")
        selected = directory.absolute()
        if (
            selected.parent != self.source.parent / "backups"
            or re.fullmatch(r"corrupt-original-[0-9a-f]{32}", selected.name) is None
            or selected.resolve(strict=True) != selected
            or selected.is_symlink()
            or selected.is_junction()
            or not selected.is_dir()
        ):
            raise ValueError("New owned original-source retention directory required")
        self.verify()
        target = selected / self.source.name
        _rename_by_handle(target, self.stream)
        self.retained_path = target
        self.verify()
        return target


@contextmanager
def hold_existing_corrupt_source(path: Path) -> Iterator[CorruptSourcePin]:
    """OPEN_EXISTING exclusive native handle; never create/follow/delete source.

    Windows-only: portable path moves cannot provide these ownership/sharing
    guarantees. The caller holds the app lock through the readonly→exclusive
    lease gap, and freshly checks expected damage bytes before any mutation.
    """
    if sys.platform != "win32":
        raise OSError("Guarded damaged-source retention requires Windows handles")
    selected = path.absolute()
    if selected.name not in _NAMES or selected.resolve(strict=True) != selected:
        raise ValueError("Canonical original database/sidecar path required")
    before = _regular(selected)
    if before is None:
        raise ValueError("Existing original database/sidecar required")
    import msvcrt

    kernel = _kernel32()
    # READ|WRITE|DELETE, share=0, OPEN_EXISTING, OPEN_REPARSE_POINT.
    native = kernel.CreateFileW(str(selected), 0xC0010000, 0, None, 3, 0x00200000, None)
    if native == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(native, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel.CloseHandle(native)
        raise
    try:
        stream = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        observed = os.fstat(stream.fileno())
        if _identity(observed) != _identity(before):
            raise RuntimeError("Damaged source changed before exclusive lease")
        pin = CorruptSourcePin(selected, stream, observed)
        pin.verify()
        yield pin
        pin.verify()
