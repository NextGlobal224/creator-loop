"""Manage Project workspaces and explicit references through domain services."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_loop.database import _connect_write, open_readonly
from creator_loop.projects import (
    Project,
    UsageIntent,
    add_project_reference,
    archive_project,
    create_project,
    list_project_references,
    list_projects,
    list_reference_targets,
    reference_target_label,
)


class ProjectWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        action: str,
        *,
        title: str = "",
        project_id: str = "",
        target_type: str = "",
        target_id: str = "",
        usage_intent: UsageIntent = "RESEARCH",
    ) -> None:
        super().__init__()
        self.root = root
        self.action = action
        self.title = title
        self.project_id = project_id
        self.target_type = target_type
        self.target_id = target_id
        self.usage_intent = usage_intent

    def run(self) -> None:
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                if self.action == "create":
                    saved_project_id = create_project(db, title=self.title).project_id
                elif self.action == "archive":
                    saved_project_id = archive_project(
                        db, project_id=self.project_id
                    ).project_id
                elif self.action == "reference":
                    if self.target_type not in (
                        "asset_id",
                        "source_id",
                        "claim_version_id",
                    ):
                        raise ValueError("Invalid Project reference target type")
                    add_project_reference(
                        db,
                        project_id=self.project_id,
                        usage_intent=self.usage_intent,
                        **{self.target_type: self.target_id},
                    )
                    saved_project_id = self.project_id
                else:
                    raise ValueError("Unknown Project action")
            self.completed.emit(saved_project_id)
        except Exception as exc:
            self.failed.emit(f"Không thể ghi Project: {exc}")


class ProjectDialog(QDialog):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: ProjectWorker | None = None
        self._projects: dict[str, Project] = {}
        self._result_project_id: str | None = None
        self._failure: str | None = None
        self.setWindowTitle("Projects — tư liệu tham chiếu")
        self.resize(1000, 620)
        layout = QVBoxLayout(self)

        create_row = QHBoxLayout()
        self.title = QLineEdit()
        self.title.setPlaceholderText("Tên Project mới")
        self.create_button = QPushButton("Tạo Project")
        self.create_button.clicked.connect(self.create_new_project)
        create_row.addWidget(self.title)
        create_row.addWidget(self.create_button)
        layout.addLayout(create_row)

        project_row = QHBoxLayout()
        project_row.addWidget(QLabel("Project"))
        self.project = QComboBox()
        self.project.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.project.setMinimumContentsLength(24)
        project_row.addWidget(self.project, 1)
        self.include_archived = QCheckBox("Hiện Project đã lưu trữ")
        project_row.addWidget(self.include_archived)
        self.archive_button = QPushButton("Lưu trữ Project")
        self.archive_button.clicked.connect(self.archive_selected_project)
        project_row.addWidget(self.archive_button)
        layout.addLayout(project_row)

        layout.addWidget(QLabel("Tham chiếu đã lưu; Claim giữ đúng Version được chọn."))
        self.references = QTableWidget(0, 4)
        self.references.setHorizontalHeaderLabels(
            ("Loại", "Nội dung", "ID chính xác", "Mục đích")
        )
        self.references.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.references.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for column, width in enumerate((100, 390, 260, 150)):
            self.references.setColumnWidth(column, width)
        self.references.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.references)

        form = QFormLayout()
        self.target_type = QComboBox()
        self.target_type.addItem("Asset", "asset_id")
        self.target_type.addItem("Source", "source_id")
        self.target_type.addItem("Claim Version", "claim_version_id")
        form.addRow("Loại tham chiếu", self.target_type)
        self.target = QComboBox()
        self.target.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.target.setMinimumContentsLength(24)
        form.addRow("Đối tượng", self.target)
        self.usage = QComboBox()
        for label, intent in (
            ("Nghiên cứu — RESEARCH", "RESEARCH"),
            ("Trích dẫn — QUOTE", "QUOTE"),
            ("Dùng lại media — REUSE_MEDIA", "REUSE_MEDIA"),
        ):
            self.usage.addItem(label, intent)
        form.addRow("Mục đích", self.usage)
        layout.addLayout(form)
        self.add_button = QPushButton("Thêm tham chiếu")
        self.add_button.clicked.connect(self.add_selected_reference)
        layout.addWidget(self.add_button)
        layout.addWidget(
            QLabel("REUSE_MEDIA chỉ ghi mục đích; không cấp quyền dùng media.")
        )
        self.status = QLabel("Chọn Project hoặc tạo Project mới.")
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.title.textChanged.connect(self._refresh_controls)
        self.project.currentIndexChanged.connect(self._project_changed)
        self.include_archived.toggled.connect(lambda _checked: self.reload())
        self.target_type.currentIndexChanged.connect(self._reload_targets)
        self.target.currentIndexChanged.connect(self._refresh_controls)
        self.reload()

    def selected_project_id(self) -> str | None:
        selected = self.project.currentData()
        return str(selected) if selected is not None else None

    def reload(self, project_id: str | None = None) -> bool:
        if self._worker is not None:
            return False
        selected = project_id or self.selected_project_id()
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                projects = list_projects(
                    db, include_archived=self.include_archived.isChecked()
                )
            self._projects = {project.project_id: project for project in projects}
            self.project.blockSignals(True)
            self.project.clear()
            for project in projects:
                label = f"{project.title} — {project.status} [{project.project_id}]"
                self.project.addItem(
                    label,
                    project.project_id,
                )
                self.project.setItemData(
                    self.project.count() - 1, label, Qt.ItemDataRole.ToolTipRole
                )
            selection_index = self.project.findData(selected)
            if selection_index >= 0:
                self.project.setCurrentIndex(selection_index)
            self.project.blockSignals(False)
            references_loaded = self._project_changed()
            targets_loaded = self._reload_targets()
            if not references_loaded or not targets_loaded:
                return False
        except Exception as exc:
            self.project.blockSignals(False)
            self._projects.clear()
            self.project.clear()
            self.references.setRowCount(0)
            self.target.clear()
            self.status.setText(f"Không thể đọc Project: {exc}")
            QMessageBox.warning(self, "Project", self.status.text())
            self._refresh_controls()
            return False
        self._refresh_controls()
        return True

    def _project_changed(self) -> bool:
        self.references.setRowCount(0)
        project_id = self.selected_project_id()
        if project_id is not None:
            try:
                with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                    references = list_project_references(db, project_id=project_id)
                    labels = [
                        reference_target_label(db, reference)
                        for reference in references
                    ]
                self.references.setRowCount(len(references))
                identifier: str | None
                for index, (reference, label) in enumerate(zip(references, labels)):
                    if reference.asset_id is not None:
                        kind, identifier = "Asset", reference.asset_id
                    elif reference.claim_version_id is not None:
                        kind, identifier = "Claim Version", reference.claim_version_id
                    else:
                        kind, identifier = "Source", reference.source_id
                    for column, value in enumerate(
                        (kind, label, identifier, reference.usage_intent)
                    ):
                        item = QTableWidgetItem(str(value))
                        item.setToolTip(str(value))
                        self.references.setItem(index, column, item)
            except Exception as exc:
                self.status.setText(f"Không thể đọc tham chiếu: {exc}")
                QMessageBox.warning(self, "Project", self.status.text())
                self._refresh_controls()
                return False
        self._refresh_controls()
        return True

    def _reload_targets(self) -> bool:
        selected_target = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                targets = list_reference_targets(
                    db, str(self.target_type.currentData())
                )
            for target in targets:
                self.target.addItem(target.label, target.target_id)
                self.target.setItemData(
                    self.target.count() - 1, target.label, Qt.ItemDataRole.ToolTipRole
                )
            index = self.target.findData(selected_target)
            if index >= 0:
                self.target.setCurrentIndex(index)
        except Exception as exc:
            self.status.setText(f"Không thể đọc đối tượng tham chiếu: {exc}")
            QMessageBox.warning(self, "Project", self.status.text())
            self.target.blockSignals(False)
            self._refresh_controls()
            return False
        self.target.blockSignals(False)
        self._refresh_controls()
        return True

    def _refresh_controls(self) -> None:
        idle = self._worker is None
        project = self._projects.get(self.selected_project_id() or "")
        active = project is not None and project.status == "ACTIVE"
        for widget in (
            self.title,
            self.project,
            self.include_archived,
            self.target_type,
            self.target,
            self.usage,
            self.buttons,
        ):
            widget.setEnabled(idle)
        self.create_button.setEnabled(idle and bool(self.title.text().strip()))
        self.archive_button.setEnabled(idle and active)
        self.add_button.setEnabled(
            idle and active and self.target.currentData() is not None
        )

    def create_new_project(self) -> None:
        if self._worker is not None or not self.title.text().strip():
            return
        self._start_worker(ProjectWorker(self.root, "create", title=self.title.text()))

    def archive_selected_project(self) -> None:
        if self._worker is not None or not self.archive_button.isEnabled():
            return
        self._start_worker(
            ProjectWorker(
                self.root, "archive", project_id=self.selected_project_id() or ""
            )
        )

    def add_selected_reference(self) -> None:
        if self._worker is not None or not self.add_button.isEnabled():
            return
        self._start_worker(
            ProjectWorker(
                self.root,
                "reference",
                project_id=self.selected_project_id() or "",
                target_type=str(self.target_type.currentData()),
                target_id=str(self.target.currentData()),
                usage_intent=cast(UsageIntent, self.usage.currentData()),
            )
        )

    def _start_worker(self, worker: ProjectWorker) -> None:
        self._worker = worker
        self._result_project_id = None
        self._failure = None
        worker.completed.connect(self._store_result)
        worker.failed.connect(self._store_failure)
        worker.finished.connect(self._finish_worker)
        worker.finished.connect(worker.deleteLater)
        self.status.setText("Đang ghi Project…")
        self._refresh_controls()
        worker.start()

    def _store_result(self, project_id: str) -> None:
        self._result_project_id = project_id

    def _store_failure(self, message: str) -> None:
        self._failure = message

    def _finish_worker(self) -> None:
        worker = self._worker
        self._worker = None
        project_id, failure = self._result_project_id, self._failure
        if worker is not None and worker.action == "archive" and failure is None:
            self.include_archived.blockSignals(True)
            self.include_archived.setChecked(True)
            self.include_archived.blockSignals(False)
        if worker is not None and worker.action == "create" and failure is None:
            self.title.clear()
        refreshed = self.reload(project_id)
        if failure is not None:
            self.status.setText(failure)
            QMessageBox.warning(self, "Project", failure)
        elif refreshed:
            message = {
                "create": "Đã tạo Project.",
                "archive": "Đã lưu trữ Project; tham chiếu giữ nguyên trong lịch sử.",
                "reference": "Đã thêm tham chiếu đúng đối tượng được chọn.",
            }
            self.status.setText(
                message.get(
                    worker.action if worker is not None else "", "Đã ghi Project."
                )
            )
        self._result_project_id = None
        self._failure = None
        self._refresh_controls()

    def done(self, result: int) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Project kết thúc trước khi đóng.")
            return
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Project kết thúc trước khi đóng.")
            event.ignore()
            return
        super().closeEvent(event)
