"""Narrow a populated table cell; a missing item is an invalid UI state."""

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem


def table_item(table: QTableWidget, row: int, column: int) -> QTableWidgetItem:
    item = table.item(row, column)
    if item is None:
        raise ValueError("Expected a populated table cell")
    return item
