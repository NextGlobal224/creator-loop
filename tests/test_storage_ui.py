"""Exercise real Qt registration, availability and confirmed relocation."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.database import initialize, open_readonly
    from creator_loop.storage_paths import resolve_storage_path
    from creator_loop.storage_roots import StorageRootError, list_storage_roots
    from creator_loop.storage_ui import StorageDialog
    from creator_loop.text_intake import intake_text_original
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Requires Windows and Qt")
class StorageUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "User data Huế"
        self.root.mkdir()
        initialize(self.root / "creator_loop.sqlite3")
        self.media = Path(self.temp.name) / "Kho Đà Nẵng"
        self.media.mkdir()
        source = Path(self.temp.name) / "Tư liệu.txt"
        source.write_bytes(b"Exact original")
        self.imported = intake_text_original(source, root=self.root)

    def wait(self, dialog) -> None:
        deadline = time.monotonic() + 10
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(dialog._worker)
        self.app.processEvents()

    def dialog(self, asset: bool = True):
        dialog = StorageDialog(self.root, self.imported.asset_id if asset else "")
        self.addCleanup(dialog.close)
        self.wait(dialog)
        return dialog

    def register(self, dialog) -> None:
        with patch(
            "creator_loop.storage_ui.QFileDialog.getExistingDirectory",
            return_value=str(self.media),
        ):
            dialog.register_button.click()
        self.wait(dialog)

    def test_register_and_move_real_bytes_requires_explicit_confirmation(self) -> None:
        dialog = self.dialog()
        self.register(dialog)
        self.assertEqual(len(list_storage_roots(self.root)), 1)
        self.assertEqual(dialog.stores.count(), 1)
        self.assertFalse(dialog.move_button.isEnabled())
        old = resolve_storage_path(self.root, "ORIGINAL", self.imported.storage_key)
        dialog.confirm.setChecked(True)
        self.assertTrue(dialog.move_button.isEnabled())
        dialog.move_button.click()
        self.assertFalse(dialog.register_button.isEnabled())
        self.wait(dialog)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            key, asset = db.execute(
                "SELECT storage_key,asset_id FROM asset_files WHERE file_id=?",
                (self.imported.file_id,),
            ).fetchone()
        self.assertEqual(asset, self.imported.asset_id)
        self.assertEqual(
            resolve_storage_path(self.root, "ORIGINAL", key).read_bytes(),
            old.read_bytes(),
        )
        self.assertTrue(key.startswith("registered/"))
        self.assertFalse(dialog.confirm.isChecked())
        self.assertIn("giữ nguyên ID", dialog.status.text())

    def test_changed_volume_is_visible_and_move_disabled(self) -> None:
        dialog = self.dialog()
        self.register(dialog)
        with patch(
            "creator_loop.storage_ui.resolve_registered_root",
            side_effect=StorageRootError("volume changed"),
        ):
            dialog.refresh_button.click()
            self.wait(dialog)
        dialog.confirm.setChecked(True)
        self.assertIn("Unavailable", dialog.stores.currentText())
        self.assertFalse(dialog.move_button.isEnabled())
        self.assertEqual(len(list_storage_roots(self.root)), 1)

    def test_failed_registration_reports_failure_without_creating_root(self) -> None:
        dialog = self.dialog(False)
        with patch(
            "creator_loop.storage_ui.QFileDialog.getExistingDirectory",
            return_value=str(self.root),
        ):
            dialog.register_button.click()
        self.wait(dialog)
        self.assertIn("Không thể", dialog.status.text())
        self.assertEqual(list_storage_roots(self.root), [])
        self.assertFalse(dialog.move_button.isEnabled())

    def test_library_routes_selected_asset_to_storage_dialog(self) -> None:
        from creator_loop.library_ui import LibraryWindow

        window = LibraryWindow(self.root)
        self.addCleanup(window.close)
        window.table.selectRow(0)
        with patch("creator_loop.library_ui.StorageDialog") as dialog:
            window.choose_storage()
        dialog.assert_called_once_with(self.root, self.imported.asset_id)
        dialog.return_value.exec.assert_called_once()

    def test_cancel_button_requests_owned_worker_interruption_and_keeps_keys(
        self,
    ) -> None:
        from creator_loop.storage_relocation import relocate_asset_files

        dialog = self.dialog()
        self.register(dialog)
        entered = threading.Event()

        def delayed(root, files, target, *, cancelled):
            entered.set()
            deadline = time.monotonic() + 5
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not cancelled():
                raise TimeoutError("Test did not request cancellation")
            return relocate_asset_files(root, files, target, cancelled=cancelled)

        with patch("creator_loop.storage_ui.relocate_asset_files", side_effect=delayed):
            dialog.confirm.setChecked(True)
            dialog.move_button.click()
            self.assertTrue(entered.wait(5))
            self.assertTrue(dialog.cancel_button.isEnabled())
            dialog.cancel_button.click()
            self.wait(dialog)
        self.assertIn("Đã hủy", dialog.status.text())
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            key = db.execute(
                "SELECT storage_key FROM asset_files WHERE file_id=?",
                (self.imported.file_id,),
            ).fetchone()[0]
        self.assertEqual(key, self.imported.storage_key)
        self.assertFalse(dialog.cancel_button.isEnabled())

    def test_dialog_stays_alive_until_owned_worker_finishes(self) -> None:
        from creator_loop.storage_roots import register_storage_root

        dialog = self.dialog()
        dialog.show()
        entered, release = threading.Event(), threading.Event()

        def delayed(root, path):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release registration")
            return register_storage_root(root, path)

        with patch(
            "creator_loop.storage_ui.register_storage_root", side_effect=delayed
        ):
            dialog.start("register", str(self.media))
            try:
                self.assertTrue(entered.wait(5))
                dialog.reject()
                dialog.done(0)
                dialog.close()
                self.app.processEvents()
                self.assertIsNotNone(dialog._worker)
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.buttons.isEnabled())
            finally:
                release.set()
                self.wait(dialog)
        self.assertTrue(dialog.buttons.isEnabled())
