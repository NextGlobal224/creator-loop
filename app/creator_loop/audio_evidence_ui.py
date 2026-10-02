"""Select and replay an MP4 audio range in the Library UI."""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
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

from creator_loop.audio_evidence import DecodedAudioSegment


class AudioSegmentWidget(QWidget):
    def __init__(self, decoded: DecodedAudioSegment) -> None:
        super().__init__()
        self.start_ms = 0
        self.end_ms = decoded.duration_ms
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Nghe và đối chiếu đoạn âm thanh của MP4 gốc."))
        self.output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self.player.setSource(QUrl.fromLocalFile(str(decoded.anchor_path)))
        self.player.positionChanged.connect(self._stop_at_end)
        self.play_button = QPushButton("Nghe đoạn đã chọn")
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


class AudioEvidenceDialog(QDialog):
    def __init__(self, decoded: DecodedAudioSegment) -> None:
        super().__init__()
        self.setWindowTitle("Tạo Evidence từ Audio")
        self.resize(600, 360)
        layout = QVBoxLayout(self)
        self.segment = AudioSegmentWidget(decoded)
        layout.addWidget(self.segment)
        form = QFormLayout()
        self.start = QSpinBox()
        self.end = QSpinBox()
        self.start.setRange(0, decoded.duration_ms - 1)
        self.end.setRange(1, decoded.duration_ms)
        self.end.setValue(min(decoded.duration_ms, 1000))
        self.kind = QComboBox()
        self.kind.addItem("Lời nói", "SPEECH")
        self.kind.addItem("Âm thanh khác", "OTHER")
        self.content = QLineEdit()
        self.actor = QLineEdit("creator")
        form.addRow("Bắt đầu (ms)", self.start)
        form.addRow("Kết thúc (ms)", self.end)
        form.addRow("Loại quan sát", self.kind)
        form.addRow("Nội dung nghe được", self.content)
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


class AudioRangeView(QDialog):
    def __init__(
        self, content: str, decoded: DecodedAudioSegment, start_ms: int, end_ms: int
    ) -> None:
        super().__init__()
        self.setWindowTitle("Đoạn Evidence audio đã xác minh")
        self.resize(600, 250)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{content} — {start_ms}–{end_ms} ms"))
        self.segment = AudioSegmentWidget(decoded)
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
