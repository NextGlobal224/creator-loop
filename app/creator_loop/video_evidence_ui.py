"""Select and replay a decoded video time range in the Library UI."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from creator_loop.video_evidence import DecodedVideoFrame


class VideoSegmentWidget(QWidget):
    def __init__(self, decoded: DecodedVideoFrame) -> None:
        super().__init__()
        self.start_ms = 0
        self.end_ms = decoded.duration_ms
        layout = QVBoxLayout(self)
        poster = QLabel()
        poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        poster.setPixmap(
            QPixmap.fromImage(decoded.image).scaled(
                480,
                180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        layout.addWidget(poster)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(180)
        layout.addWidget(self.video)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.player.setSource(QUrl.fromLocalFile(str(decoded.anchor_path)))
        self.player.positionChanged.connect(self._stop_at_end)
        self.play_button = QPushButton("Phát đoạn đã chọn")
        self.play_button.clicked.connect(self.play)
        layout.addWidget(self.play_button)

    def set_range(self, start_ms: int, end_ms: int) -> None:
        self.start_ms = start_ms
        self.end_ms = end_ms

    def play(self) -> None:
        self.player.pause()
        self.player.setPosition(self.start_ms)
        self.player.play()

    def _stop_at_end(self, position: int) -> None:
        if position >= self.end_ms:
            self.player.pause()

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())


class VideoEvidenceDialog(QDialog):
    def __init__(self, decoded: DecodedVideoFrame) -> None:
        super().__init__()
        self.setWindowTitle("Tạo Evidence từ Video")
        self.resize(720, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn đoạn theo milliseconds của MP4 đã giải mã."))
        self.segment = VideoSegmentWidget(decoded)
        layout.addWidget(self.segment)
        form = QFormLayout()
        self.start = QSpinBox()
        self.end = QSpinBox()
        self.start.setRange(0, decoded.duration_ms - 1)
        self.end.setRange(1, decoded.duration_ms)
        self.end.setValue(min(decoded.duration_ms, 1000))
        self.content = QLineEdit()
        self.actor = QLineEdit("creator")
        form.addRow("Bắt đầu (ms)", self.start)
        form.addRow("Kết thúc (ms)", self.end)
        form.addRow("Quan sát", self.content)
        form.addRow("Người tạo", self.actor)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.start.valueChanged.connect(self._refresh)
        self.end.valueChanged.connect(self._refresh)
        self.content.textChanged.connect(self._refresh)
        self.actor.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        start, end = self.start.value(), self.end.value()
        self.segment.set_range(start, end)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            start < end
            and bool(self.content.text().strip())
            and bool(self.actor.text().strip())
        )

    def done(self, result: int) -> None:
        self.segment.stop()
        super().done(result)


class VideoRangeView(QDialog):
    def __init__(
        self, content: str, decoded: DecodedVideoFrame, start_ms: int, end_ms: int
    ) -> None:
        super().__init__()
        self.setWindowTitle("Đoạn Evidence video đã xác minh")
        self.resize(720, 550)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{content} — {start_ms}–{end_ms} ms"))
        self.segment = VideoSegmentWidget(decoded)
        self.segment.set_range(start_ms, end_ms)
        layout.addWidget(self.segment)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def exec(self) -> int:
        QTimer.singleShot(0, self.segment.play)
        return super().exec()

    def done(self, result: int) -> None:
        self.segment.stop()
        super().done(result)
