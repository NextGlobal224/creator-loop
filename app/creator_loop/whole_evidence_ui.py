"""Inspect one complete verified source before recording whole-source Evidence."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from creator_loop.database import _connect_write, open_readonly
from creator_loop.evidence_correction import claim_versions_needing_review
from creator_loop.video_evidence import DecodedVideoFrame
from creator_loop.video_evidence_ui import VideoSegmentWidget
from creator_loop.whole_evidence import (
    WholeSource,
    correct_whole_evidence,
    create_whole_evidence,
    read_verified_whole_source,
    reopen_whole_evidence,
)


class WholeEvidenceWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        action: str,
        identifier: str,
        root: Path,
        *,
        content: str | None = None,
        actor: str = "",
        reason: str = "",
        confirmed: bool = False,
    ) -> None:
        super().__init__()
        self.action, self.identifier, self.root = action, identifier, root
        self.content, self.actor, self.reason = content, actor, reason
        self.confirmed = confirmed

    def run(self) -> None:
        try:
            db_path = self.root / "creator_loop.sqlite3"
            if self.action in ("load", "reopen", "load-correction"):
                with closing(open_readonly(db_path)) as db:
                    if self.action == "load":
                        source = read_verified_whole_source(
                            db, self.identifier, self.root
                        )
                        result: object = (source.text_snapshot or "", source)
                    else:
                        result = reopen_whole_evidence(db, self.identifier, self.root)
            else:
                with closing(_connect_write(db_path)) as db:
                    if self.action == "create":
                        version = create_whole_evidence(
                            db,
                            file_id=self.identifier,
                            data_root=self.root,
                            actor=self.actor,
                            whole_source_relevant=self.confirmed,
                            content=self.content,
                        )
                        result = version.evidence_version_id
                    elif self.action == "correct":
                        corrected = correct_whole_evidence(
                            db,
                            evidence_version_id=self.identifier,
                            data_root=self.root,
                            actor=self.actor,
                            reason=self.reason,
                            whole_source_relevant=self.confirmed,
                            content=self.content,
                        )
                        result = (
                            corrected.new_version_id,
                            len(
                                claim_versions_needing_review(db, corrected.evidence_id)
                            ),
                        )
                    else:
                        raise ValueError("Unknown whole-source Evidence action")
            self.result.emit(result)
        except Exception as exc:
            self.failed.emit(f"Không thể xử lý toàn nguồn: {exc}")


class WholeSourceWidget(QWidget):
    def __init__(self, source: WholeSource) -> None:
        super().__init__()
        self.segment: VideoSegmentWidget | None = None
        self.audio: QAudioOutput | None = None
        self.error = ""
        self.failed = QLabel()
        layout = QVBoxLayout(self)
        description = QLabel(
            f"{source.media_type} / {source.role} — file {source.anchor_file_id}"
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        if source.media_type == "TEXT" and source.text_snapshot is not None:
            self.text = QTextEdit()
            self.text.setReadOnly(True)
            self.text.setPlainText(source.text_snapshot)
            layout.addWidget(self.text)
        elif source.media_type == "IMAGE" and source.image is not None:
            self.image = QLabel()
            self.image.setPixmap(QPixmap.fromImage(source.image))
            scroll = QScrollArea()
            scroll.setWidget(self.image)
            scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(scroll)
            layout.addWidget(
                QLabel("Toàn ảnh ở tỷ lệ 1:1; cuộn để xem các vùng ngoài khung.")
            )
        elif (
            source.media_type == "VIDEO"
            and source.image is not None
            and source.duration_ms is not None
        ):
            self.segment = VideoSegmentWidget(
                DecodedVideoFrame(
                    source.duration_ms,
                    0,
                    source.image,
                    source.anchor_path,
                    source.data_root,
                    source.expected_size,
                    source.expected_sha256,
                )
            )
            self.audio = QAudioOutput(self.segment)
            self.segment.player.setAudioOutput(self.audio)
            self.segment.play_button.setText("Phát toàn video và audio")
            self.segment.player.errorOccurred.connect(self._playback_error)
            layout.addWidget(self.segment)
            layout.addWidget(QLabel(f"Toàn video: 0–{source.duration_ms} ms."))
        else:
            raise ValueError("Whole-source preview requires decoded source data")
        self.failed.setWordWrap(True)
        layout.addWidget(self.failed)

    def _playback_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self.error = message or "Không thể phát nguồn đã chọn."
        self.failed.setText(f"Lỗi phát nguồn: {self.error}")

    def stop(self) -> None:
        if self.segment is not None:
            self.segment.stop()


class WholeEvidenceDialog(QDialog):
    def __init__(
        self, source: WholeSource, *, content: str = "", correction: bool = False
    ) -> None:
        super().__init__()
        self.correction = correction
        self.setWindowTitle(
            "Sửa Evidence toàn nguồn" if correction else "Evidence toàn nguồn"
        )
        self.resize(760, 720)
        layout = QVBoxLayout(self)
        self.preview = WholeSourceWidget(source)
        layout.addWidget(self.preview, 1)
        form = QFormLayout()
        self.content = QTextEdit()
        self.content.setMaximumHeight(85)
        self.content.setPlainText(
            source.text_snapshot if source.media_type == "TEXT" else content
        )
        self.content.setReadOnly(source.media_type == "TEXT")
        form.addRow(
            "Toàn văn" if source.media_type == "TEXT" else "Quan sát", self.content
        )
        self.actor = QLineEdit("creator")
        self.reason = QLineEdit()
        form.addRow("Người sửa" if correction else "Người tạo", self.actor)
        if correction:
            form.addRow("Lý do sửa", self.reason)
        layout.addLayout(form)
        self.confirmed = QCheckBox(
            "Tôi đã xem nguồn và xác nhận toàn nguồn thực sự liên quan."
        )
        layout.addWidget(self.confirmed)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.confirmed.toggled.connect(self._refresh)
        self.content.textChanged.connect(self._refresh)
        self.actor.textChanged.connect(self._refresh)
        self.reason.textChanged.connect(self._refresh)
        if self.preview.segment is not None:
            self.preview.segment.player.errorOccurred.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args: object) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.confirmed.isChecked()
            and bool(self.content.toPlainText().strip())
            and bool(self.actor.text().strip())
            and (not self.correction or bool(self.reason.text().strip()))
            and not self.preview.error
        )

    def accept(self) -> None:
        self._refresh()
        if self.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
            super().accept()

    def done(self, result: int) -> None:
        self.preview.stop()
        super().done(result)


class WholeEvidenceView(QDialog):
    def __init__(self, content: str, source: WholeSource) -> None:
        super().__init__()
        self.setWindowTitle("Toàn nguồn Evidence đã xác minh")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        self.preview = WholeSourceWidget(source)
        layout.addWidget(self.preview, 1)
        saved = QTextEdit()
        saved.setReadOnly(True)
        saved.setMaximumHeight(90)
        saved.setPlainText(content)
        layout.addWidget(saved)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:
        self.preview.stop()
        super().done(result)
