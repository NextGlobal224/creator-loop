"""Small Windows Library view for importing and listing physical originals."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
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

from creator_loop.database import _connect_write, open_readonly
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.media_intake import intake_image_original, intake_video_original
from creator_loop.text_evidence import create_text_evidence, read_verified_text_snapshot
from creator_loop.text_evidence_ui import TextEvidenceDialog
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


class TextEvidenceWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        action: str,
        identifier: str,
        root: Path,
        *,
        start: int = 0,
        end: int = 0,
        actor: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.identifier = identifier
        self.root = root
        self.range_start = start
        self.range_end = end
        self.actor = actor

    def run(self) -> None:
        db_path = self.root / "creator_loop.sqlite3"
        try:
            if self.action == "create":
                with closing(_connect_write(db_path)) as db:
                    version = create_text_evidence(
                        db,
                        file_id=self.identifier,
                        data_root=self.root,
                        start=self.range_start,
                        end=self.range_end,
                        actor=self.actor,
                    )
                self.result.emit(version.evidence_version_id)
            elif self.action == "load":
                with closing(open_readonly(db_path)) as db:
                    _asset_id, snapshot = read_verified_text_snapshot(
                        db, self.identifier, self.root
                    )
                self.result.emit(snapshot)
            elif self.action == "reopen":
                with closing(open_readonly(db_path)) as db:
                    reopened = reopen_evidence_version(db, self.identifier, self.root)
                if (
                    reopened.locator_type != "TEXT_RANGE"
                    or reopened.text_excerpt is None
                ):
                    raise ValueError("Selected Evidence is not a text range")
                self.result.emit(reopened.text_excerpt)
            else:
                raise ValueError("Unknown Evidence action")
        except Exception as exc:
            self.failed.emit(f"Không thể xử lý Evidence: {exc}")


class LibraryWindow(QMainWindow):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: QThread | None = None
        self._evidence_result: object | None = None
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
        evidence_actions = QHBoxLayout()
        create_evidence = QPushButton("Tạo Evidence Text")
        create_evidence.clicked.connect(self.choose_text_evidence)
        self._buttons.append(create_evidence)
        evidence_actions.addWidget(create_evidence)
        reopen_evidence = QPushButton("Mở Evidence Text")
        reopen_evidence.clicked.connect(self.reopen_selected_text_evidence)
        self._buttons.append(reopen_evidence)
        evidence_actions.addWidget(reopen_evidence)
        layout.addLayout(evidence_actions)

        self.evidence_table = QTableWidget(0, 3)
        self.evidence_table.setHorizontalHeaderLabels(
            ("Evidence", "Đoạn đã lưu", "Version")
        )
        self.evidence_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.evidence_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.evidence_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.evidence_table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.reload()

    def reload(self) -> None:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            rows = db.execute(
                """SELECT a.media_type, a.display_name, f.mime_type,
                          f.byte_size, f.file_id
                   FROM assets a JOIN asset_files f ON f.asset_id = a.asset_id
                   WHERE f.role = 'ORIGINAL' AND a.deleted_at IS NULL
                   ORDER BY a.created_at DESC, f.created_at DESC, f.file_id DESC"""
            ).fetchall()
            evidence_rows = db.execute(
                """SELECT v.evidence_version_id, a.display_name, v.content,
                          v.version_no
                   FROM evidence_versions v
                   JOIN evidences e ON e.evidence_id = v.evidence_id
                   JOIN assets a ON a.asset_id = e.asset_id
                   WHERE e.evidence_type = 'DIRECT_TEXT' AND e.deleted_at IS NULL
                   ORDER BY v.created_at DESC, v.evidence_version_id DESC"""
            ).fetchall()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row[:4]):
                self.table.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )
            self.table.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, row[4])
        self.evidence_table.setRowCount(len(evidence_rows))
        for row_index, row in enumerate(evidence_rows):
            item = QTableWidgetItem(str(row[1]))
            item.setData(Qt.ItemDataRole.UserRole, row[0])
            self.evidence_table.setItem(row_index, 0, item)
            self.evidence_table.setItem(row_index, 1, QTableWidgetItem(str(row[2])))
            self.evidence_table.setItem(row_index, 2, QTableWidgetItem(str(row[3])))
        self.status.setText(f"{len(rows)} original(s)")

    def choose_original(self, kind: str, filter_text: str) -> None:
        if self._worker is not None:
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self, "Chọn tệp gốc", str(Path.home()), filter_text
        )
        if not selected:
            return
        worker = OriginalImportWorker(kind, Path(selected), self.root)
        worker.imported.connect(self._on_imported)
        worker.failed.connect(self._on_failed)
        self._start_worker(worker, f"Đang nhập {Path(selected).name}…")

    def _start_worker(
        self,
        worker: QThread,
        message: str,
        after_finished: Callable[[], None] | None = None,
    ) -> None:
        self._worker = worker
        worker.finished.connect(self._on_finished)
        if after_finished is not None:
            worker.finished.connect(after_finished)
        for button in self._buttons:
            button.setEnabled(False)
        self.status.setText(message)
        worker.start()

    def _start_evidence_worker(self, worker: TextEvidenceWorker, message: str) -> None:
        self._evidence_result = None
        worker.result.connect(self._store_evidence_result)
        worker.failed.connect(self._on_failed)
        self._start_worker(
            worker,
            message,
            lambda: self._finish_evidence(worker.action, worker.identifier),
        )

    def _store_evidence_result(self, result: object) -> None:
        self._evidence_result = result

    def choose_text_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0).text() != "TEXT":
            QMessageBox.information(self, "Chọn Text", "Chọn một original TEXT.")
            return
        file_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_evidence_worker(
            TextEvidenceWorker("load", str(file_id), self.root),
            "Đang xác minh snapshot TEXT…",
        )

    def reopen_selected_text_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Text.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_evidence_worker(
            TextEvidenceWorker("reopen", str(version_id), self.root),
            "Đang xác minh Evidence…",
        )

    def _finish_evidence(self, action: str, identifier: str) -> None:
        result = self._evidence_result
        self._evidence_result = None
        if result is None:
            return
        if action == "load":
            dialog = TextEvidenceDialog(str(result))
            if dialog.exec() == TextEvidenceDialog.DialogCode.Accepted:
                self._start_evidence_worker(
                    TextEvidenceWorker(
                        "create",
                        identifier,
                        self.root,
                        start=dialog.start.value(),
                        end=dialog.end.value(),
                        actor=dialog.actor.text(),
                    ),
                    "Đang ghi Evidence…",
                )
        elif action == "create":
            self.reload()
            self.status.setText("Evidence Text đã được tạo.")
        elif action == "reopen":
            QMessageBox.information(self, "Đoạn Evidence", str(result))

    def _on_imported(self, _asset_id: str) -> None:
        self.reload()

    def _on_failed(self, message: str) -> None:
        self.status.setText(message)
        QMessageBox.warning(self, "Không thể hoàn tất tác vụ", message)

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
                "Đang xử lý",
                "Đợi tác vụ hiện tại hoàn tất trước khi đóng ứng dụng.",
            )
            event.ignore()
            return
        super().closeEvent(event)
