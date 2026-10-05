"""A missing required UI cell must not be silently treated as an empty value."""

import importlib.util
import os
import unittest

if importlib.util.find_spec("PySide6") is not None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.qt_table_items import table_item
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem


@unittest.skipUnless(importlib.util.find_spec("PySide6") is not None, "Qt runtime")
class PopulatedTableItemsTests(unittest.TestCase):
    def test_required_cell_preserves_identity_and_missing_cell_refuses(self):
        app = QApplication.instance() or QApplication([])
        table = QTableWidget(1, 2)
        self.addCleanup(table.close)
        item = QTableWidgetItem("verified snapshot")
        item.setData(Qt.ItemDataRole.UserRole, "exact-version")
        table.setItem(0, 0, item)
        self.assertIs(table_item(table, 0, 0), item)
        self.assertEqual(
            table_item(table, 0, 0).data(Qt.ItemDataRole.UserRole), "exact-version"
        )
        with self.assertRaisesRegex(ValueError, "populated table cell"):
            table_item(table, 0, 1)
        self.assertIsNone(table.item(0, 1))
        _ = app


if __name__ == "__main__":
    unittest.main()
