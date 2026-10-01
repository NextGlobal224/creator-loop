"""Exercise the actual Qt Library import flow on Windows with pinned PySide6."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.library_ui import LibraryWindow
        from PySide6.QtWidgets import QApplication, QFileDialog
    except ImportError:
        QApplication = None
else:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class LibraryUiTests(unittest.TestCase):
    def test_three_original_types_appear_after_worker_import(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Creator Loop"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()

        self.assertEqual(window.table.rowCount(), 0)
        samples = (
            ("TEXT", "Bản ghi Đà Nẵng.txt", "Một đoạn văn".encode("utf-8")),
            ("VIDEO", "Huế Kha.mp4", b"\x00\x00\x00\x18ftypisom" + b"sample"),
            ("IMAGE", "Ảnh mẫu.png", b"\x89PNG\r\n\x1a\n" + b"sample"),
        )
        for index, (kind, name, content) in enumerate(samples, start=1):
            source = Path(self.temp.name) / name
            source.write_bytes(content)
            with patch.object(
                QFileDialog, "getOpenFileName", return_value=(str(source), "")
            ):
                window.choose_original(kind, "Tất cả tệp (*)")
            self.assertIsNotNone(window._worker)
            self.assertTrue(all(not button.isEnabled() for button in window._buttons))

            deadline = time.monotonic() + 10
            while window._worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(window._worker)
            self.assertEqual(window.table.rowCount(), index)
            self.assertTrue(all(button.isEnabled() for button in window._buttons))

        shown = {
            (window.table.item(row, 0).text(), window.table.item(row, 1).text())
            for row in range(window.table.rowCount())
        }
        self.assertEqual(shown, {(kind, name) for kind, name, _ in samples})
