"""Choose an exact NFC text range before creating a human Evidence Version."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)


class TextEvidenceDialog(QDialog):
    def __init__(
        self,
        snapshot: str,
        *,
        correction: bool = False,
        start: int = 0,
        end: int | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.correction = correction
        self.setWindowTitle(
            "Sửa Evidence Text" if correction else "Tạo Evidence từ Text"
        )
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn khoảng ký tự trên snapshot NFC đã xác minh."))
        view = QPlainTextEdit(snapshot)
        view.setReadOnly(True)
        layout.addWidget(view)

        form = QFormLayout()
        self.start = QSpinBox()
        self.end = QSpinBox()
        maximum = min(len(snapshot), 2_147_483_647)
        self.start.setRange(0, maximum)
        self.end.setRange(0, maximum)
        self.start.setValue(start)
        self.end.setValue(min(maximum, 120) if end is None else end)
        self.actor = QLineEdit("creator")
        self.reason = QLineEdit()
        form.addRow("Bắt đầu (code point)", self.start)
        form.addRow("Kết thúc (không gồm ký tự này)", self.end)
        form.addRow("Người sửa" if correction else "Người tạo", self.actor)
        if correction:
            form.addRow("Lý do sửa", self.reason)
        layout.addLayout(form)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.start.valueChanged.connect(self._refresh_preview)
        self.end.valueChanged.connect(self._refresh_preview)
        self.actor.textChanged.connect(self._refresh_preview)
        if correction:
            self.reason.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        valid = (
            self.start.value() < self.end.value()
            and bool(self.actor.text().strip())
            and (not self.correction or bool(self.reason.text().strip()))
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(valid)
        self.preview.setText(
            self.snapshot[self.start.value() : self.end.value()]
            if valid
            else "Chọn một đoạn và nhập người tạo."
        )
