"""Select a retained machine segment explicitly; review remains a separate flow."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_loop.audio_evidence import DecodedAudioSegment
from creator_loop.audio_evidence_ui import AudioSegmentWidget
from creator_loop.qt_table_items import table_item
from creator_loop.transcript_review_command import TranscriptReviewCommand
from creator_loop.transcript_review_task import PAGE_SIZE

LIBRARY_REQUESTED = 23


class TranscriptAudioView(QDialog):
    """Keep the dialog/leases alive until the existing player proves cleanup."""

    def __init__(self, decoded: DecodedAudioSegment, start: int, end: int, parent):
        super().__init__(parent)
        self.close_pending = False
        self.close_result = 0
        self.setWindowTitle("Đối chiếu âm thanh gốc")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(650, 250)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"Đoạn máy gợi ý: {start}–{end} ms. Chưa duyệt Evidence.")
        )
        self.segment = AudioSegmentWidget(decoded)
        self.segment.set_range(start, end)
        self.segment.player.finished.connect(self._terminal)
        layout.addWidget(self.segment)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def done(self, result: int) -> None:
        player = self.segment.player
        if (
            player.process is not None
            or player.pipe is not None
            or player.held is not None
        ):
            self.close_pending, self.close_result = True, result
            self.segment.play_button.setEnabled(False)
            self.segment.stop()
            return
        super().done(result)

    def _terminal(self) -> None:
        if self.close_pending:
            super().done(self.close_result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.done(0)
        if self.close_pending:
            event.ignore()
        else:
            event.accept()


class TranscriptReviewWindow(QMainWindow):
    def __init__(self, root: Path, raw_file_id: str, display_name: str) -> None:
        super().__init__()
        if re.fullmatch("[0-9a-f]{32}", raw_file_id) is None:
            raise ValueError("Invalid retained RAW selection")
        self.root, self.raw_file_id = root, raw_file_id
        self.raw_sha256: str | None = None
        self.offset, self.total = 0, 0
        self.selected: dict | None = None
        self.preview: TranscriptAudioView | None = None
        self.close_pending = False
        self.library_requested = False
        self.command = TranscriptReviewCommand(root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.setWindowTitle("Creator Loop — Chọn đoạn chép lời máy")
        self.resize(900, 650)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            display_name
            + "\nChép lời MODEL có thể sai. Chọn đoạn, đối chiếu âm thanh gốc rồi tạo Evidence chờ duyệt. Sửa bằng HUMAN và review ở Library là các bước riêng."
        )
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ("Bắt đầu (ms)", "Kết thúc (ms)", "Text máy — xem trước")
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._select)
        layout.addWidget(self.table)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText("Chưa chọn đoạn. Nội dung máy được giữ nguyên.")
        layout.addWidget(self.text)
        row = QHBoxLayout()
        self.previous_button = QPushButton("Trang trước")
        self.previous_button.clicked.connect(
            lambda: self._request("page", max(0, self.offset - PAGE_SIZE))
        )
        self.next_button = QPushButton("Trang sau")
        self.next_button.clicked.connect(
            lambda: self._request("page", self.offset + PAGE_SIZE)
        )
        self.preview_button = QPushButton("Đối chiếu âm thanh gốc")
        self.preview_button.clicked.connect(lambda: self._selected_action("preview"))
        self.create_button = QPushButton("Tạo Evidence MODEL chờ duyệt")
        self.create_button.clicked.connect(lambda: self._selected_action("create"))
        for button in (
            self.previous_button,
            self.next_button,
            self.preview_button,
            self.create_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.load_button = QPushButton("Đọc lại RAW")
        self.load_button.clicked.connect(self._reload)
        self.cancel_button = QPushButton("Hủy tác vụ")
        self.cancel_button.clicked.connect(self.command.cancel)
        self.library_button = QPushButton("Mở lại Library")
        self.library_button.clicked.connect(self._library)
        for button in (self.load_button, self.cancel_button, self.library_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel("Chưa đọc RAW. Chưa chọn hoặc tạo Evidence.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self._refresh()

    def _refresh(self) -> None:
        idle = not self.command.busy and self.preview is None and not self.close_pending
        for control in (self.table, self.load_button, self.library_button):
            control.setEnabled(idle)
        self.previous_button.setEnabled(idle and self.offset > 0)
        self.next_button.setEnabled(idle and self.offset + PAGE_SIZE < self.total)
        for button in (self.preview_button, self.create_button):
            button.setEnabled(idle and self.selected is not None)
        self.cancel_button.setEnabled(
            self.command.busy and self.command.cleanup_deadline is None
        )

    def _reload(self) -> None:
        self.raw_sha256 = None
        self._request("page", 0)

    def _request(self, action: str, index: int) -> None:
        if self.command.busy or self.preview is not None:
            return
        try:
            self.command.start_review(
                self.raw_file_id, action=action, index=index, raw_sha256=self.raw_sha256
            )
            self.status.setText(
                "Đang đọc và kiểm nguồn…"
                if action != "create"
                else "Đang tạo Evidence MODEL chờ duyệt…"
            )
        except (OSError, ValueError, RuntimeError):
            self._failed(
                "Không khởi chạy được; giữ dữ liệu và kiểm tra lịch sử trước khi thử lại."
            )
        self._refresh()

    def _select(self) -> None:
        if self.command.busy:
            return
        row = self.table.currentRow()
        self.selected = None
        self.text.clear()
        if row >= 0 and self.table.selectedItems():
            item = self.table.item(row, 0)
            if item is not None:
                self._request("segment", item.data(Qt.ItemDataRole.UserRole))
        self._refresh()

    def _selected_action(self, action: str) -> None:
        if self.selected is not None:
            self._request(action, self.selected["index"])

    def _finished(self, code: int, raw: str) -> None:
        try:
            body = json.loads(raw)
            if (code, body["status"]) not in (
                (0, "SUCCEEDED"),
                (3, "FAILED"),
                (4, "FAILED"),
            ):
                raise ValueError("Unexpected review result")
            if code:
                self._failed(
                    "Không xác nhận kết quả; kiểm tra RAW/nguồn và lịch sử trước khi thử lại."
                )
                return
            data, action = body["data"], body["action"]
            if (
                data["raw_file_id"] != self.raw_file_id
                or re.fullmatch("[0-9a-f]{64}", data["raw_sha256"]) is None
            ):
                raise ValueError("Unexpected retained RAW identity")
            self.raw_sha256 = data["raw_sha256"]
            if action == "page":
                self.offset, self.total = data["offset"], data["total"]
                self.selected = None
                self.text.clear()
                self.table.blockSignals(True)
                try:
                    self.table.setRowCount(len(data["segments"]))
                    for row, segment in enumerate(data["segments"]):
                        for column, key in enumerate(("start_ms", "end_ms", "preview")):
                            self.table.setItem(
                                row, column, QTableWidgetItem(str(segment[key]))
                            )
                        table_item(self.table, row, 0).setData(
                            Qt.ItemDataRole.UserRole, segment["index"]
                        )
                    self.table.clearSelection()
                    self.table.setCurrentCell(-1, -1)
                finally:
                    self.table.blockSignals(False)
                self.status.setText(
                    f"{self.total} đoạn máy. Chọn rõ một đoạn; chưa tạo Evidence."
                )
            elif action == "segment":
                self.selected = data
                self.text.setPlainText(data["text"])
                self.status.setText(
                    f"Đoạn {data['start_ms']}–{data['end_ms']} ms chưa được duyệt."
                )
            elif action == "preview":
                if self.close_pending:
                    self._terminal_close()
                    return
                decoded = data["decoded"]
                if decoded["data_root"] != str(self.root) or not Path(
                    decoded["anchor_path"]
                ).is_relative_to(self.root):
                    raise ValueError("Audio anchor is outside this root")
                metadata = DecodedAudioSegment(
                    **{
                        **decoded,
                        "anchor_path": Path(decoded["anchor_path"]),
                        "data_root": self.root,
                    }
                )
                self.preview = TranscriptAudioView(
                    metadata, data["start_ms"], data["end_ms"], self
                )
                self.preview.finished.connect(self._preview_finished)
                self.preview.show()
            elif action == "create":
                self.selected = None
                self.table.clearSelection()
                self.text.clear()
                self.status.setText(
                    "Đã tạo Evidence MODEL chờ duyệt. Mở Library để xem, sửa HUMAN hoặc review riêng."
                )
            self._terminal_close()
            self._refresh()
        except (ValueError, KeyError, TypeError, OSError, RuntimeError):
            self._failed(
                "Kết quả chưa xác nhận; giữ dữ liệu và kiểm tra lịch sử trước khi tạo lại."
            )

    def _preview_finished(self, _code: int) -> None:
        if self.preview is not None:
            self.preview.deleteLater()
            self.preview = None
        self._terminal_close()
        self._refresh()

    def _failed(self, message: str) -> None:
        self.status.setText(message)
        self.selected = None
        self._terminal_close()
        self._refresh()

    def _library(self) -> None:
        if not self.command.busy and self.preview is None:
            self.library_requested = True
            self.close()

    def _terminal_close(self) -> None:
        if self.close_pending and not self.command.busy and self.preview is None:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.command.busy or self.preview is not None:
            self.close_pending = True
            if self.command.busy:
                self.command.cancel()
            if self.preview is not None:
                self.preview.reject()
            self._refresh()
            event.ignore()
        else:
            event.accept()


def run_transcript_review(root: Path, raw_file_id: str, display_name: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = TranscriptReviewWindow(root, raw_file_id, display_name)
    window.show()
    result = app.exec()
    window.command.shutdown()
    return LIBRARY_REQUESTED if window.library_requested else result
