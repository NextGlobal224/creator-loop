"""Choose exact Creator alternatives and reopen immutable decision history."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from creator_loop.database import _connect_write, open_readonly
from creator_loop.projects import list_projects
from creator_loop.selections import Selection, list_selections, select_draft


class SelectionWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        project_id: str,
        candidates: tuple[str, ...],
        selected: str,
        actor: str,
        reason: str,
    ) -> None:
        super().__init__()
        self.root, self.project_id = root, project_id
        self.candidates, self.selected = candidates, selected
        self.actor, self.reason = actor, reason

    def run(self) -> None:
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                decision = select_draft(
                    db,
                    project_id=self.project_id,
                    candidate_version_ids=self.candidates,
                    selected_draft_version_id=self.selected,
                    actor=self.actor,
                    reason=self.reason,
                )
            self.completed.emit(decision.selection_event_id)
        except Exception as exc:
            self.failed.emit(str(exc))


class SelectionDialog(QDialog):
    draft_requested = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: SelectionWorker | None = None
        self._versions: dict[str, tuple[str, bool]] = {}
        self._decisions: list[Selection] = []
        self._projects: dict[str, bool] = {}
        self._loaded = False
        self._historical = False
        self._result: str | None = None
        self._failure: str | None = None
        self.setWindowTitle("Selection — chọn phương án Creator")
        self.resize(1050, 780)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.project = QComboBox()
        top.addWidget(self.project, 1)
        self.new_button = QPushButton("Quyết định mới")
        top.addWidget(self.new_button)
        self.reload_button = QPushButton("Đọc lại")
        top.addWidget(self.reload_button)
        layout.addLayout(top)
        layout.addWidget(
            QLabel("Đánh dấu tập A/B/C, rồi chọn một exact Draft Version trong tập.")
        )
        self.candidates = QListWidget()
        self.candidates.setMaximumHeight(175)
        layout.addWidget(self.candidates)
        form = QFormLayout()
        self.selected = QComboBox()
        self.selected.setMinimumContentsLength(40)
        self.selected.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.actor, self.reason = QLineEdit("editor"), QLineEdit()
        form.addRow("Bản được chọn", self.selected)
        form.addRow("Người thực hiện", self.actor)
        form.addRow("Lý do (có thể để trống)", self.reason)
        layout.addLayout(form)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        self.save_button = QPushButton("Lưu quyết định")
        actions.addWidget(self.save_button)
        self.open_button = QPushButton("Đóng và mở Draft được chọn")
        actions.addWidget(self.open_button)
        layout.addLayout(actions)
        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(
            (
                "Thời điểm",
                "Bản được chọn",
                "Candidate set",
                "Người",
                "Lý do",
                "Event ID",
            )
        )
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.history.setMaximumHeight(170)
        layout.addWidget(self.history)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(
            QLabel(
                "Selection giữ lịch sử lựa chọn; không cấp Approval hoặc quyền xuất bản."
            )
        )
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.project.currentIndexChanged.connect(lambda: self._load_project())
        self.candidates.itemChanged.connect(self._sync_candidates)
        self.selected.currentIndexChanged.connect(self._show_preview)
        self.actor.textChanged.connect(self._refresh)
        self.history.itemSelectionChanged.connect(self._show_history)
        self.new_button.clicked.connect(self.new_decision)
        self.reload_button.clicked.connect(lambda: self.reload())
        self.save_button.clicked.connect(self.save)
        self.open_button.clicked.connect(self.open_draft)
        self.reload()

    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            str(self.candidates.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.candidates.count())
            if self.candidates.item(i).checkState() == Qt.CheckState.Checked
        )

    def reload(self, event_id: str | None = None) -> bool:
        if self._worker is not None:
            return False
        project_id = self.project.currentData()
        self._loaded = False
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                projects = list_projects(db, include_archived=True)
            self._projects = {p.project_id: p.status == "ACTIVE" for p in projects}
            self.project.blockSignals(True)
            self.project.clear()
            for project in projects:
                self.project.addItem(
                    f"{project.title} / {project.status} [{project.project_id}]",
                    project.project_id,
                )
            self.project.setCurrentIndex(max(self.project.findData(project_id), 0))
            self.project.blockSignals(False)
            return self._load_project(event_id)
        except Exception as exc:
            self.project.blockSignals(False)
            self.status.setText(f"Không thể đọc Selection: {exc}")
            QMessageBox.warning(self, "Selection", self.status.text())
            self._refresh()
            return False

    def new_decision(self) -> None:
        if self._worker is not None:
            return
        self._historical = False
        self.history.blockSignals(True)
        self.history.clearSelection()
        self.history.blockSignals(False)
        self.reason.clear()
        self.candidates.blockSignals(True)
        for i in range(self.candidates.count()):
            self.candidates.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.candidates.blockSignals(False)
        self.status.setText("Quyết định mới; chỉ chọn trong tập đã đánh dấu.")
        self._sync_candidates()

    def _sync_candidates(self) -> None:
        selected = self.selected.currentData()
        self.selected.blockSignals(True)
        self.selected.clear()
        for i in range(self.candidates.count()):
            item = self.candidates.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                self.selected.addItem(item.text(), item.data(Qt.ItemDataRole.UserRole))
        self.selected.setCurrentIndex(max(self.selected.findData(selected), 0))
        self.selected.blockSignals(False)
        self._show_preview()

    def _show_preview(self) -> None:
        version = self._versions.get(str(self.selected.currentData()))
        self.preview.setPlainText(version[0] if version else "")
        self._refresh()

    def _show_history(self) -> None:
        if self._worker is not None:
            return
        row = self.history.currentRow()
        if not self.history.selectedItems() or not (0 <= row < len(self._decisions)):
            return
        decision = self._decisions[row]
        self._historical = True
        self.actor.setText(decision.actor_id)
        self.reason.setText(decision.reason_text or "")
        self.candidates.blockSignals(True)
        for i in range(self.candidates.count()):
            item = self.candidates.item(i)
            item.setCheckState(
                Qt.CheckState.Checked
                if item.data(Qt.ItemDataRole.UserRole) in decision.candidate_version_ids
                else Qt.CheckState.Unchecked
            )
        self.candidates.blockSignals(False)
        self._sync_candidates()
        self.selected.setCurrentIndex(
            self.selected.findData(decision.selected_draft_version_id)
        )
        self.status.setText(
            f"Lịch sử bất biến: {decision.selection_event_id}; {len(decision.candidate_version_ids)} ứng viên; candidate set {decision.candidate_set_id}."
        )
        self._refresh()

    def _refresh(self) -> None:
        base = self._loaded and self._worker is None
        editing = (
            base
            and not self._historical
            and self._projects.get(str(self.project.currentData()), False)
        )
        self.project.setEnabled(base)
        self.history.setEnabled(base)
        self.new_button.setEnabled(
            base and self._projects.get(str(self.project.currentData()), False)
        )
        self.reload_button.setEnabled(self._worker is None)
        for widget in (self.candidates, self.selected, self.actor, self.reason):
            widget.setEnabled(editing)
        candidates = self.candidate_ids()
        selected = str(self.selected.currentData())
        valid = (
            bool(candidates)
            and selected in candidates
            and all(self._versions.get(v, ("", False))[1] for v in candidates)
        )
        self.save_button.setEnabled(
            editing and valid and bool(self.actor.text().strip())
        )
        self.open_button.setEnabled(base and selected in self._versions)
        self.buttons.setEnabled(self._worker is None)

    def save(self) -> None:
        if not self.save_button.isEnabled() or self._worker is not None:
            return
        self._result = self._failure = None
        self._worker = SelectionWorker(
            self.root,
            str(self.project.currentData()),
            self.candidate_ids(),
            str(self.selected.currentData()),
            self.actor.text(),
            self.reason.text(),
        )
        self._worker.completed.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finish)
        self.status.setText("Đang ghi quyết định…")
        self._refresh()
        self._worker.start()

    def _completed(self, event_id: str) -> None:
        self._result = event_id

    def _failed(self, error: str) -> None:
        self._failure = error

    def _finish(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if self._failure is not None:
            self.status.setText(f"Không thể lưu Selection: {self._failure}")
            QMessageBox.warning(self, "Selection", self.status.text())
            self._refresh()
        elif self._result is not None:
            if self.reload(self._result):
                self.status.setText(
                    f"Đã lưu quyết định {self._result}; chọn dòng lịch sử để xem tập/bản đã chọn."
                )
            else:
                self.status.setText(
                    f"Đã ghi event {self._result}, nhưng không đọc lại được. Đọc lại trước khi tiếp tục."
                )
        else:
            self._loaded = False
            self.status.setText(
                "Worker kết thúc mà chưa có kết quả; cần đọc lại dữ liệu."
            )
            self._refresh()

    def open_draft(self) -> None:
        if not self.open_button.isEnabled() or self._worker is not None:
            return
        version_id = str(self.selected.currentData())
        self.accept()
        self.draft_requested.emit(version_id)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self) -> None:
        if self._worker is None:
            super().reject()

    def done(self, result: int) -> None:
        if self._worker is None:
            super().done(result)

    def _load_project(self, event_id: str | None = None) -> bool:
        self._loaded = False
        self._versions.clear()
        self._decisions.clear()
        self.candidates.blockSignals(True)
        self.history.blockSignals(True)
        self.candidates.clear()
        self.history.setRowCount(0)
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                db.execute("BEGIN")
                versions = db.execute(
                    """SELECT v.draft_version_id,v.version_no,v.body_text,d.status,
                    v.version_no=(SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)
                    FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
                    JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
                    WHERE d.project_id=? ORDER BY d.rowid,v.version_no""",
                    (self.project.currentData(),),
                ).fetchall()
                self._decisions = list_selections(
                    db, project_id=str(self.project.currentData())
                )
            for version_id, number, body, status, latest in versions:
                active = status == "ACTIVE"
                self._versions[version_id] = (body, active)
                item = QListWidgetItem(
                    f"v{number} / {status}{' / bản cũ' if not latest else ''} — {body[:65]} [{version_id}]"
                )
                item.setData(Qt.ItemDataRole.UserRole, version_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if not active:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.candidates.addItem(item)
            self.history.setRowCount(len(self._decisions))
            for row, decision in enumerate(self._decisions):
                values = (
                    decision.created_at,
                    decision.selected_draft_version_id,
                    decision.candidate_set_id,
                    decision.actor_id,
                    decision.reason_text or "— chưa ghi lý do",
                    decision.selection_event_id,
                )
                for column, value in enumerate(values):
                    self.history.setItem(row, column, QTableWidgetItem(value))
            self._loaded = True
            self.candidates.blockSignals(False)
            self.history.blockSignals(False)
            self.new_decision()
            if event_id is not None:
                event_row = next(
                    (
                        i
                        for i, d in enumerate(self._decisions)
                        if d.selection_event_id == event_id
                    ),
                    None,
                )
                if event_row is None:
                    raise ValueError("Saved Selection event is unavailable")
                self.history.selectRow(event_row)
            return True
        except Exception as exc:
            self._loaded = False
            self.candidates.blockSignals(False)
            self.history.blockSignals(False)
            self.status.setText(f"Không thể đọc Selection: {exc}")
            QMessageBox.warning(self, "Selection", self.status.text())
            self._refresh()
            return False
