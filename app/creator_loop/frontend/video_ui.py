"""Edit a scene plan, dispatch owned local render and preview exact outputs."""

from pathlib import Path
from typing import Literal, cast

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_loop.backend.video.component_config import configure_video_components
from creator_loop.backend.video.render_service import RenderService
from creator_loop.shared.workflow import (
    Progress,
    RenderPreset,
    RenderResult,
    Scene,
    Subtitle,
)


class RenderWorker(QThread):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        service: RenderService,
        version_id: str,
        scenes: tuple[Scene, ...],
        preset: RenderPreset,
        subtitles: tuple[Subtitle, ...],
        voiceover: str | None,
    ):
        super().__init__()
        (
            self.service,
            self.version_id,
            self.scenes,
            self.preset,
            self.subtitles,
            self.voiceover,
        ) = service, version_id, scenes, preset, subtitles, voiceover

    def run(self) -> None:
        try:
            result = self.service.render(
                self.version_id,
                self.scenes,
                self.preset,
                self.progress.emit,
                self.isInterruptionRequested,
                self.subtitles,
                self.voiceover,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class VideoConfigWorker(QThread):
    completed = Signal(str)

    def __init__(self, root: Path, manifest: Path):
        super().__init__()
        self.root, self.manifest = root, manifest

    def run(self) -> None:
        try:
            specs = configure_video_components(
                self.root, self.manifest, self.isInterruptionRequested
            )
            self.completed.emit(
                "Đã xác minh và chọn video local: "
                + ", ".join(s.component_id + " " + s.version for s in specs)
            )
        except Exception as exc:
            self.completed.emit("Chưa cấu hình được: " + str(exc))


class VideoDialog(QDialog):
    def __init__(
        self,
        root: Path,
        parent=None,
        *,
        draft_version_id: str | None = None,
        auto_start: bool = False,
    ):
        super().__init__(parent)
        from creator_loop.frontend.video_preview import VideoPreviewWidget

        self.service = RenderService(root)
        self._worker: RenderWorker | VideoConfigWorker | None = None
        self._result: RenderResult | None = None
        self._pending_error: str | None = None
        self.setWindowTitle("Video nháp — MP4 local")
        self.resize(1040, 850)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.draft = QComboBox()
        self.preset = QComboBox()
        for name, preset in [
            ("Dọc 360×640", RenderPreset()),
            ("Ngang 640×360", RenderPreset("preview-landscape", 640, 360)),
            ("Vuông 480×480", RenderPreset("preview-square", 480, 480)),
        ]:
            self.preset.addItem(name, preset)
        row.addWidget(QLabel("Draft Version"))
        row.addWidget(self.draft, 1)
        row.addWidget(self.preset)
        layout.addLayout(row)
        self.scenes = QTableWidget(0, 5)
        self.scenes.setHorizontalHeaderLabels(
            [
                "Chữ / lời đọc",
                "Thời lượng (ms)",
                "TITLE / IMAGE / VIDEO",
                "File ID ảnh/clip",
                "Bắt đầu clip (ms)",
            ]
        )
        self.scenes.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.scenes)
        actions = QHBoxLayout()
        self.plan_button = QPushButton("Lập cảnh từ Draft")
        self.add_button = QPushButton("Thêm cảnh")
        self.remove_button = QPushButton("Bỏ cảnh")
        self.render_button = QPushButton("Render MP4 preview")
        self.cancel_button = QPushButton("Dừng / Hủy render")
        self.configure_button = QPushButton("Chọn FFmpeg / ffprobe local…")
        self.cancel_button.setEnabled(False)
        for widget in [
            self.plan_button,
            self.add_button,
            self.remove_button,
            self.render_button,
            self.cancel_button,
        ]:
            actions.addWidget(widget)
        layout.addLayout(actions)
        layout.addWidget(self.configure_button)
        options = QHBoxLayout()
        self.subtitles = QCheckBox("Phụ đề tiếng Việt theo cảnh")
        self.subtitles.setChecked(True)
        self.voiceover = QComboBox()
        self.voiceover.addItem("Không có voiceover; không tự gọi TTS", None)
        options.addWidget(self.subtitles)
        options.addWidget(self.voiceover, 1)
        layout.addLayout(options)
        self.media_hint = QLabel(
            "Ảnh/clip/âm thanh cần REUSE_MEDIA và quyền OWNED/LICENSED. Không có media hợp lệ vẫn tạo được cảnh chữ."
        )
        self.media_hint.setWordWrap(True)
        layout.addWidget(self.media_hint)
        self.status = QLabel(
            "MP4 nháp chờ review; chưa tạo Package hoặc đăng nội dung."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(
            ["Render run", "Trạng thái", "Thời lượng", "MP4 đã kiểm"]
        )
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history)
        self.preview = VideoPreviewWidget(root, self)
        layout.addWidget(self.preview)
        self.draft.currentIndexChanged.connect(self._draft_changed)
        self.plan_button.clicked.connect(self.plan)
        self.add_button.clicked.connect(lambda: self._add(Scene("Cảnh mới")))
        self.remove_button.clicked.connect(
            lambda: self.scenes.removeRow(self.scenes.currentRow())
        )
        self.render_button.clicked.connect(self.render)
        self.cancel_button.clicked.connect(self.cancel)
        self.configure_button.clicked.connect(self.configure)
        self.history.itemSelectionChanged.connect(self.show_output)
        self.draft.blockSignals(True)
        for draft in self.service.drafts():
            self.draft.addItem(
                f"v{draft.version_no} — {draft.body_text[:70]} [{draft.draft_version_id}]",
                draft.draft_version_id,
            )
        if draft_version_id:
            index = self.draft.findData(draft_version_id)
            if index >= 0:
                self.draft.setCurrentIndex(index)
        self.draft.blockSignals(False)
        self._draft_changed()
        if auto_start:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self.render)

    def _draft_changed(self) -> None:
        if self._worker is not None:
            return
        self.preview.dispose()
        self.voiceover.clear()
        self.voiceover.addItem("Không có voiceover; không tự gọi TTS", None)
        version = self.draft.currentData()
        if version:
            draft = self.service.draft(str(version))
            media = self.service.media(draft.project_id)
            for item in media:
                if item.media_type == "AUDIO":
                    self.voiceover.addItem(item.file_id, item.file_id)
            self.media_hint.setText(
                "Media được phép: "
                + (
                    "; ".join(f"{m.media_type}: {m.file_id}" for m in media)
                    or "chưa có — preview dùng cảnh chữ."
                )
            )
            self.plan()
        self.reload_history()

    def _add(self, scene: Scene) -> None:
        row = self.scenes.rowCount()
        self.scenes.insertRow(row)
        for col, value in enumerate(
            [
                scene.text,
                scene.duration_ms,
                scene.kind,
                scene.file_id or "",
                scene.clip_start_ms,
            ]
        ):
            self.scenes.setItem(row, col, QTableWidgetItem(str(value)))

    def plan(self) -> None:
        from creator_loop.backend.video.planner import plan_draft

        if self._worker is not None or not self.draft.currentData():
            return
        draft = self.service.draft(str(self.draft.currentData()))
        self.scenes.setRowCount(0)
        for scene in plan_draft(draft, preset=self.preset.currentData()):
            self._add(scene)

    def render(self) -> None:
        if self._worker is not None or not self.draft.currentData():
            return
        try:
            scenes = []
            for row in range(self.scenes.rowCount()):
                values = [
                    self.scenes.item(row, col).text()
                    if self.scenes.item(row, col)
                    else ""
                    for col in range(5)
                ]
                kind = values[2].strip().upper()
                if kind not in {"TITLE", "IMAGE", "VIDEO"}:
                    raise ValueError("Loại cảnh phải là TITLE, IMAGE hoặc VIDEO.")
                scenes.append(
                    Scene(
                        values[0],
                        int(values[1]),
                        cast(Literal["TITLE", "IMAGE", "VIDEO"], kind),
                        values[3].strip() or None,
                        int(values[4] or "0"),
                    )
                )
            subtitles = []
            offset = 0
            if self.subtitles.isChecked():
                for scene in scenes:
                    subtitles.append(
                        Subtitle(offset, offset + scene.duration_ms, scene.text)
                    )
                    offset += scene.duration_ms
            self.preview.dispose()
            self._result = None
            self._pending_error = None
            self._worker = RenderWorker(
                self.service,
                str(self.draft.currentData()),
                tuple(scenes),
                self.preset.currentData(),
                tuple(subtitles),
                self.voiceover.currentData(),
            )
            self._worker.progress.connect(self._progress)
            self._worker.completed.connect(self._completed)
            self._worker.failed.connect(self._failed)
            self._worker.finished.connect(self._finished)
            self._set_busy(True)
            self.status.setText("Đang lập run và render local…")
            self._worker.start()
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc))

    def _set_busy(self, busy: bool) -> None:
        for widget in [
            self.draft,
            self.preset,
            self.scenes,
            self.plan_button,
            self.add_button,
            self.remove_button,
            self.render_button,
            self.subtitles,
            self.voiceover,
            self.configure_button,
        ]:
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def configure(self) -> None:
        if self._worker is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn manifest FFmpeg / ffprobe local",
            "",
            "Component manifest (*.json)",
        )
        if not path:
            return
        worker = VideoConfigWorker(self.service.root, Path(path))
        self._worker = worker
        worker.completed.connect(self.status.setText)
        worker.finished.connect(self._configuration_finished)
        self._set_busy(True)
        self.status.setText("Đang xác minh version/digest và quyền chạy local…")
        worker.start()

    def _configuration_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker:
            worker.deleteLater()
        self._set_busy(False)

    def _progress(self, progress: Progress) -> None:
        self.status.setText(
            f"{progress.message} — {progress.completed}/{progress.total}"
        )

    def _completed(self, result: RenderResult) -> None:
        self._result = result

    def _failed(self, error: str) -> None:
        self._pending_error = error

    def _finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker:
            worker.deleteLater()
        self._set_busy(False)
        self.reload_history()
        if self._pending_error:
            self.status.setText("Chưa render được: " + self._pending_error)
        elif self._result:
            if self._result.status == "SUCCEEDED":
                self.status.setText("Đã tạo MP4 nháp: " + str(self._result.output_path))
                self.preview.set_result(self._result)
            elif self._result.status == "CANCELLED":
                self.status.setText(
                    "Đã Hủy. Không ghi output thành công; kết quả cũ được giữ."
                )
            else:
                self.status.setText("Render lỗi: " + (self._result.error or "Chưa rõ"))

    def cancel(self) -> None:
        if self._worker:
            self._worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText("Đang Hủy worker render local…")

    def reload_history(self) -> None:
        self._history = (
            self.service.history(str(self.draft.currentData()))
            if self.draft.currentData()
            else []
        )
        self.history.setRowCount(len(self._history))
        for row, item in enumerate(self._history):
            for col, value in enumerate(
                [
                    item["render_run_id"],
                    item["status"],
                    item["duration_ms"]
                    if item["duration_ms"] is not None
                    else "Chưa rõ",
                    item["storage_key"] or "Chưa có output thành công",
                ]
            ):
                self.history.setItem(row, col, QTableWidgetItem(str(value)))

    def show_output(self) -> None:
        if self._worker is not None:
            return
        row = self.history.currentRow()
        if (
            0 <= row < len(self._history)
            and self._history[row]["status"] == "SUCCEEDED"
        ):
            try:
                self.preview.set_result(
                    self.service.result(str(self._history[row]["render_run_id"]))
                )
            except (ValueError, OSError) as exc:
                self.status.setText(str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self.cancel()
            event.ignore()
            return
        self.preview.dispose()
        event.accept()
