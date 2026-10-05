"""Explicit video transcription task; root lock belongs to its I/O worker."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from creator_loop.transcription_command import TranscriptionCommand

LIBRARY_REQUESTED = 23


class TranscriptionWindow(QMainWindow):
    def __init__(self, root: Path, file_id: str, display_name: str) -> None:
        super().__init__()
        if re.fullmatch("[0-9a-f]{32}", file_id) is None:
            raise ValueError("Invalid original selection")
        self.file_id = file_id
        self.close_pending = False
        self.library_requested = False
        self.command = TranscriptionCommand(root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.command.phase_changed.connect(self._phase)
        self.setWindowTitle("Creator Loop — Chép lời Video")
        self.resize(800, 400)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            "Video: "
            + display_name
            + "\nMỗi lần chạy tạo PCM và bản chép lời máy mới, giữ nguyên nguồn và lịch sử. Chép lời máy cần đối chiếu với âm thanh trước khi dùng làm Evidence."
        )
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        row = QHBoxLayout()
        row.addWidget(QLabel("Ngôn ngữ:"))
        self.language = QComboBox()
        self.language.addItem("Tiếng Việt", "vi")
        self.language.addItem("English", "en")
        row.addWidget(self.language)
        row.addWidget(QLabel("Bộ nhớ worker đã chọn:"))
        self.worker_budget = QComboBox()
        self.worker_budget.addItem("512 MiB", 512)
        self.worker_budget.addItem("768 MiB (Whisper base CPU)", 768)
        self.worker_budget.setCurrentIndex(1)
        row.addWidget(self.worker_budget)
        layout.addLayout(row)
        settings = QLabel(
            "Dùng engine/model đã kiểm và lưu trong lựa chọn cục bộ, với đúng budget ở trên. Nếu chưa lưu, quay lại Library → chọn engine/model. Kết quả chưa được duyệt tự động."
        )
        settings.setTextFormat(Qt.TextFormat.PlainText)
        settings.setWordWrap(True)
        layout.addWidget(settings)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("Tạo PCM và chép lời máy")
        self.start_button.clicked.connect(self._start)
        self.cancel_button = QPushButton("Hủy tác vụ")
        self.cancel_button.clicked.connect(self.command.cancel)
        self.library_button = QPushButton("Mở lại Library")
        self.library_button.clicked.connect(self._library)
        for button in (self.start_button, self.cancel_button, self.library_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status = QLabel("Sẵn sàng. Chưa tạo tác vụ.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        self.setCentralWidget(central)
        self._refresh()

    def _refresh(self) -> None:
        busy = self.command.busy
        for control in (
            self.start_button,
            self.language,
            self.worker_budget,
            self.library_button,
        ):
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and self.command.cancel_deadline is None)

    def _start(self) -> None:
        if self.command.busy:
            return
        try:
            self.command.start_task(
                self.file_id,
                language=self.language.currentData(),
                worker_memory_mib=self.worker_budget.currentData(),
            )
            self.status.setText("Đang kiểm nguồn và lựa chọn engine/model…")
        except (OSError, ValueError, RuntimeError):
            self._failed(
                "Không khởi chạy được. Dữ liệu giữ nguyên; kiểm nguồn và lựa chọn cục bộ."
            )
        self._refresh()

    def _phase(self, phase: str) -> None:
        self.status.setText(
            {
                "decode": "Đang tạo PCM từ âm thanh của video…",
                "transcribe": "PCM đã lưu. Đang chép lời bằng engine/model đã chọn…",
                "cancelling": "Đã yêu cầu hủy. Đang chờ tác vụ và cây worker thoát; giữ dữ liệu đã tạo…",
            }[phase]
        )
        self._refresh()

    def _finished(self, code: int, raw: str) -> None:
        try:
            body = json.loads(raw.splitlines()[-1])
            if (
                not isinstance(body, dict)
                or type(body.get("format")) is not int
                or body.get("format") != 1
                or body.get("event") != "result"
                or body.get("request_id") != self.command.request_id
                or (code, body.get("status"))
                not in (
                    (0, "SUCCEEDED"),
                    (2, "CANCELLED"),
                    (3, "FAILED"),
                    (4, "FAILED"),
                )
            ):
                raise ValueError("Task result mismatch")
            if code == 0:
                if any(
                    not isinstance(body.get(key), str)
                    or re.fullmatch("[0-9a-f]{32}", body[key]) is None
                    for key in (
                        "decode_run_id",
                        "audio_file_id",
                        "transcription_run_id",
                        "raw_file_id",
                        "provenance_file_id",
                    )
                ):
                    raise ValueError("Task output identities missing")
                message = "PCM và chép lời máy đã lưu. Chưa tạo hoặc duyệt Evidence. Mở lại Library để xem từng task và đối chiếu nguồn."
            elif code == 2:
                message = "Tác vụ đã hủy; dữ liệu đã tạo và lịch sử được giữ. Chạy lại tạo lượt mới."
            elif body.get("error") == "DATA_ROOT_BUSY":
                message = "Dữ liệu đang được phiên khác sử dụng; không ghi tác vụ. Đóng phiên đó rồi thử lại."
            else:
                message = "Chép lời chưa thành công. Kiểm nguồn, lựa chọn engine/model và budget; dữ liệu cùng RAW đã tạo được giữ."
            if code != 0 and body.get("audio_file_id"):
                message += " PCM của lượt này đã lưu thành công."
            self.status.setText(message + "\nLog: " + str(self.command.log_directory))
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            self._failed(
                "Chưa xác nhận được kết quả. Giữ dữ liệu/log và kiểm từng task khi mở lại Library."
            )
            return
        self._refresh()
        if self.close_pending:
            self.close()

    def _failed(self, message: str) -> None:
        self.status.setText(message + "\nLog: " + str(self.command.log_directory))
        self._refresh()
        if self.close_pending and not self.command.busy:
            self.close()

    def _library(self) -> None:
        if not self.command.busy:
            self.library_requested = True
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.command.busy:
            self.close_pending = True
            self.command.cancel()
            event.ignore()
            return
        super().closeEvent(event)


def run_transcription(root: Path, file_id: str, display_name: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = TranscriptionWindow(root, file_id, display_name)
    app.aboutToQuit.connect(window.command.shutdown)
    try:
        window.show()
        result = app.exec()
        return LIBRARY_REQUESTED if window.library_requested else result
    finally:
        app.aboutToQuit.disconnect(window.command.shutdown)
        window.command.shutdown()
