"""Small Windows Library view for importing and listing physical originals."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, cast

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
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

from creator_loop.backend.knowledge.audio_evidence import (
    DecodedAudioSegment,
)
from creator_loop.backend.knowledge.evidence_review import (
    ReviewAction,
)
from creator_loop.backend.knowledge.video_evidence import (
    DecodedVideoFrame,
)
from creator_loop.backend.knowledge.whole_evidence import WholeSource
from creator_loop.backend.library import library_snapshots as _snapshots
from creator_loop.backend.library.library_operations import (
    run_AudioEvidenceWorker,
    run_EvidenceReviewWorker,
    run_ImageEvidenceWorker,
    run_OriginalImportWorker,
    run_SourceWorker,
    run_TextEvidenceWorker,
    run_ThumbnailWorker,
    run_VideoEvidenceWorker,
)
from creator_loop.backend.library.source_association import (
    SourceDetails,
)
from creator_loop.frontend.audio_evidence_ui import AudioEvidenceDialog, AudioRangeView
from creator_loop.frontend.claim_ui import ClaimDialog
from creator_loop.frontend.creator_ui import CreatorDialog
from creator_loop.frontend.evidence_review_ui import EvidenceReviewDialog
from creator_loop.frontend.image_evidence_ui import ImageEvidenceDialog, ImageRegionView
from creator_loop.frontend.owned_thread import OwnedThreadExit
from creator_loop.frontend.project_ui import ProjectDialog
from creator_loop.frontend.publication_ui import PublicationDialog
from creator_loop.frontend.qt_table_items import table_item
from creator_loop.frontend.selection_ui import SelectionDialog
from creator_loop.frontend.source_ui import SourceDialog
from creator_loop.frontend.storage_ui import StorageDialog
from creator_loop.frontend.text_evidence_ui import TextEvidenceDialog
from creator_loop.frontend.video_evidence_ui import VideoEvidenceDialog, VideoRangeView
from creator_loop.frontend.whole_evidence_ui import (
    WholeEvidenceDialog,
    WholeEvidenceView,
    WholeEvidenceWorker,
)


class OriginalImportWorker(QThread):
    imported = Signal(str)
    failed = Signal(str)

    def __init__(self, kind: str, source: Path, root: Path) -> None:
        super().__init__()
        self.kind = kind
        self.source = source
        self.root = root

    def run(self) -> None:
        run_OriginalImportWorker(self)


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
        reason: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.identifier = identifier
        self.root = root
        self.range_start = start
        self.range_end = end
        self.actor = actor
        self.reason = reason

    def run(self) -> None:
        run_TextEvidenceWorker(self)


class ImageEvidenceWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        action: str,
        identifier: str,
        root: Path,
        *,
        region: dict[str, float] | None = None,
        content: str = "",
        actor: str = "",
        reason: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.identifier = identifier
        self.root = root
        self.region = region
        self.content = content
        self.actor = actor
        self.reason = reason

    def run(self) -> None:
        run_ImageEvidenceWorker(self)


class ThumbnailWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, file_id: str, root: Path) -> None:
        super().__init__()
        self.file_id = file_id
        self.root = root

    def run(self) -> None:
        run_ThumbnailWorker(self)


class VideoEvidenceWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        action: str,
        identifier: str,
        root: Path,
        *,
        start_ms: int = 0,
        end_ms: int = 0,
        content: str = "",
        actor: str = "",
        reason: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.identifier = identifier
        self.root = root
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.content = content
        self.actor = actor
        self.reason = reason

    def run(self) -> None:
        run_VideoEvidenceWorker(self)


class AudioEvidenceWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        action: str,
        identifier: str,
        root: Path,
        *,
        start_ms: int = 0,
        end_ms: int = 0,
        content: str = "",
        actor: str = "",
        evidence_type: str = "SPEECH",
        reason: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.identifier = identifier
        self.root = root
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.content = content
        self.actor = actor
        self.evidence_type = evidence_type
        self.reason = reason

    def run(self) -> None:
        run_AudioEvidenceWorker(self)


class EvidenceReviewWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        version_id: str,
        root: Path,
        action: ReviewAction,
        actor: str,
        reason: str,
    ) -> None:
        super().__init__()
        self.version_id = version_id
        self.root = root
        self.action = action
        self.actor = actor
        self.reason = reason

    def run(self) -> None:
        run_EvidenceReviewWorker(self)


class SourceWorker(QThread):
    linked = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        asset_id: str,
        relationship_type: str,
        details: SourceDetails,
        existing_source_id: str | None,
    ) -> None:
        super().__init__()
        self.root = root
        self.asset_id = asset_id
        self.relationship_type = relationship_type
        self.details = details
        self.existing_source_id = existing_source_id

    def run(self) -> None:
        run_SourceWorker(self)


class LibraryWindow(QMainWindow):
    def choose_facebook(self) -> None:
        from creator_loop.frontend.facebook_ui import FacebookDialog

        FacebookDialog(self.root, self).exec()
        self.reload()

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.maintenance_requested = False
        self.components_requested = False
        self.transcription_selection: tuple[str, str] | None = None
        self.transcript_review_selection: tuple[str, str] | None = None
        self._close_after_worker = False
        self._media_cancel_requested = False
        self._worker: QThread | None = None
        self._after_worker_finished: Callable[[], None] | None = None
        self._worker_exit: OwnedThreadExit | None = None
        self._evidence_result: object | None = None
        self._image_result: object | None = None
        self._video_result: object | None = None
        self._audio_result: object | None = None
        self._whole_result: object | None = None
        self.setWindowTitle("Creator Loop — Library")
        self.resize(1000, 700)

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
        thumbnail = QPushButton("Tạo thumbnail Image")
        thumbnail.clicked.connect(self.choose_image_thumbnail)
        self._buttons.append(thumbnail)
        actions.addWidget(thumbnail)
        projects = QPushButton("Projects")
        projects.clicked.connect(self.choose_project)
        self._buttons.append(projects)
        actions.addWidget(projects)
        claims = QPushButton("Claims")
        claims.clicked.connect(self.choose_claim)
        self._buttons.append(claims)
        actions.addWidget(claims)
        creator = QPushButton("Creator")
        creator.clicked.connect(self.choose_creator)
        self._buttons.append(creator)
        actions.addWidget(creator)
        selection = QPushButton("Selection")
        selection.clicked.connect(self.choose_selection)
        self._buttons.append(selection)
        actions.addWidget(selection)
        publication = QPushButton("Package / Publication")
        publication.clicked.connect(self.choose_publication)
        self._buttons.append(publication)
        actions.addWidget(publication)
        storage = QPushButton("Kho media")
        storage.clicked.connect(self.choose_storage)
        self._buttons.append(storage)
        actions.addWidget(storage)
        layout.addLayout(actions)
        facebook = QPushButton("Tư liệu Facebook")
        facebook.setObjectName("facebookLibrary")
        facebook.clicked.connect(self.choose_facebook)
        self._buttons.append(facebook)
        layout.addWidget(facebook)
        self.maintenance_button = QPushButton(
            "Đóng Library để sao lưu / cập nhật / khôi phục"
        )
        self.maintenance_button.clicked.connect(self.choose_maintenance)
        self._buttons.append(self.maintenance_button)
        layout.addWidget(self.maintenance_button)
        self.components_button = QPushButton("Đóng Library để chọn engine/model cục bộ")
        self.components_button.clicked.connect(self.choose_components)
        self._buttons.append(self.components_button)
        layout.addWidget(self.components_button)
        self.transcription_button = QPushButton(
            "Chép lời Video bằng engine/model đã chọn"
        )
        self.transcription_button.clicked.connect(self.choose_transcription)
        self._buttons.append(self.transcription_button)
        layout.addWidget(self.transcription_button)
        self.cancel_media_button = QPushButton("Hủy giải mã media")
        self.cancel_media_button.setEnabled(False)
        self.cancel_media_button.clicked.connect(self._cancel_media_worker)
        layout.addWidget(self.cancel_media_button)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Loại", "Tên", "MIME", "Byte"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        evidence_actions = QGridLayout()
        attach_source = QPushButton("Nguồn của Asset")
        attach_source.clicked.connect(self.choose_source)
        self._buttons.append(attach_source)
        evidence_actions.addWidget(attach_source, 0, 0)
        create_evidence = QPushButton("Tạo Evidence Text")
        create_evidence.clicked.connect(self.choose_text_evidence)
        self._buttons.append(create_evidence)
        evidence_actions.addWidget(create_evidence, 0, 1)
        create_image = QPushButton("Tạo Evidence Image")
        create_image.clicked.connect(self.choose_image_evidence)
        self._buttons.append(create_image)
        evidence_actions.addWidget(create_image, 0, 2)
        create_thumbnail_evidence = QPushButton("Evidence từ thumbnail")
        create_thumbnail_evidence.clicked.connect(self.choose_thumbnail_evidence)
        self._buttons.append(create_thumbnail_evidence)
        evidence_actions.addWidget(create_thumbnail_evidence, 0, 3)
        reopen_evidence = QPushButton("Mở Evidence Text")
        reopen_evidence.clicked.connect(self.reopen_selected_text_evidence)
        self._buttons.append(reopen_evidence)
        evidence_actions.addWidget(reopen_evidence, 1, 0)
        correct_evidence = QPushButton("Sửa Evidence Text")
        correct_evidence.clicked.connect(self.correct_selected_text_evidence)
        self._buttons.append(correct_evidence)
        evidence_actions.addWidget(correct_evidence, 1, 1)
        reopen_image = QPushButton("Mở Evidence Image")
        reopen_image.clicked.connect(self.reopen_selected_image_evidence)
        self._buttons.append(reopen_image)
        evidence_actions.addWidget(reopen_image, 1, 2)
        correct_image = QPushButton("Sửa Evidence Image")
        correct_image.clicked.connect(self.correct_selected_image_evidence)
        self._buttons.append(correct_image)
        evidence_actions.addWidget(correct_image, 1, 3)
        layout.addLayout(evidence_actions)
        video_actions = QGridLayout()
        create_video = QPushButton("Tạo Evidence Video")
        create_video.clicked.connect(self.choose_video_evidence)
        self._buttons.append(create_video)
        video_actions.addWidget(create_video, 0, 0)
        reopen_video = QPushButton("Mở Evidence Video")
        reopen_video.clicked.connect(self.reopen_selected_video_evidence)
        self._buttons.append(reopen_video)
        video_actions.addWidget(reopen_video, 0, 1)
        correct_video = QPushButton("Sửa Evidence Video")
        correct_video.clicked.connect(self.correct_selected_video_evidence)
        self._buttons.append(correct_video)
        video_actions.addWidget(correct_video, 0, 2)
        create_audio = QPushButton("Tạo Evidence Audio")
        create_audio.clicked.connect(self.choose_audio_evidence)
        self._buttons.append(create_audio)
        video_actions.addWidget(create_audio, 1, 0)
        reopen_audio = QPushButton("Mở Evidence Audio")
        reopen_audio.clicked.connect(self.reopen_selected_audio_evidence)
        self._buttons.append(reopen_audio)
        video_actions.addWidget(reopen_audio, 1, 1)
        correct_audio = QPushButton("Sửa Evidence Audio")
        correct_audio.clicked.connect(self.correct_selected_audio_evidence)
        self._buttons.append(correct_audio)
        video_actions.addWidget(correct_audio, 1, 2)
        review_evidence = QPushButton("Review Evidence")
        review_evidence.clicked.connect(self.choose_evidence_review)
        self._buttons.append(review_evidence)
        video_actions.addWidget(review_evidence, 0, 3, 2, 1)
        layout.addLayout(video_actions)

        whole_actions = QHBoxLayout()
        for action, label in (
            ("load", "Tạo Evidence toàn nguồn"),
            ("reopen", "Mở Evidence toàn nguồn"),
            ("load-correction", "Sửa Evidence toàn nguồn"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, a=action: self.choose_whole_evidence(a)
            )
            self._buttons.append(button)
            whole_actions.addWidget(button)
        layout.addLayout(whole_actions)

        self.evidence_table = QTableWidget(0, 4)
        self.evidence_table.setHorizontalHeaderLabels(
            ("Evidence", "Đoạn đã lưu", "Version", "Review")
        )
        self.evidence_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.evidence_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.evidence_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.evidence_table)
        layout.addWidget(QLabel("Các task đã chạy"))
        self.transcript_review_button = QPushButton("Chọn đoạn từ task chép lời máy")
        self.transcript_review_button.clicked.connect(self.choose_transcript_review)
        self._buttons.append(self.transcript_review_button)
        layout.addWidget(self.transcript_review_button)
        self.run_table = QTableWidget(0, 4)
        self.run_table.setHorizontalHeaderLabels(
            ("Asset", "Task", "Trạng thái", "File dẫn xuất")
        )
        self.run_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.run_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.run_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.run_table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.reload()

    def reload(self) -> None:
        (evidence_rows, rows, run_rows) = _snapshots.reload_snapshot(root=self.root)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row[:4]):
                self.table.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )
            table_item(self.table, row_index, 0).setData(
                Qt.ItemDataRole.UserRole, row[4]
            )
            table_item(self.table, row_index, 1).setData(
                Qt.ItemDataRole.UserRole, row[5]
            )
        self.evidence_table.setRowCount(len(evidence_rows))
        for row_index, row in enumerate(evidence_rows):
            evidence_kind = {
                "IMAGE_REGION": "Image thumbnail" if row[6] == "THUMBNAIL" else "Image",
                "TEXT_RANGE": "Text",
                "TIME_RANGE": "Audio" if row[5] == "audio" else "Video",
                "WHOLE_ASSET": f"Toàn nguồn {row[6]}",
            }.get(str(row[4]), str(row[4]))
            item = QTableWidgetItem(f"{row[1]} ({evidence_kind})")
            item.setData(Qt.ItemDataRole.UserRole, row[0])
            self.evidence_table.setItem(row_index, 0, item)
            content_item = QTableWidgetItem(str(row[2]))
            locator_tag = f"TIME_RANGE:{row[5]}" if row[4] == "TIME_RANGE" else row[4]
            content_item.setData(Qt.ItemDataRole.UserRole, locator_tag)
            self.evidence_table.setItem(row_index, 1, content_item)
            self.evidence_table.setItem(row_index, 2, QTableWidgetItem(str(row[3])))
            self.evidence_table.setItem(row_index, 3, QTableWidgetItem(str(row[7])))
        self.run_table.setRowCount(len(run_rows))
        for row_index, row in enumerate(run_rows):
            for column_index, value in enumerate(row[:4]):
                self.run_table.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )
            table_item(self.run_table, row_index, 3).setData(
                Qt.ItemDataRole.UserRole, row[4]
            )
            table_item(self.run_table, row_index, 1).setData(
                Qt.ItemDataRole.UserRole, (row[5], row[0])
            )
        self.status.setText(f"{len(rows)} original(s), {len(run_rows)} run(s)")

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
        self._close_after_worker = False
        self._media_cancel_requested = False
        self._after_worker_finished = after_finished
        self._worker_exit = OwnedThreadExit()
        worker.started.connect(
            self._worker_exit.capture, Qt.ConnectionType.DirectConnection
        )
        worker.finished.connect(self._on_finished)
        for button in self._buttons:
            button.setEnabled(False)
        self.status.setText(message)
        self.cancel_media_button.setEnabled(self._media_worker_cancelable(worker))
        worker.start()

    @staticmethod
    def _media_worker_cancelable(worker: QThread) -> bool:
        return isinstance(
            worker, (VideoEvidenceWorker, AudioEvidenceWorker, WholeEvidenceWorker)
        ) or (isinstance(worker, OriginalImportWorker) and worker.kind == "VIDEO")

    def _cancel_media_worker(self) -> None:
        worker = self._worker
        if worker is None or not self._media_worker_cancelable(worker):
            return
        worker.requestInterruption()
        self._media_cancel_requested = True
        self.cancel_media_button.setEnabled(False)
        self.status.setText(
            "Đã yêu cầu hủy giải mã; đang đợi tác vụ dọn worker an toàn…"
        )

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
        if row < 0 or table_item(self.table, row, 0).text() != "TEXT":
            QMessageBox.information(self, "Chọn Text", "Chọn một original TEXT.")
            return
        file_id = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_evidence_worker(
            TextEvidenceWorker("load", str(file_id), self.root),
            "Đang xác minh snapshot TEXT…",
        )

    def choose_whole_evidence(self, action: str) -> None:
        if self._worker is not None:
            return
        if action == "load":
            row = self.table.currentRow()
            if row < 0:
                QMessageBox.information(
                    self, "Chọn nguồn", "Chọn một original trong Library."
                )
                return
            identifier = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        elif action in ("reopen", "load-correction"):
            row = self.evidence_table.currentRow()
            if (
                row < 0
                or table_item(self.evidence_table, row, 1).data(
                    Qt.ItemDataRole.UserRole
                )
                != "WHOLE_ASSET"
            ):
                QMessageBox.information(
                    self, "Chọn Evidence", "Chọn một Evidence toàn nguồn."
                )
                return
            identifier = table_item(self.evidence_table, row, 0).data(
                Qt.ItemDataRole.UserRole
            )
        else:
            raise ValueError("Unknown whole-source UI action")
        self._start_whole_worker(
            WholeEvidenceWorker(action, str(identifier), self.root)
        )

    def _start_whole_worker(self, worker: WholeEvidenceWorker) -> None:
        self._whole_result = None
        worker.result.connect(self._store_whole_result)
        worker.failed.connect(self._on_failed)
        self._start_worker(
            worker,
            "Đang xác minh toàn nguồn…",
            lambda: self._finish_whole(worker.action, worker.identifier),
        )

    def _store_whole_result(self, result: object) -> None:
        self._whole_result = result

    def _finish_whole(self, action: str, identifier: str) -> None:
        result, self._whole_result = self._whole_result, None
        if result is None:
            return
        if action in ("create", "correct"):
            try:
                self.reload()
            except Exception as exc:
                self._on_failed(
                    f"Đã ghi Evidence nhưng không thể tải lại Library: {exc}"
                )
                return
            if action == "correct":
                _version, stale = cast(tuple[str, int], result)
                self.status.setText(
                    f"Đã sửa toàn nguồn; {stale} Claim Version cần review."
                )
            else:
                self.status.setText("Evidence toàn nguồn đã tạo, đang chờ review.")
            return
        content, source = cast(tuple[str, WholeSource], result)
        if action == "reopen":
            WholeEvidenceView(content, source).exec()
            return
        if action == "load-correction" and source.media_type == "TEXT":
            snapshot = source.text_snapshot or ""
            dialog = TextEvidenceDialog(
                snapshot, correction=True, start=0, end=len(snapshot)
            )
            if dialog.exec() == TextEvidenceDialog.DialogCode.Accepted:
                self._start_evidence_worker(
                    TextEvidenceWorker(
                        "correct",
                        identifier,
                        self.root,
                        start=dialog.start.value(),
                        end=dialog.end.value(),
                        actor=dialog.actor.text(),
                        reason=dialog.reason.text(),
                    ),
                    "Đang lưu đoạn TEXT đã sửa…",
                )
            return
        whole_dialog = WholeEvidenceDialog(
            source, content=content, correction=action == "load-correction"
        )
        if whole_dialog.exec() == WholeEvidenceDialog.DialogCode.Accepted:
            self._start_whole_worker(
                WholeEvidenceWorker(
                    "correct" if action == "load-correction" else "create",
                    identifier,
                    self.root,
                    content=(
                        source.text_snapshot
                        if source.media_type == "TEXT"
                        else whole_dialog.content.toPlainText()
                    ),
                    actor=whole_dialog.actor.text(),
                    reason=whole_dialog.reason.text(),
                    confirmed=whole_dialog.confirmed.isChecked(),
                )
            )

    def choose_storage(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        asset_id = ""
        if row >= 0:
            asset_id = str(
                table_item(self.table, row, 1).data(Qt.ItemDataRole.UserRole)
            )
        StorageDialog(self.root, asset_id).exec()
        self.reload()

    def choose_project(self) -> None:
        if self._worker is not None:
            return
        ProjectDialog(self.root).exec()

    def choose_claim(self) -> None:
        if self._worker is not None:
            return
        dialog = ClaimDialog(self.root)
        dialog.evidence_requested.connect(self.open_claim_evidence)
        dialog.exec()

    def choose_creator(self) -> None:
        if self._worker is not None:
            return
        dialog = CreatorDialog(self.root)
        dialog.claim_requested.connect(self.open_draft_claim)
        dialog.exec()

    def open_draft_claim(self, version_id: str) -> None:
        if self._worker is not None:
            return
        dialog = ClaimDialog(self.root)
        dialog.evidence_requested.connect(self.open_claim_evidence)
        if dialog.reload(version_id):
            dialog.exec()

    def choose_selection(self) -> None:
        if self._worker is not None:
            return
        dialog = SelectionDialog(self.root)
        dialog.draft_requested.connect(self.open_selected_draft)
        dialog.exec()

    def choose_publication(self) -> None:
        if self._worker is not None:
            return
        dialog = PublicationDialog(self.root)
        dialog.draft_requested.connect(self.open_selected_draft)
        dialog.exec()

    def open_selected_draft(self, version_id: str) -> None:
        if self._worker is not None:
            return
        try:
            (row,) = _snapshots.open_selected_draft_snapshot(
                root=self.root, version_id=version_id
            )
            if row is None:
                raise ValueError("Exact Draft Version is unavailable")
            dialog = CreatorDialog(self.root)
            project_index = dialog.project.findData(row[0])
            if project_index < 0:
                raise ValueError("Project is unavailable in Creator")
            dialog.project.setCurrentIndex(project_index)
            dialog.claim_requested.connect(self.open_draft_claim)
            if dialog.reload(version_id) and dialog.selected_version_id() == version_id:
                dialog.exec()
        except Exception as exc:
            QMessageBox.warning(
                self, "Selection", f"Không thể mở exact Draft Version: {exc}"
            )

    def open_claim_evidence(self, version_id: str) -> None:
        if self._worker is not None:
            return
        self.reload()
        for row in range(self.evidence_table.rowCount()):
            if (
                table_item(self.evidence_table, row, 0).data(Qt.ItemDataRole.UserRole)
                != version_id
            ):
                continue
            self.evidence_table.setCurrentCell(row, 0)
            locator = table_item(self.evidence_table, row, 1).data(
                Qt.ItemDataRole.UserRole
            )
            handlers = {
                "TEXT_RANGE": self.reopen_selected_text_evidence,
                "IMAGE_REGION": self.reopen_selected_image_evidence,
                "TIME_RANGE:video": self.reopen_selected_video_evidence,
                "TIME_RANGE:audio": self.reopen_selected_audio_evidence,
                "WHOLE_ASSET": lambda: self.choose_whole_evidence("reopen"),
            }
            handler = handlers.get(str(locator))
            if handler is not None:
                handler()
                return
        QMessageBox.warning(
            self, "Claim citation", "Evidence Version không còn khả dụng trong Library."
        )

    def choose_source(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Chọn Asset", "Chọn một original trong Library."
            )
            return
        item = table_item(self.table, row, 1)
        asset_id = str(item.data(Qt.ItemDataRole.UserRole))
        dialog = SourceDialog(self.root, asset_id, item.text())
        if dialog.exec() != SourceDialog.DialogCode.Accepted:
            return
        worker = SourceWorker(
            self.root,
            asset_id,
            dialog.relationship.currentText(),
            dialog.details(),
            dialog.source_id(),
        )
        worker.linked.connect(self._on_source_linked)
        worker.failed.connect(self._on_failed)
        self._start_worker(worker, "Đang gắn Source…")

    def _start_image_worker(self, worker: ImageEvidenceWorker, message: str) -> None:
        self._image_result = None
        worker.result.connect(self._store_image_result)
        worker.failed.connect(self._on_failed)
        self._start_worker(
            worker,
            message,
            lambda: self._finish_image(worker.action, worker.identifier),
        )

    def _store_image_result(self, result: object) -> None:
        self._image_result = result

    def choose_image_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or table_item(self.table, row, 0).text() != "IMAGE":
            QMessageBox.information(self, "Chọn Image", "Chọn một original IMAGE.")
            return
        file_id = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_image_worker(
            ImageEvidenceWorker("load", str(file_id), self.root),
            "Đang xác minh ảnh gốc…",
        )

    def choose_thumbnail_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.run_table.currentRow()
        if (
            row < 0
            or table_item(self.run_table, row, 1).text() != "IMAGE_THUMBNAIL"
            or table_item(self.run_table, row, 2).text() != "SUCCEEDED"
        ):
            QMessageBox.information(
                self, "Chọn thumbnail", "Chọn một task thumbnail đã thành công."
            )
            return
        file_id = table_item(self.run_table, row, 3).data(Qt.ItemDataRole.UserRole)
        if not file_id:
            QMessageBox.information(
                self, "Chọn thumbnail", "Task chưa có file dẫn xuất."
            )
            return
        self._start_image_worker(
            ImageEvidenceWorker("load", str(file_id), self.root),
            "Đang xác minh thumbnail…",
        )

    def choose_image_thumbnail(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or table_item(self.table, row, 0).text() != "IMAGE":
            QMessageBox.information(self, "Chọn Image", "Chọn một original IMAGE.")
            return
        file_id = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        worker = ThumbnailWorker(str(file_id), self.root)
        worker.completed.connect(self._on_thumbnail_completed)
        worker.failed.connect(self._on_thumbnail_failed)
        self._start_worker(worker, "Đang tạo thumbnail…")

    def _start_video_worker(self, worker: VideoEvidenceWorker, message: str) -> None:
        self._video_result = None
        worker.result.connect(self._store_video_result)
        worker.failed.connect(self._on_failed)
        self._start_worker(
            worker,
            message,
            lambda: self._finish_video(worker.action, worker.identifier),
        )

    def _store_video_result(self, result: object) -> None:
        self._video_result = result

    def choose_video_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or table_item(self.table, row, 0).text() != "VIDEO":
            QMessageBox.information(self, "Chọn Video", "Chọn một original VIDEO.")
            return
        file_id = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_video_worker(
            VideoEvidenceWorker("load", str(file_id), self.root),
            "Đang giải mã video gốc…",
        )

    def _start_audio_worker(self, worker: AudioEvidenceWorker, message: str) -> None:
        self._audio_result = None
        worker.result.connect(self._store_audio_result)
        worker.failed.connect(self._on_failed)
        self._start_worker(
            worker,
            message,
            lambda: self._finish_audio(worker.action, worker.identifier),
        )

    def _store_audio_result(self, result: object) -> None:
        self._audio_result = result

    def choose_audio_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or table_item(self.table, row, 0).text() != "VIDEO":
            QMessageBox.information(self, "Chọn Video", "Chọn một original MP4.")
            return
        file_id = table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)
        self._start_audio_worker(
            AudioEvidenceWorker("load", str(file_id), self.root),
            "Đang xác minh audio track…",
        )

    def _on_thumbnail_completed(self, _run_id: str) -> None:
        self.reload()
        self.status.setText("Thumbnail và run đã được ghi.")

    def _on_thumbnail_failed(self, message: str) -> None:
        self.reload()
        self._on_failed(message)

    def reopen_selected_text_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "TEXT_RANGE"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Text.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_evidence_worker(
            TextEvidenceWorker("reopen", str(version_id), self.root),
            "Đang xác minh Evidence…",
        )

    def correct_selected_text_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "TEXT_RANGE"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Text.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_evidence_worker(
            TextEvidenceWorker("load-correction", str(version_id), self.root),
            "Đang xác minh Evidence để sửa…",
        )

    def reopen_selected_image_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "IMAGE_REGION"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Image.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_image_worker(
            ImageEvidenceWorker("reopen", str(version_id), self.root),
            "Đang xác minh vùng ảnh…",
        )

    def correct_selected_image_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "IMAGE_REGION"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Image.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_image_worker(
            ImageEvidenceWorker("load-correction", str(version_id), self.root),
            "Đang xác minh Evidence ảnh để sửa…",
        )

    def reopen_selected_video_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "TIME_RANGE:video"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Video.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_video_worker(
            VideoEvidenceWorker("reopen", str(version_id), self.root),
            "Đang xác minh đoạn video…",
        )

    def reopen_selected_audio_evidence(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != "TIME_RANGE:audio"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Audio.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        self._start_audio_worker(
            AudioEvidenceWorker("reopen", str(version_id), self.root),
            "Đang xác minh đoạn audio…",
        )

    def correct_selected_video_evidence(self) -> None:
        self._correct_selected_time_evidence("video")

    def correct_selected_audio_evidence(self) -> None:
        self._correct_selected_time_evidence("audio")

    def _correct_selected_time_evidence(self, track: str) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if (
            row < 0
            or table_item(self.evidence_table, row, 1).data(Qt.ItemDataRole.UserRole)
            != f"TIME_RANGE:{track}"
        ):
            QMessageBox.information(
                self, "Chọn Evidence", f"Chọn một Evidence {track}."
            )
            return
        version_id = str(
            table_item(self.evidence_table, row, 0).data(Qt.ItemDataRole.UserRole)
        )
        if track == "video":
            self._start_video_worker(
                VideoEvidenceWorker("load-correction", version_id, self.root),
                "Đang xác minh Evidence video để sửa…",
            )
        else:
            self._start_audio_worker(
                AudioEvidenceWorker("load-correction", version_id, self.root),
                "Đang xác minh Evidence audio để sửa…",
            )

    def choose_evidence_review(self) -> None:
        if self._worker is not None:
            return
        row = self.evidence_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Version.")
            return
        version_id = table_item(self.evidence_table, row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        content = table_item(self.evidence_table, row, 1).text()
        state = table_item(self.evidence_table, row, 3).text()
        dialog = EvidenceReviewDialog(content, state)
        if dialog.exec() != EvidenceReviewDialog.DialogCode.Accepted:
            return
        worker = EvidenceReviewWorker(
            str(version_id),
            self.root,
            cast(ReviewAction, dialog.action.currentData()),
            dialog.actor.text(),
            dialog.reason.text(),
        )
        worker.completed.connect(self._on_evidence_reviewed)
        worker.failed.connect(self._on_failed)
        self._start_worker(worker, "Đang ghi review Evidence…")

    def _on_evidence_reviewed(self, state: str) -> None:
        self.reload()
        self.status.setText(f"Đã ghi review Evidence: {state}.")

    def _finish_video(self, action: str, identifier: str) -> None:
        result = self._video_result
        self._video_result = None
        if result is None:
            return
        if action == "load":
            dialog = VideoEvidenceDialog(cast(DecodedVideoFrame, result))
            if dialog.exec() == VideoEvidenceDialog.DialogCode.Accepted:
                self._start_video_worker(
                    VideoEvidenceWorker(
                        "create",
                        identifier,
                        self.root,
                        start_ms=dialog.start.value(),
                        end_ms=dialog.end.value(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                    ),
                    "Đang ghi Evidence video…",
                )
        elif action == "create":
            self.reload()
            self.status.setText("Evidence Video đã được tạo.")
        elif action == "reopen":
            content, decoded, start_ms, end_ms = cast(
                tuple[str, DecodedVideoFrame, int, int], result
            )
            VideoRangeView(content, decoded, start_ms, end_ms).exec()
        elif action == "load-correction":
            content, decoded, start_ms, end_ms = cast(
                tuple[str, DecodedVideoFrame, int, int], result
            )
            dialog = VideoEvidenceDialog(
                decoded,
                correction=True,
                start_ms=start_ms,
                end_ms=end_ms,
                content=content,
            )
            if dialog.exec() == VideoEvidenceDialog.DialogCode.Accepted:
                self._start_video_worker(
                    VideoEvidenceWorker(
                        "correct",
                        identifier,
                        self.root,
                        start_ms=dialog.start.value(),
                        end_ms=dialog.end.value(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                        reason=dialog.reason.text(),
                    ),
                    "Đang ghi Evidence video Version mới…",
                )
        elif action == "correct":
            _new_version_id, stale_count = cast(tuple[str, int], result)
            self.reload()
            self.status.setText(
                f"Đã tạo Evidence Video Version mới; {stale_count} Claim Version cần xem lại."
            )

    def _finish_audio(self, action: str, identifier: str) -> None:
        result = self._audio_result
        self._audio_result = None
        if result is None:
            return
        if action == "load":
            dialog = AudioEvidenceDialog(cast(DecodedAudioSegment, result))
            if dialog.exec() == AudioEvidenceDialog.DialogCode.Accepted:
                self._start_audio_worker(
                    AudioEvidenceWorker(
                        "create",
                        identifier,
                        self.root,
                        start_ms=dialog.start.value(),
                        end_ms=dialog.end.value(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                        evidence_type=str(dialog.kind.currentData()),
                    ),
                    "Đang ghi Evidence audio…",
                )
        elif action == "create":
            self.reload()
            self.status.setText("Evidence Audio đã được tạo.")
        elif action == "reopen":
            content, decoded, start_ms, end_ms = cast(
                tuple[str, DecodedAudioSegment, int, int], result
            )
            AudioRangeView(content, decoded, start_ms, end_ms).exec()
        elif action == "load-correction":
            content, decoded, start_ms, end_ms, kind = cast(
                tuple[str, DecodedAudioSegment, int, int, str], result
            )
            dialog = AudioEvidenceDialog(
                decoded,
                correction=True,
                start_ms=start_ms,
                end_ms=end_ms,
                content=content,
                evidence_type=kind,
            )
            if dialog.exec() == AudioEvidenceDialog.DialogCode.Accepted:
                self._start_audio_worker(
                    AudioEvidenceWorker(
                        "correct",
                        identifier,
                        self.root,
                        start_ms=dialog.start.value(),
                        end_ms=dialog.end.value(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                        reason=dialog.reason.text(),
                    ),
                    "Đang ghi Evidence audio Version mới…",
                )
        elif action == "correct":
            _new_version_id, stale_count = cast(tuple[str, int], result)
            self.reload()
            self.status.setText(
                f"Đã tạo Evidence Audio Version mới; {stale_count} Claim Version cần xem lại."
            )

    def _finish_image(self, action: str, identifier: str) -> None:
        result = self._image_result
        self._image_result = None
        if result is None:
            return
        if action == "load":
            dialog = ImageEvidenceDialog(cast(QImage, result))
            if dialog.exec() == ImageEvidenceDialog.DialogCode.Accepted:
                self._start_image_worker(
                    ImageEvidenceWorker(
                        "create",
                        identifier,
                        self.root,
                        region=dialog.region(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                    ),
                    "Đang ghi Evidence ảnh…",
                )
        elif action == "create":
            self.reload()
            self.status.setText("Evidence Image đã được tạo.")
        elif action == "reopen":
            content, crop = cast(tuple[str, QImage], result)
            ImageRegionView(content, crop).exec()
        elif action == "load-correction":
            image, region, content = cast(tuple[QImage, dict[str, float], str], result)
            dialog = ImageEvidenceDialog(
                image, correction=True, region=region, content=content
            )
            if dialog.exec() == ImageEvidenceDialog.DialogCode.Accepted:
                self._start_image_worker(
                    ImageEvidenceWorker(
                        "correct",
                        identifier,
                        self.root,
                        region=dialog.region(),
                        content=dialog.content.text(),
                        actor=dialog.actor.text(),
                        reason=dialog.reason.text(),
                    ),
                    "Đang ghi Evidence ảnh Version mới…",
                )
        elif action == "correct":
            _new_version_id, stale_count = cast(tuple[str, int], result)
            self.reload()
            self.status.setText(
                f"Đã tạo Evidence Image Version mới; {stale_count} Claim Version cần xem lại."
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
        elif action == "load-correction":
            snapshot, start, end = cast(tuple[str, int, int], result)
            dialog = TextEvidenceDialog(snapshot, correction=True, start=start, end=end)
            if dialog.exec() == TextEvidenceDialog.DialogCode.Accepted:
                self._start_evidence_worker(
                    TextEvidenceWorker(
                        "correct",
                        identifier,
                        self.root,
                        start=dialog.start.value(),
                        end=dialog.end.value(),
                        actor=dialog.actor.text(),
                        reason=dialog.reason.text(),
                    ),
                    "Đang ghi Evidence Version mới…",
                )
        elif action == "correct":
            _new_version_id, stale_count = cast(tuple[str, int], result)
            self.reload()
            self.status.setText(
                f"Đã tạo Evidence Version mới; {stale_count} Claim Version cần xem lại."
            )
        elif action == "reopen":
            QMessageBox.information(self, "Đoạn Evidence", str(result))

    def _on_imported(self, _asset_id: str) -> None:
        self.reload()

    def _on_source_linked(self, _source_id: str) -> None:
        self.status.setText("Source đã gắn vào Asset.")

    def _on_failed(self, message: str) -> None:
        self.status.setText(message)
        if self._media_cancel_requested and "Media decoder cancelled" in message:
            self.status.setText(
                "Đã hủy giải mã media; original và Evidence giữ nguyên."
            )
            return
        QMessageBox.warning(self, "Không thể hoàn tất tác vụ", message)

    def _on_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        # Qt finished/wait can precede Windows FLS cleanup. Retain the actual
        # thread handle, and never block the GUI waiting for native exit.
        guard = self._worker_exit
        if guard is None or guard.error is not None:
            self.status.setText(
                "Chưa xác minh được cleanup thread; giữ nguyên cửa sổ và tác vụ."
            )
            return
        if not guard.terminal() or not worker.wait(0):
            QTimer.singleShot(10, self._on_finished)
            return
        guard.close()
        self._worker_exit = None
        after_finished = self._after_worker_finished
        self._after_worker_finished = None
        self._worker = None
        self.cancel_media_button.setEnabled(False)
        for button in self._buttons:
            button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
        if self._close_after_worker:
            self.close()
        elif after_finished is not None:
            after_finished()

    def choose_maintenance(self) -> None:
        self.maintenance_requested = True
        if not self.close():
            self.maintenance_requested = False

    def choose_components(self) -> None:
        self.components_requested = True
        if not self.close():
            self.components_requested = False

    def choose_transcription(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or table_item(self.table, row, 0).text() != "VIDEO":
            QMessageBox.information(
                self, "Chọn Video", "Chọn một original VIDEO để chép lời."
            )
            return
        self.transcription_selection = (
            str(table_item(self.table, row, 0).data(Qt.ItemDataRole.UserRole)),
            table_item(self.table, row, 1).text(),
        )
        if not self.close():
            self.transcription_selection = None

    def choose_transcript_review(self) -> None:
        if self._worker is not None:
            return
        row = self.run_table.currentRow()
        if (
            row < 0
            or table_item(self.run_table, row, 1).text() != "AUDIO_TRANSCRIPTION"
            or table_item(self.run_table, row, 2).text() != "SUCCEEDED"
        ):
            QMessageBox.information(
                self,
                "Chọn task",
                "Chọn task chép lời AUDIO_TRANSCRIPTION đã thành công.",
            )
            return
        raw_id, display_name = table_item(self.run_table, row, 1).data(
            Qt.ItemDataRole.UserRole
        )
        if not raw_id:
            QMessageBox.information(
                self,
                "RAW chưa xác nhận",
                "Task cần đúng một RAW cùng Asset/run và parent PCM; chưa mở lựa chọn.",
            )
            return
        self.transcript_review_selection = (str(raw_id), str(display_name))
        if not self.close():
            self.transcript_review_selection = None

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            if self._worker_exit is not None and self._worker_exit.terminal():
                self._close_after_worker = True
                QTimer.singleShot(0, self._on_finished)
                event.ignore()
                return
            if self._media_worker_cancelable(self._worker):
                self._close_after_worker = True
                self._cancel_media_worker()
                event.ignore()
                return
            QMessageBox.information(
                self,
                "Đang xử lý",
                "Đợi tác vụ hiện tại hoàn tất trước khi đóng ứng dụng.",
            )
            event.ignore()
            return
        super().closeEvent(event)
