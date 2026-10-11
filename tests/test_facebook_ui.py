"""Real Qt regression: preserve QDialog APIs while providing source actions."""

import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.facebook_ui import FacebookDialog
from PySide6.QtWidgets import QApplication, QPushButton


class FacebookUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_keeps_native_open_method_and_bounded_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                self.assertTrue(callable(dialog.open))
                self.assertIsInstance(dialog.open_source, QPushButton)
                self.assertEqual(dialog.open_source.objectName(), "facebookOpen")
                self.assertEqual(dialog.limit.value(), 20)
                self.assertEqual(dialog.keyword.maxLength(), 200)
                self.assertFalse(dialog.stop.isEnabled())
                dialog.open_source.click()
                self.assertEqual(dialog.status.text(), "Chọn một nguồn đã lưu.")
                dialog.scan.click()
                self.assertEqual(dialog.status.text(), "Nhập từ khóa trước khi Quét.")
                self.assertIsNone(dialog.worker)
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    def test_existing_chrome_scan_requires_a_verified_selected_tab(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                self.assertEqual(dialog.browser_mode.currentData(), "existing")
                self.assertEqual(dialog.login.text(), "Kết nối Chrome đang mở")
                dialog.keyword.setText("fixture")
                dialog.scan.click()
                self.assertIsNone(dialog.worker)
                self.assertIn("Kết nối đúng tab Facebook", dialog.status.text())
                dialog.chrome_tabs.clear()
                dialog.chrome_tabs.addItem("selected", "target")
                dialog.verified_target = "target"
                dialog.chrome_tabs.addItem("another", "another")
                dialog.chrome_tabs.setCurrentIndex(1)
                self.assertIsNone(dialog.verified_target)
                dialog.browser_mode.setCurrentIndex(1)
                self.assertEqual(dialog.login.text(), "Đăng nhập riêng")
                self.assertIsNone(dialog.chrome_tabs.currentData())
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
