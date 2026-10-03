"""Retain the current Windows thread handle for exit proof; never terminate."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from creator_loop.owned_process import _api


class OwnedThreadExit:
    def __init__(self) -> None:
        self.kernel = _api()
        self.handle: int | None = None
        self.error: OSError | None = None

    def capture(self) -> None:
        """Connect directly to QThread.started: this runs on the worker thread."""
        if sys.platform != "win32":
            self.error = OSError("Native thread exit proof requires Windows")
            return
        value = wintypes.HANDLE()
        current = self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(
            current,
            self.kernel.GetCurrentThread(),
            current,
            ctypes.byref(value),
            0x00100000,
            False,
            0,
        ):
            self.error = ctypes.WinError(ctypes.get_last_error())
        elif value.value is None:
            self.error = OSError("Native thread handle is unavailable")
        else:
            self.handle = value.value

    def terminal(self) -> bool:
        if sys.platform != "win32" or self.handle is None:
            return False
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state not in (0, 0x102):
            self.error = ctypes.WinError(ctypes.get_last_error())
            return False
        return state == 0

    def close(self) -> None:
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
