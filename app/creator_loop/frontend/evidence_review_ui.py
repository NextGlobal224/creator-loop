"""Collect a human decision for the selected Evidence Version."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class EvidenceReviewDialog(QDialog):
    def __init__(self, content: str, state: str) -> None:
        super().__init__()
        self.setWindowTitle("Review Evidence Version")
        self.resize(540, 260)
        layout = QVBoxLayout(self)
        excerpt = QLabel(content)
        excerpt.setWordWrap(True)
        layout.addWidget(excerpt)
        layout.addWidget(QLabel(f"Trạng thái review hiện tại: {state}"))
        layout.addWidget(
            QLabel("Mở Evidence đúng loại để đối chiếu nguồn trước khi duyệt.")
        )
        form = QFormLayout()
        self.action = QComboBox()
        for label, value in (
            ("Chấp nhận", "ACCEPT"),
            ("Từ chối", "REJECT"),
            ("Yêu cầu sửa", "REQUEST_CHANGES"),
            ("Mở lại review", "REOPEN"),
        ):
            self.action.addItem(label, value)
        self.actor = QLineEdit("reviewer")
        self.reason = QLineEdit()
        form.addRow("Quyết định", self.action)
        form.addRow("Người review", self.actor)
        form.addRow("Lý do", self.reason)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.action.currentIndexChanged.connect(self._refresh)
        self.actor.textChanged.connect(self._refresh)
        self.reason.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        action = self.action.currentData()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.actor.text().strip())
            and (action == "ACCEPT" or bool(self.reason.text().strip()))
        )
