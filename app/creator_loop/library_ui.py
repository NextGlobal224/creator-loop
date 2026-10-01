"""Small Windows Library view for importing and listing physical originals."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_loop.database import open_readonly
from creator_loop.media_intake import intake_image_original, intake_video_original
from creator_loop.text_intake import intake_text_original


class OriginalImportWorker(QThread):
    imported = Signal(str)
    failed = Signal(str)

    def __init__(self, kind: str, source: Path, root: Path) -> None:
        super().__init__()
        self.kind = kind
        self.source = source
        self.root = root

    def run(self) -> None:
        try:
            import_fn = {
                "TEXT": intake_text_original,
                "VIDEO": intake_video_original,
                "IMAGE": intake_image_original,
            }[self.kind]
            result = import_fn(self.source, root=self.root)
        except Exception as exc:
            self.failed.emit(f"Không thể nhập tệp: {exc}")
        else:
            self.imported.emit(result.asset_id)


class LibraryWindow(QMainWindow):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: OriginalImportWorker | None = None
        self.setWindowTitle("Creator Loop — Library")
        self.resize(900, 540)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel("Originals trong Library"))

        actions = QHBoxLayout()
        self._buttons: list[QPushButton] = []
        for kind, label, filter_text in (
            ("TEXT", "Nhập Text", "Text (*.txt *.md);;Tất cả tệp (*)"),
            ("VIDEO", "Nhập Video", "MP4 (*.mp4);;Tất cả tệp (*)"),
            ("IMAGE", "Nhập Image", "Image (*.png *.jpg *.jpeg);;Tất cả tệp (*)"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, k=kind, f=filter_text: self.choose_original(k, f)
            )
            self._buttons.append(button)
            actions.addWidget(button)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Loại", "Tên", "MIME", "Byte"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.reload()

    def reload(self) -> None:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            rows = db.execute(
                """SELECT a.media_type, a.display_name, f.mime_type, f.byte_size
                   FROM assets a JOIN asset_files f ON f.asset_id = a.asset_id
                   WHERE f.role = 'ORIGINAL' AND a.deleted_at IS NULL
                   ORDER BY a.created_at DESC, f.created_at DESC, f.file_id DESC"""
            ).fetchall()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                self.table.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )
        self.status.setText(f"{len(rows)} original(s)")

    def choose_original(self, kind: str, filter_text: str) -> None:
        if self._worker is not None:
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self, "Chọn tệp gốc", str(Path.home()), filter_text
        )
        if not selected:
            return
        self._worker = OriginalImportWorker(kind, Path(selected), self.root)
        self._worker.imported.connect(self._on_imported)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        for button in self._buttons:
            button.setEnabled(False)
        self.status.setText(f"Đang nhập {Path(selected).name}…")
        self._worker.start()

    def _on_imported(self, _asset_id: str) -> None:
        self.reload()

    def _on_failed(self, message: str) -> None:
        self.status.setText(message)
        QMessageBox.warning(self, "Lỗi nhập tệp", message)

    def _on_finished(self) -> None:
        worker = self._worker
        self._worker = None
        for button in self._buttons:
            button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self,
                "Đang nhập tệp",
                "Đợi quá trình nhập tệp hoàn tất trước khi đóng ứng dụng.",
            )
            event.ignore()
            return
        super().closeEvent(event)
