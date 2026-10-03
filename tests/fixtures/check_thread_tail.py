"""Isolated native FLS tail: Library must wait past Qt finished for OS exit."""

import ctypes
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from creator_loop.database import initialize
from creator_loop.library_ui import LibraryWindow, VideoEvidenceWorker
from creator_loop.owned_process import _api
from PySide6.QtWidgets import QApplication


def main():
    # Callback is called on thread exit and index free; retain through FlsFree.
    # https://learn.microsoft.com/en-us/windows/win32/api/winnt/nc-winnt-pfls_callback_function
    app = QApplication([])
    kernel = _api()
    entered = threading.Event()
    release = threading.Event()
    callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)

    @callback_type
    def tail(_value):
        entered.set()
        release.wait(15)

    kernel.FlsAlloc.argtypes = (callback_type,)
    kernel.FlsAlloc.restype = wintypes.DWORD
    kernel.FlsSetValue.argtypes = (wintypes.DWORD, ctypes.c_void_p)
    kernel.FlsSetValue.restype = wintypes.BOOL
    kernel.FlsFree.argtypes = (wintypes.DWORD,)
    kernel.FlsFree.restype = wintypes.BOOL
    index = kernel.FlsAlloc(tail)
    assert index != 0xFFFFFFFF

    def wait_until(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate()

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve(strict=True)
        initialize(root / "creator_loop.sqlite3")
        window = LibraryWindow(root)
        window.show()

        class TailWorker(VideoEvidenceWorker):
            def run(self):
                if not kernel.FlsSetValue(index, 1):
                    raise ctypes.WinError(ctypes.get_last_error())

        worker = TailWorker("load", "unused-fixture-file", root)
        after = []
        try:
            window._start_worker(worker, "Native exit fixture", lambda: after.append(1))
            wait_until(entered.is_set)
            app.processEvents()
            assert worker.isFinished()
            assert not window._worker_exit.terminal()
            assert window._worker is worker
            assert after == []
            assert not window.close()
            assert window.isVisible()
            release.set()
            wait_until(lambda: window._worker is None)
            assert not window.isVisible()
            assert after == []
        finally:
            release.set()
            if window._worker is worker:
                assert worker.wait(10000)
                window._on_finished()
            assert kernel.FlsFree(index)
            window.close()
    print("Native thread exit gate PASS")


if __name__ == "__main__":
    main()
