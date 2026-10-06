"""One cooperating app/updater per data root, released by the OS on process exit."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from creator_loop.windows_owned_file import _INVALID_HANDLE_VALUE, _kernel32
from creator_loop.windows_paths import file_io_path


class DataRootBusy(RuntimeError):
    pass


class AppDataLock:
    """Persistent coordination file; never delete it or infer ownership from PID.

    Windows denies all sharing while the handle is held. Portable flock exists
    for source CI. This is an app/updater protocol, not a SQLite security sandbox.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.path = self.root / "runtime" / "app-data.lock"
        self._handle: int | None = None
        self._file: BinaryIO | None = None

    def __enter__(self) -> AppDataLock:
        if self._handle is not None or self._file is not None:
            raise RuntimeError("Lock object is already held")
        runtime = self.path.parent
        if not runtime.is_dir() or runtime.is_symlink() or runtime.is_junction():
            raise OSError("A real data-root runtime directory is required")
        if self.path.is_symlink() or self.path.is_junction():
            raise OSError("App lock must not be a linked file")
        if sys.platform == "win32":
            kernel = _kernel32()
            handle = kernel.CreateFileW(
                str(file_io_path(self.path)),
                0x80000000 | 0x40000000,
                0,
                None,
                4,
                0x80,
                None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    raise DataRootBusy("Data root is in use by another app/updater")
                raise ctypes.WinError(error)
            self._handle = handle
        else:
            import fcntl

            stream = self.path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                stream.close()
                raise DataRootBusy(
                    "Data root is in use by another app/updater"
                ) from exc
            except BaseException:
                stream.close()
                raise
            self._file = stream
        return self

    @property
    def held(self) -> bool:
        """Whether this coordination object still owns its acquired OS lock."""
        return self._handle is not None or self._file is not None

    def close(self) -> None:
        if self._handle is not None:
            if sys.platform != "win32":
                raise OSError("Cannot release a Windows lock on another platform")
            kernel = _kernel32()
            if not kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
