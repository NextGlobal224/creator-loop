"""Small Windows Library view for importing and listing physical originals."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Callable, cast

from PySide6.QtCore import Qt, QThread, Signal
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

from creator_loop.audio_evidence import (
    DecodedAudioSegment,
    create_audio_evidence,
    decode_audio_segment,
    reopen_audio_evidence,
)
from creator_loop.audio_evidence_ui import AudioEvidenceDialog, AudioRangeView
from creator_loop.claim_ui import ClaimDialog
from creator_loop.creator_ui import CreatorDialog
from creator_loop.database import _connect_write, open_readonly
from creator_loop.evidence_correction import (
    claim_versions_needing_review,
    correct_text_evidence,
)
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.evidence_review import (
    ReviewAction,
    current_evidence_review,
    record_evidence_review,
)
from creator_loop.evidence_review_ui import EvidenceReviewDialog
from creator_loop.image_evidence import (
    create_image_evidence,
    read_verified_image,
    reopen_image_region,
)
from creator_loop.image_evidence_correction import correct_image_evidence
from creator_loop.image_evidence_ui import ImageEvidenceDialog, ImageRegionView
from creator_loop.image_thumbnail import create_image_thumbnail
from creator_loop.media_intake import intake_image_original, intake_video_original
from creator_loop.project_ui import ProjectDialog
from creator_loop.publication_ui import PublicationDialog
from creator_loop.selection_ui import SelectionDialog
from creator_loop.source_association import (
    SourceDetails,
    create_source_for_asset,
    link_existing_source,
)
from creator_loop.source_ui import SourceDialog
from creator_loop.storage_ui import StorageDialog
from creator_loop.text_evidence import create_text_evidence, read_verified_text_snapshot
from creator_loop.text_evidence_ui import TextEvidenceDialog
from creator_loop.text_intake import intake_text_original
from creator_loop.time_evidence_correction import correct_time_evidence
from creator_loop.video_evidence import (
    DecodedVideoFrame,
    create_video_evidence,
    decode_video_frame,
    reopen_video_evidence,
)
from creator_loop.video_evidence_ui import VideoEvidenceDialog, VideoRangeView
from creator_loop.whole_evidence import WholeSource
from creator_loop.whole_evidence_ui import (
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
            elif self.action == "load-correction":
                with closing(open_readonly(db_path)) as db:
                    reopened = reopen_evidence_version(db, self.identifier, self.root)
                    if reopened.locator_type != "TEXT_RANGE":
                        raise ValueError("Selected Evidence is not a text range")
                    _asset_id, snapshot = read_verified_text_snapshot(
                        db, reopened.anchor_file_id, self.root
                    )
                    start = reopened.locator["start"]
                    end = reopened.locator["end"]
                    if type(start) is not int or type(end) is not int:
                        raise ValueError("Selected Evidence has an invalid text range")
                self.result.emit(
                    (
                        snapshot,
                        start,
                        end,
                    )
                )
            elif self.action == "correct":
                with closing(_connect_write(db_path)) as db:
                    corrected = correct_text_evidence(
                        db,
                        evidence_version_id=self.identifier,
                        data_root=self.root,
                        start=self.range_start,
                        end=self.range_end,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    stale_claims = claim_versions_needing_review(
                        db, corrected.evidence_id
                    )
                self.result.emit((corrected.new_version_id, len(stale_claims)))
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
        try:
            db_path = self.root / "creator_loop.sqlite3"
            if self.action == "load":
                with closing(open_readonly(db_path)) as db:
                    _asset_id, image = read_verified_image(
                        db, self.identifier, self.root
                    )
                self.result.emit(image)
            elif self.action == "create":
                if self.region is None:
                    raise ValueError("Image region is required")
                with closing(_connect_write(db_path)) as db:
                    version = create_image_evidence(
                        db,
                        file_id=self.identifier,
                        data_root=self.root,
                        region=self.region,
                        content=self.content,
                        actor=self.actor,
                    )
                self.result.emit(version.evidence_version_id)
            elif self.action == "reopen":
                with closing(open_readonly(db_path)) as db:
                    self.result.emit(
                        reopen_image_region(db, self.identifier, self.root)
                    )
            elif self.action == "load-correction":
                with closing(open_readonly(db_path)) as db:
                    reopened = reopen_evidence_version(db, self.identifier, self.root)
                    if reopened.locator_type != "IMAGE_REGION":
                        raise ValueError("Selected Evidence is not an image region")
                    _asset_id, image = read_verified_image(
                        db, reopened.anchor_file_id, self.root
                    )
                    row = db.execute(
                        "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
                        (self.identifier,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("Selected Evidence version does not exist")
                self.result.emit(
                    (image, cast(dict[str, float], reopened.locator), str(row[0]))
                )
            elif self.action == "correct":
                if self.region is None:
                    raise ValueError("Image region is required")
                with closing(_connect_write(db_path)) as db:
                    corrected = correct_image_evidence(
                        db,
                        evidence_version_id=self.identifier,
                        data_root=self.root,
                        region=self.region,
                        content=self.content,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    stale_claims = claim_versions_needing_review(
                        db, corrected.evidence_id
                    )
                self.result.emit((corrected.new_version_id, len(stale_claims)))
            else:
                raise ValueError("Unknown image Evidence action")
        except Exception as exc:
            self.failed.emit(f"Không thể xử lý Evidence ảnh: {exc}")


class ThumbnailWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, file_id: str, root: Path) -> None:
        super().__init__()
        self.file_id = file_id
        self.root = root

    def run(self) -> None:
        try:
            result = create_image_thumbnail(self.file_id, data_root=self.root)
            self.completed.emit(result.run_id)
        except Exception as exc:
            self.failed.emit(f"Không thể tạo thumbnail: {exc}")


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
        db_path = self.root / "creator_loop.sqlite3"
        try:
            if self.action == "load":
                with closing(open_readonly(db_path)) as db:
                    _asset_id, decoded = decode_video_frame(
                        db, self.identifier, self.root, start_ms=0
                    )
                self.result.emit(decoded)
            elif self.action == "create":
                with closing(_connect_write(db_path)) as db:
                    version = create_video_evidence(
                        db,
                        file_id=self.identifier,
                        data_root=self.root,
                        start_ms=self.start_ms,
                        end_ms=self.end_ms,
                        content=self.content,
                        actor=self.actor,
                    )
                self.result.emit(version.evidence_version_id)
            elif self.action == "correct":
                with closing(_connect_write(db_path)) as db:
                    corrected = correct_time_evidence(
                        db,
                        evidence_version_id=self.identifier,
                        data_root=self.root,
                        start_ms=self.start_ms,
                        end_ms=self.end_ms,
                        content=self.content,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    stale_count = len(
                        claim_versions_needing_review(db, corrected.evidence_id)
                    )
                self.result.emit((corrected.new_version_id, stale_count))
            elif self.action in ("reopen", "load-correction"):
                with closing(open_readonly(db_path)) as db:
                    self.result.emit(
                        reopen_video_evidence(db, self.identifier, self.root)
                    )
            else:
                raise ValueError("Unknown video Evidence action")
        except Exception as exc:
            self.failed.emit(f"Không thể xử lý Evidence video: {exc}")


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
        db_path = self.root / "creator_loop.sqlite3"
        try:
            if self.action == "load":
                with closing(open_readonly(db_path)) as db:
                    _asset_id, decoded = decode_audio_segment(
                        db, self.identifier, self.root, start_ms=0, end_ms=1
                    )
                self.result.emit(decoded)
            elif self.action == "create":
                with closing(_connect_write(db_path)) as db:
                    version = create_audio_evidence(
                        db,
                        file_id=self.identifier,
                        data_root=self.root,
                        start_ms=self.start_ms,
                        end_ms=self.end_ms,
                        content=self.content,
                        actor=self.actor,
                        evidence_type=self.evidence_type,
                    )
                self.result.emit(version.evidence_version_id)
            elif self.action == "correct":
                with closing(_connect_write(db_path)) as db:
                    corrected = correct_time_evidence(
                        db,
                        evidence_version_id=self.identifier,
                        data_root=self.root,
                        start_ms=self.start_ms,
                        end_ms=self.end_ms,
                        content=self.content,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    stale_count = len(
                        claim_versions_needing_review(db, corrected.evidence_id)
                    )
                self.result.emit((corrected.new_version_id, stale_count))
            elif self.action == "load-correction":
                with closing(open_readonly(db_path)) as db:
                    reopened = reopen_audio_evidence(db, self.identifier, self.root)
                    kind = db.execute(
                        """SELECT e.evidence_type FROM evidences e
                           JOIN evidence_versions v ON v.evidence_id=e.evidence_id
                           WHERE v.evidence_version_id=?""",
                        (self.identifier,),
                    ).fetchone()[0]
                    if kind not in ("SPEECH", "OTHER"):
                        raise ValueError(
                            "Audio correction requires SPEECH or OTHER Evidence"
                        )
                self.result.emit((*reopened, str(kind)))
            elif self.action == "reopen":
                with closing(open_readonly(db_path)) as db:
                    self.result.emit(
                        reopen_audio_evidence(db, self.identifier, self.root)
                    )
            else:
                raise ValueError("Unknown audio Evidence action")
        except Exception as exc:
            self.failed.emit(f"Không thể xử lý Evidence audio: {exc}")


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
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                record_evidence_review(
                    db,
                    evidence_version_id=self.version_id,
                    data_root=self.root,
                    action=self.action,
                    actor=self.actor,
                    reason=self.reason,
                )
                state = current_evidence_review(db, self.version_id)
            self.completed.emit(state)
        except Exception as exc:
            self.failed.emit(f"Không thể review Evidence: {exc}")


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
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                if self.existing_source_id is None:
                    source_id = create_source_for_asset(
                        db,
                        asset_id=self.asset_id,
                        details=self.details,
                        relationship_type=self.relationship_type,
                    )
                else:
                    source_id = self.existing_source_id
                    link_existing_source(
                        db,
                        source_id=source_id,
                        asset_id=self.asset_id,
                        relationship_type=self.relationship_type,
                    )
            self.linked.emit(source_id)
        except Exception as exc:
            self.failed.emit(f"Không thể gắn Source: {exc}")


class LibraryWindow(QMainWindow):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: QThread | None = None
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
        self.run_table = QTableWidget(0, 4)
        self.run_table.setHorizontalHeaderLabels(
            ("Asset", "Task", "Trạng thái", "File dẫn xuất")
        )
        self.run_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.run_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.run_table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.reload()

    def reload(self) -> None:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            rows = db.execute(
                """SELECT a.media_type, a.display_name, f.mime_type,
                          f.byte_size, f.file_id, a.asset_id
                   FROM assets a JOIN asset_files f ON f.asset_id = a.asset_id
                   WHERE f.role = 'ORIGINAL' AND a.deleted_at IS NULL
                   ORDER BY a.created_at DESC, f.created_at DESC, f.file_id DESC"""
            ).fetchall()
            evidence_rows = db.execute(
                """SELECT v.evidence_version_id, a.display_name, v.content,
                          v.version_no, v.locator_type,
                          json_extract(v.locator_data,'$.track'),f.role,
                          COALESCE((SELECT action FROM review_events re
                                    WHERE re.evidence_version_id=v.evidence_version_id
                                    ORDER BY re.rowid DESC LIMIT 1),'PENDING')
                   FROM evidence_versions v
                   JOIN evidences e ON e.evidence_id = v.evidence_id
                   JOIN assets a ON a.asset_id = e.asset_id
                   JOIN asset_files f ON f.file_id=v.anchor_file_id
                   WHERE e.deleted_at IS NULL
                   ORDER BY v.created_at DESC, v.evidence_version_id DESC"""
            ).fetchall()
            run_rows = db.execute(
                """SELECT a.display_name,r.task_type,r.status,
                          COALESCE((SELECT f.storage_key FROM asset_files f
                                    WHERE f.processing_run_id=r.run_id
                                    ORDER BY f.created_at,f.file_id LIMIT 1),''),
                          (SELECT f.file_id FROM asset_files f
                           WHERE f.processing_run_id=r.run_id
                           ORDER BY f.created_at,f.file_id LIMIT 1)
                   FROM processing_runs r JOIN assets a ON a.asset_id=r.asset_id
                   ORDER BY r.created_at DESC,r.run_id DESC"""
            ).fetchall()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row[:4]):
                self.table.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )
            self.table.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, row[4])
            self.table.item(row_index, 1).setData(Qt.ItemDataRole.UserRole, row[5])
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
            self.run_table.item(row_index, 3).setData(Qt.ItemDataRole.UserRole, row[4])
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
            identifier = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        elif action in ("reopen", "load-correction"):
            row = self.evidence_table.currentRow()
            if (
                row < 0
                or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
                != "WHOLE_ASSET"
            ):
                QMessageBox.information(
                    self, "Chọn Evidence", "Chọn một Evidence toàn nguồn."
                )
                return
            identifier = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            asset_id = str(self.table.item(row, 1).data(Qt.ItemDataRole.UserRole))
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
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                row = db.execute(
                    """SELECT d.project_id FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
                    JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id WHERE v.draft_version_id=?""",
                    (version_id,),
                ).fetchone()
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
                self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                != version_id
            ):
                continue
            self.evidence_table.setCurrentCell(row, 0)
            locator = self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
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
        item = self.table.item(row, 1)
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
        if row < 0 or self.table.item(row, 0).text() != "IMAGE":
            QMessageBox.information(self, "Chọn Image", "Chọn một original IMAGE.")
            return
        file_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.run_table.item(row, 1).text() != "IMAGE_THUMBNAIL"
            or self.run_table.item(row, 2).text() != "SUCCEEDED"
        ):
            QMessageBox.information(
                self, "Chọn thumbnail", "Chọn một task thumbnail đã thành công."
            )
            return
        file_id = self.run_table.item(row, 3).data(Qt.ItemDataRole.UserRole)
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
        if row < 0 or self.table.item(row, 0).text() != "IMAGE":
            QMessageBox.information(self, "Chọn Image", "Chọn một original IMAGE.")
            return
        file_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
        if row < 0 or self.table.item(row, 0).text() != "VIDEO":
            QMessageBox.information(self, "Chọn Video", "Chọn một original VIDEO.")
            return
        file_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
        if row < 0 or self.table.item(row, 0).text() != "VIDEO":
            QMessageBox.information(self, "Chọn Video", "Chọn một original MP4.")
            return
        file_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "TEXT_RANGE"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Text.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "TEXT_RANGE"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Text.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "IMAGE_REGION"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Image.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "IMAGE_REGION"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Image.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "TIME_RANGE:video"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Video.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != "TIME_RANGE:audio"
        ):
            QMessageBox.information(self, "Chọn Evidence", "Chọn một Evidence Audio.")
            return
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
            or self.evidence_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            != f"TIME_RANGE:{track}"
        ):
            QMessageBox.information(
                self, "Chọn Evidence", f"Chọn một Evidence {track}."
            )
            return
        version_id = str(
            self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
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
        version_id = self.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        content = self.evidence_table.item(row, 1).text()
        state = self.evidence_table.item(row, 3).text()
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
