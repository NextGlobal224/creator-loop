"""Exercise image correction dialog validation and locator precision."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.image_evidence_ui import ImageEvidenceDialog
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication, QDialogButtonBox
    except ImportError:
        QApplication = None
else:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ImageEvidenceDialogTests(unittest.TestCase):
    def test_correction_requires_reason_and_preserves_untouched_locator(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        original_region = {
            "x": 0.1234567891234567,
            "y": 0.2345678912345678,
            "width": 0.4444444444444444,
            "height": 0.5555555555555555,
        }
        dialog = ImageEvidenceDialog(
            image,
            correction=True,
            region=original_region,
            content="Vùng màu xanh",
        )
        self.addCleanup(dialog.close)
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

        self.assertEqual(dialog.content.text(), "Vùng màu xanh")
        self.assertEqual(dialog.region(), original_region)
        self.assertIn("1234567891234567", dialog.coords["x"].text())
        self.assertFalse(dialog.preview.pixmap().isNull())
        self.assertFalse(ok.isEnabled())
        dialog.reason.setText("Sửa mô tả")
        self.assertTrue(ok.isEnabled())
        dialog.content.setText("Vùng xanh đã sửa")
        self.assertEqual(dialog.region(), original_region)
        dialog.actor.clear()
        self.assertFalse(ok.isEnabled())
        dialog.actor.setText("reviewer")
        self.assertTrue(ok.isEnabled())
        dialog.coords["x"].setValue(0.375)
        self.assertEqual(dialog.region()["x"], 0.375)
        self.assertEqual(dialog.region()["y"], original_region["y"])
        app.processEvents()

    def test_create_dialog_keeps_prior_behavior_and_rejects_no_pixel_crop(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        dialog = ImageEvidenceDialog(image)
        self.addCleanup(dialog.close)
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertEqual(
            dialog.region(),
            {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        )
        self.assertFalse(ok.isEnabled())
        dialog.content.setText("Toàn ảnh")
        self.assertTrue(ok.isEnabled())
        dialog.coords["width"].setValue(0.0)
        self.assertFalse(ok.isEnabled())

        empty = ImageEvidenceDialog(QImage())
        self.addCleanup(empty.close)
        empty.content.setText("Ảnh rỗng")
        self.assertFalse(
            empty.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        )
        app.processEvents()
