"""Read-only local MP4 preview using the app's owned playback worker."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from creator_loop.backend.runtime.windows_paths import file_io_path, resolve_file_path
from creator_loop.backend.video.isolated_playback import IsolatedMediaPlayer
from creator_loop.shared.workflow import RenderResult


class VideoPreviewWidget(QWidget):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root.resolve(strict=True)
        self.result: RenderResult | None = None
        self.player: IsolatedMediaPlayer | None = None
        self._owner: QObject | None = None
        self._audio: QAudioOutput | None = None
        layout = QVBoxLayout(self)
        self.description = QLabel("Chưa có MP4 hoàn tất để preview.")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.video = QVideoWidget()
        self.video.setMinimumSize(180, 240)
        layout.addWidget(self.video)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setEnabled(False)
        self.seek.sliderReleased.connect(self._seek)
        layout.addWidget(self.seek)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Phát MP4 local")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.play)
        self.stop_button = QPushButton("Dừng preview")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)
        self.status = QLabel("File nháp chờ review; chưa xuất bản.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_result(self, result: RenderResult) -> None:
        if (
            result.status != "SUCCEEDED"
            or result.output_path is None
            or not isinstance(result.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", result.sha256) is None
            or type(result.byte_size) is not int
            or result.byte_size <= 0
            or type(result.duration_ms) is not int
            or result.duration_ms <= 0
            or result.output_path.suffix.lower() != ".mp4"
        ):
            raise ValueError(
                "Preview chỉ nhận MP4 SUCCEEDED có digest và metadata đã kiểm."
            )
        canonical = resolve_file_path(result.output_path, strict=True)
        info = file_io_path(canonical).stat()
        if not file_io_path(canonical).is_file() or info.st_size != result.byte_size:
            raise ValueError("MP4 preview đã mất hoặc đổi dung lượng.")
        self.dispose()
        self.result = result
        owner = QObject(self)
        self._owner = owner
        player = IsolatedMediaPlayer(
            owner,
            path=canonical,
            root=self.root,
            duration_ms=result.duration_ms,
            size=result.byte_size,
            sha256=result.sha256,
        )
        self.player = player
        player.setVideoOutput(self.video)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.5)
        player.setAudioOutput(self._audio)
        player.positionChanged.connect(self._position)
        player.errorOccurred.connect(self._error)
        player.finished.connect(self._finished)
        self.seek.setRange(0, result.duration_ms - 1)
        self.seek.setValue(0)
        self.seek.setEnabled(True)
        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.description.setText(
            f"Draft Version {result.draft_version_id}\n"
            f"{canonical.name} • {result.duration_ms / 1000:.2f} giây • "
            f"{result.width}×{result.height}"
        )
        self.status.setText("MP4 local đã kiểm; bấm Phát để review hình và âm thanh.")

    def play(self) -> None:
        if (
            self.player is None
            or self.result is None
            or self.result.duration_ms is None
        ):
            return
        position = self.seek.value()
        if position >= self.result.duration_ms - 100:
            position = 0
            self.seek.setValue(0)
        self.player.set_range(position, self.result.duration_ms)
        self.player.play()
        if self.player.mediaStatus() != QMediaPlayer.MediaStatus.InvalidMedia:
            self.status.setText(
                "Đang phát MP4 local; không chạy phân tích hoặc render."
            )

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.status.setText("Đã dừng preview; MP4 và lịch sử vẫn được giữ.")

    def _seek(self) -> None:
        if (
            self.player is not None
            and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ):
            self.play()

    def _position(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(position)

    def _error(self, _code: object, message: str) -> None:
        self.status.setText(f"Lỗi preview: {message}")

    def _finished(self) -> None:
        if (
            self.player is not None
            and self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia
        ):
            self.status.setText(
                "Đã phát xong MP4; có thể xem lại hoặc sửa Draft/render mới."
            )

    def dispose(self) -> None:
        player, self.player = self.player, None
        if player is not None:
            player.positionChanged.disconnect(self._position)
            player.errorOccurred.disconnect(self._error)
            player.finished.disconnect(self._finished)
            player.setSource(QUrl())
        if self._owner is not None:
            self._owner.deleteLater()  # player retains native resources until owned tree exits
            self._owner = None
        if self._audio is not None:
            self._audio.deleteLater()
            self._audio = None
        self.result = None
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.seek.setEnabled(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.dispose()
        super().closeEvent(event)
