"""Two explicit user workflows over the same research service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_loop.backend.analysis.provider_selection import (
    provider_name,
    provider_ready,
    select_component_provider,
)
from creator_loop.backend.creator.research_pipeline import ResearchPipeline
from creator_loop.frontend.owned_thread import OwnedThreadExit
from creator_loop.shared.workflow import CreatorRequest, DraftSnapshot


class ResearchCreatorWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        pipeline: ResearchPipeline,
        request: CreatorRequest,
        action: str,
        workflow_id: str = "",
        direction: str = "",
        body_text: str = "",
        image_file_id: str | None = None,
        image_provenance: str = "",
        image_usage_note: str = "",
    ) -> None:
        super().__init__()
        self.pipeline, self.request, self.action = pipeline, request, action
        self.workflow_id, self.direction, self.body_text = (
            workflow_id,
            direction,
            body_text,
        )
        self.image_file_id = image_file_id
        self.image_provenance = image_provenance
        self.image_usage_note = image_usage_note

    def run(self) -> None:
        try:
            if self.isInterruptionRequested():
                raise InterruptedError("Đã dừng trước xử lý")
            wid = (
                self.workflow_id
                if self.action == "draft"
                else self.pipeline.analyze(self.request, self.isInterruptionRequested)
            )
            if self.isInterruptionRequested():
                raise InterruptedError(
                    "Đã dừng; giữ phân tích đã commit, chưa tạo Draft"
                )
            snapshot = None
            if self.action in ("automatic", "draft"):
                snapshot = self.pipeline.create_draft(
                    wid,
                    self.direction or self.request.direction,
                    body_text=self.body_text,
                    image_file_id=self.image_file_id,
                    image_provenance=self.image_provenance,
                    image_usage_note=self.image_usage_note,
                )
            self.completed.emit(
                (
                    wid,
                    snapshot,
                    False,  # B completes at a Draft; C requires a separate action.
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class ResearchCreatorDialog(QDialog):
    draft_ready = Signal(str, bool)
    evidence_requested = Signal(str)
    claim_requested = Signal(str)

    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pipeline = ResearchPipeline(root)
        self.workflow_id = ""
        self.worker: ResearchCreatorWorker | None = None
        self.guard: OwnedThreadExit | None = None
        self.result: Any = None
        self.error = ""
        self.close_after = False
        self.setWindowTitle("B — Phân tích và tạo Draft")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(
            min(1120, available.width() - 40) if available else 1120,
            min(900, available.height() - 60) if available else 900,
        )
        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        limit = QLabel(
            "Backend LOCAL_LLAMA_CPP: mô hình local đã chọn; chạy khi bấm Phân tích/Chạy B."
            if (root / "manifests/creator-llm-components.json").is_file()
            else "Backend LOCAL_STRUCTURED_RULES: cấu trúc text và mẫu editorial local; chưa có LLM sinh nội dung. Caption/URL không thay phân tích toàn ảnh/video. Kết quả máy chờ review."
        )
        limit.setWordWrap(True)
        layout.addWidget(limit)
        self.backend_label = limit
        if provider_name(root) == "CODEX_CLI_TEXT":
            limit.setText(
                "B — Codex CLI/GPT-6.1 Sol High: gửi riêng tư liệu đã chọn tới dịch vụ model qua đăng nhập Codex trên máy. Chỉ chạy khi bấm Phân tích/Chạy B."
            )
        elif provider_name(root) == "CLOUDFLARE_WORKERS_AI":
            limit.setText(
                "B — Workers AI: gửi riêng tư liệu đã chọn tới Cloudflare. Chỉ chạy khi bấm Phân tích/Chạy B; không khởi động A/C."
            )
        self.model_button = QPushButton("Chọn cấu hình backend AI của B")
        self.model_button.setObjectName("researchLocalModel")
        self.model_button.clicked.connect(self.select_local_model)
        layout.addWidget(self.model_button)
        form = QFormLayout()
        self.project = QComboBox()
        self.project.setObjectName("researchProject")
        for project in self.pipeline.projects():
            self.project.addItem(project.title, project.project_id)
        self.mode = QComboBox()
        self.mode.setObjectName("researchMode")
        self.mode.addItem("Thủ công — chạy từng bước", "MANUAL")
        self.mode.addItem("Tự động — tư liệu đã chọn → Draft chờ review", "AUTOMATIC")
        self.format = QComboBox()
        self.format.setObjectName("researchFormat")
        for label, value in (
            ("Bài viết", "TEXT"),
            ("Ảnh + caption", "IMAGE_CAPTION"),
            ("Video", "VIDEO"),
        ):
            self.format.addItem(label, value)
        self.purpose = QLineEdit("nhận thức đúng về sự thật")
        self.purpose.setObjectName("researchPurpose")
        self.audience = QLineEdit("Người đọc muốn kiểm chứng thông tin")
        self.audience.setObjectName("researchAudience")
        self.style_input = QLineEdit("Rõ ràng, thận trọng, có nguồn")
        self.style_input.setObjectName("researchStyle")
        for label, widget in (
            ("Project", self.project),
            ("Chế độ", self.mode),
            ("Định dạng", self.format),
            ("Mục đích", self.purpose),
            ("Khán giả", self.audience),
            ("Phong cách", self.style_input),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.material_table = QTableWidget(0, 5)
        self.material_table.setObjectName("researchMaterials")
        self.material_table.setHorizontalHeaderLabels(
            (
                "Chọn",
                "Tư liệu đã lưu",
                "Loại / phạm vi",
                "Phần đã đọc",
                "Evidence Versions",
            )
        )
        layout.addWidget(self.material_table, 1)
        self.material_rows = self.pipeline.materials()
        self.material_table.setRowCount(len(self.material_rows))
        for index, material in enumerate(self.material_rows):
            pick = QTableWidgetItem()
            pick.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            pick.setCheckState(Qt.CheckState.Unchecked)
            self.material_table.setItem(index, 0, pick)
            for column, value in enumerate(
                (
                    material.source_url or material.material_id,
                    material.media_type + " / " + material.scope,
                    material.text[:180] or "Chưa xử lý nội dung",
                    ", ".join(material.evidence_version_ids) or "Chưa có",
                ),
                1,
            ):
                self.material_table.setItem(index, column, QTableWidgetItem(value))
        for column, width in enumerate((50, 290, 220, 290, 200)):
            self.material_table.setColumnWidth(column, width)
        self.history = QComboBox()
        self.history.setObjectName("researchWorkflowHistory")
        layout.addWidget(self.history)
        self.analysis = QPlainTextEdit()
        self.analysis.setObjectName("researchAnalysis")
        self.analysis.setReadOnly(True)
        self.analysis.setMaximumHeight(170)
        layout.addWidget(self.analysis)
        self.direction = QPlainTextEdit()
        self.direction.setObjectName("researchDirection")
        self.direction.setPlaceholderText(
            "Hướng đề xuất; Creator có thể chọn/chỉnh trước khi tạo Draft"
        )
        self.direction.setMaximumHeight(65)
        layout.addWidget(self.direction)
        self.body = QPlainTextEdit()
        self.body.setObjectName("researchDraftBody")
        self.body.setPlaceholderText(
            "Có thể tự viết/sửa Draft trong chế độ thủ công; bản sinh local vẫn cần kiểm căn cứ"
        )
        self.body.setMaximumHeight(150)
        layout.addWidget(self.body)
        self.image_choice = QComboBox()
        self.image_choice.setObjectName("researchDraftImage")
        self.image_choice.addItem("Không đính kèm ảnh", "")
        for item in self.pipeline.image_options():
            self.image_choice.addItem(
                item["display_name"] + " — " + item["file_id"], item["file_id"]
            )
        self.image_provenance = QLineEdit()
        self.image_provenance.setPlaceholderText(
            "Nguồn/tác giả của ảnh đã nhập Library"
        )
        self.image_usage_note = QLineEdit()
        self.image_usage_note.setPlaceholderText(
            "Căn cứ quyền sử dụng; ghi chú này không cấp Approval"
        )
        image_form = QFormLayout()
        image_form.addRow("Ảnh cho caption (Library)", self.image_choice)
        image_form.addRow("Nguồn ảnh", self.image_provenance)
        image_form.addRow("Quyền sử dụng ảnh", self.image_usage_note)
        layout.addLayout(image_form)
        actions = QHBoxLayout()
        self.analyze_button = QPushButton("Phân tích đã chọn")
        self.analyze_button.setObjectName("researchAnalyze")
        self.auto_button = QPushButton("Chạy tự động → Draft")
        self.auto_button.setObjectName("researchAutomatic")
        self.draft_button = QPushButton("Chọn hướng → tạo / lưu Draft Version")
        self.draft_button.setObjectName("researchCreateDraft")
        self.stop_button = QPushButton("Dừng")
        self.stop_button.setObjectName("researchStop")
        for button in (
            self.analyze_button,
            self.auto_button,
            self.draft_button,
            self.stop_button,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)
        refs = QHBoxLayout()
        self.evidence = QComboBox()
        self.evidence.setObjectName("researchEvidence")
        evidence_button = QPushButton("Mở / sửa Evidence")
        evidence_button.clicked.connect(
            lambda: (
                self.evidence_requested.emit(str(self.evidence.currentData()))
                if self.evidence.currentData()
                else None
            )
        )
        self.claim = QComboBox()
        self.claim.setObjectName("researchClaim")
        claim_button = QPushButton("Mở / sửa Claim")
        claim_button.clicked.connect(
            lambda: (
                self.claim_requested.emit(str(self.claim.currentData()))
                if self.claim.currentData()
                else None
            )
        )
        for reference_widget in (
            self.evidence,
            evidence_button,
            self.claim,
            claim_button,
        ):
            refs.addWidget(reference_widget)
        layout.addLayout(refs)
        self.status = QLabel("Chọn Project và tư liệu; không tự nhập toàn bộ ứng viên.")
        self.status.setObjectName("researchStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.reap)
        self.analyze_button.clicked.connect(lambda: self.start("analyze"))
        self.auto_button.clicked.connect(lambda: self.start("automatic"))
        self.draft_button.clicked.connect(lambda: self.start("draft"))
        self.stop_button.clicked.connect(self.cancel)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.project.currentIndexChanged.connect(self.reload_history)
        self.history.currentIndexChanged.connect(self.load_history)
        self.reload_history()
        self.mode_changed()

    def mode_changed(self) -> None:
        manual = self.mode.currentData() == "MANUAL"
        idle = self.worker is None
        self.analyze_button.setEnabled(manual and idle)
        self.auto_button.setEnabled(not manual and idle)
        self.draft_button.setEnabled(manual and idle and bool(self.workflow_id))
        self.body.setReadOnly(not manual)
        self.stop_button.setEnabled(not idle)

    def reload_history(self) -> None:
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("Lượt mới — chọn tư liệu", "")
        if self.project.currentData():
            for record in self.pipeline.workflows(str(self.project.currentData())):
                self.history.addItem(
                    record["created_at"]
                    + " — "
                    + record["format"]
                    + " — "
                    + record["purpose"],
                    record["workflow_id"],
                )
        self.history.blockSignals(False)
        self.workflow_id = ""
        self.mode_changed()

    def load_history(self) -> None:
        wid = self.history.currentData()
        if wid:
            self.show_workflow(str(wid))
        else:
            self.workflow_id = ""
            self.analysis.clear()
            self.body.clear()
            self.direction.clear()
            self.evidence.clear()
            self.claim.clear()
        self.mode_changed()

    def show_workflow(self, wid: str) -> None:
        value = self.pipeline.load(wid)
        self.workflow_id = wid
        self.analysis.setPlainText(value["analysis_text"])
        self.direction.setPlainText(value["direction_text"])
        self.purpose.setText(value["purpose"])
        self.audience.setText(value["audience"])
        self.style_input.setText(value["style"])
        self.format.setCurrentIndex(self.format.findData(value["output_format"]))
        for index, material in enumerate(self.material_rows):
            pick = self.material_table.item(index, 0)
            if pick is None:
                raise ValueError("Tư liệu trong bảng chưa có ô lựa chọn.")
            pick.setCheckState(
                Qt.CheckState.Checked
                if material.material_id in value["material_ids"]
                else Qt.CheckState.Unchecked
            )
        self.evidence.clear()
        for ev in dict.fromkeys(
            r["evidence_version_id"]
            for r in value["materials"]
            if r["evidence_version_id"]
        ):
            self.evidence.addItem(str(ev), ev)
        self.claim.clear()
        latest_draft = (
            self.pipeline.draft_metadata(value["drafts"][0])
            if value["drafts"]
            else None
        )
        for cv in dict.fromkeys(
            value["claims"]
            + list(latest_draft.claim_version_ids if latest_draft else ())
        ):
            self.claim.addItem(cv, cv)
        if latest_draft:
            self.body.setPlainText(latest_draft.body_text)
            self.direction.setPlainText(
                latest_draft.direction or value["direction_text"]
            )
            self.image_choice.setCurrentIndex(
                max(0, self.image_choice.findData(latest_draft.image_file_id or ""))
            )
            self.image_provenance.setText(latest_draft.image_provenance)
            self.image_usage_note.setText(latest_draft.image_usage_note)
        else:
            self.body.clear()
            self.image_choice.setCurrentIndex(0)
            self.image_provenance.clear()
            self.image_usage_note.clear()
        self.body.document().setModified(False)
        self.status.setText(
            "Đã mở kết quả đã lưu; exact Evidence/Claim/Draft Versions giữ nguyên. Có thể chuyển chế độ và tiếp tục."
        )

    def request(self) -> CreatorRequest:
        selected = []
        for index, material in enumerate(self.material_rows):
            pick = self.material_table.item(index, 0)
            if pick is not None and pick.checkState() == Qt.CheckState.Checked:
                selected.append(material.material_id)
        ids = tuple(selected)
        return CreatorRequest(
            str(self.project.currentData() or ""),
            self.mode.currentData(),
            self.purpose.text(),
            self.audience.text(),
            self.style_input.text(),
            self.format.currentData(),
            ids,
            self.direction.toPlainText(),
        )

    def select_local_model(self) -> None:
        if self.worker is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Chọn manifest backend AI của B", "", "Component manifest (*.json)"
        )
        if not path:
            return
        try:
            chosen = select_component_provider(self.pipeline.root, Path(path))
            self.backend_label.setText(
                "Workers AI/Cloudflare: gửi riêng text tư liệu đã chọn khi bấm Phân tích/Chạy B; không khởi chạy A/C."
                if chosen == "CLOUDFLARE_WORKERS_AI"
                else "Codex CLI/GPT-6.1 Sol High: tư liệu chọn sẽ gửi tới dịch vụ model; chỉ chạy khi bấm nút."
                if chosen == "CODEX_CLI_TEXT"
                else "Backend LOCAL_LLAMA_CPP: chỉ chạy khi bấm Phân tích/Chạy B; digest sẽ được kiểm trong worker."
            )
            self.status.setText("Đã chọn backend B. Chưa chạy hoặc tạo Draft.")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def start(self, action: str) -> None:
        if self.worker is not None:
            return
        request = self.request()
        if action != "draft" and (not request.project_id or not request.material_ids):
            self.status.setText(
                "Chọn Project và ít nhất một tư liệu đã lưu trước khi chạy."
            )
            return
        if action == "automatic" and not provider_ready(self.pipeline.root):
            self.status.setText(
                "Chưa cấu hình backend AI cho B; không thay bằng template"
            )
            return
        self.result, self.error = None, ""
        worker = ResearchCreatorWorker(
            self.pipeline,
            request,
            action,
            self.workflow_id,
            self.direction.toPlainText(),
            self.body.toPlainText()
            if action == "draft" and self.body.document().isModified()
            else "",
            str(self.image_choice.currentData() or ""),
            self.image_provenance.text(),
            self.image_usage_note.text(),
        )
        self.worker = worker
        self.guard = OwnedThreadExit()
        worker.started.connect(self.guard.capture, Qt.ConnectionType.DirectConnection)
        worker.completed.connect(lambda result: setattr(self, "result", result))
        worker.failed.connect(lambda error: setattr(self, "error", error))
        self.status.setText("Đang xử lý các tư liệu đã chọn…")
        self.mode.setEnabled(False)
        self.project.setEnabled(False)
        self.history.setEnabled(False)
        self.mode_changed()
        worker.start()
        self.timer.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.requestInterruption()
            self.status.setText("Đang dừng; giữ kết quả đã commit. Chrome vẫn mở.")

    def reap(self) -> None:
        if (
            self.worker is None
            or self.guard is None
            or self.guard.error
            or not self.guard.terminal()
            or not self.worker.wait(0)
        ):
            return
        self.guard.close()
        self.guard = None
        self.worker.deleteLater()
        self.worker = None
        self.timer.stop()
        self.mode.setEnabled(True)
        self.project.setEnabled(True)
        self.history.setEnabled(True)
        if self.error:
            self.status.setText(self.error)
        elif self.result:
            wid, snapshot, automatic_video = self.result
            self.reload_history()
            self.history.setCurrentIndex(self.history.findData(wid))
            if isinstance(snapshot, DraftSnapshot):
                self.status.setText(
                    f"Đã lưu Draft Version {snapshot.version_no}: {snapshot.draft_version_id}; chờ review, chưa Selection/Approval."
                )
                self.draft_ready.emit(snapshot.draft_version_id, automatic_video)
        self.mode_changed()
        if self.close_after:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            self.close_after = True
            self.cancel()
            event.ignore()
        else:
            event.accept()
