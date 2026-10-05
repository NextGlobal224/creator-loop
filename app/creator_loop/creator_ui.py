"""Author Draft alternatives, exact assertions, parent snapshots and review."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import TypedDict, cast

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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from creator_loop.claims import claim_support_review
from creator_loop.database import _connect_write, open_readonly
from creator_loop.draft_review import (
    correct_draft,
    current_draft_review,
    record_draft_review,
)
from creator_loop.drafts import (
    AssertionState,
    DraftAssertion,
    DraftClaimLink,
    UseType,
    combine_drafts,
    create_draft,
)
from creator_loop.evidence_review import ReviewAction
from creator_loop.projects import list_projects


class _DraftPayload(TypedDict):
    body_text: str
    format: str
    actor: str
    claims: tuple[DraftClaimLink, ...]
    assertions: tuple[DraftAssertion, ...]


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class CreatorWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        action: str,
        *,
        project_id: str,
        version_id: str = "",
        body_text: str = "",
        format: str = "POST",
        actor: str = "",
        reason: str = "",
        claims: tuple[DraftClaimLink, ...] = (),
        assertions: tuple[DraftAssertion, ...] = (),
        parents: tuple[str, ...] = (),
        review_action: ReviewAction = "ACCEPT",
    ) -> None:
        super().__init__()
        self.root, self.action = root, action
        self.project_id, self.version_id = project_id, version_id
        self.body_text, self.format, self.actor, self.reason = (
            body_text,
            format,
            actor,
            reason,
        )
        self.claims, self.assertions, self.parents = claims, assertions, parents
        self.review_action = review_action

    def run(self) -> None:
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                if self.action == "review":
                    record_draft_review(
                        db,
                        draft_version_id=self.version_id,
                        data_root=self.root,
                        action=self.review_action,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    result_id = self.version_id
                else:
                    values = _DraftPayload(
                        body_text=self.body_text,
                        format=self.format,
                        actor=self.actor,
                        claims=self.claims,
                        assertions=self.assertions,
                    )
                    if self.action == "create":
                        saved = create_draft(db, project_id=self.project_id, **values)
                    elif self.action == "correct":
                        saved = correct_draft(
                            db,
                            draft_version_id=self.version_id,
                            reason=self.reason,
                            **values,
                        )
                    elif self.action == "combine":
                        saved = combine_drafts(
                            db,
                            project_id=self.project_id,
                            parent_version_ids=self.parents,
                            **values,
                        )
                    else:
                        raise ValueError("Unknown Creator action")
                    result_id = saved.draft_version_id
            self.completed.emit(result_id)
        except Exception as exc:
            self.failed.emit(str(exc))


class CreatorDialog(QDialog):
    claim_requested = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: CreatorWorker | None = None
        self._versions: dict[str, tuple[str, int, str, str, bool]] = {}
        self._projects: dict[str, bool] = {}
        self._claim_labels: dict[str, str] = {}
        self._links: list[DraftClaimLink] = []
        self._assertions: list[tuple[DraftAssertion, str]] = []
        self._loaded = False
        self._result: str | None = None
        self._failure: str | None = None
        self._raw_body: str | None = None
        self._rendered_body = ""
        self.setWindowTitle("Creator — Draft Versions")
        self.resize(1050, 760)
        layout = QVBoxLayout(self)
        chooser = QHBoxLayout()
        self.project = QComboBox()
        chooser.addWidget(self.project, 1)
        self.version = QComboBox()
        self.version.setMinimumContentsLength(30)
        self.version.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        chooser.addWidget(self.version, 2)
        self.new_button = QPushButton("Phương án mới")
        self.new_button.clicked.connect(lambda: self.version.setCurrentIndex(0))
        chooser.addWidget(self.new_button)
        self.reload_button = QPushButton("Đọc lại")
        self.reload_button.clicked.connect(lambda: self.reload())
        chooser.addWidget(self.reload_button)
        layout.addLayout(chooser)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        self.body = QTextEdit()
        self.body.setPlaceholderText(
            "Viết phương án và đánh dấu các nhận định cần nguồn trên đúng body này."
        )
        content_layout.addWidget(self.body)
        form = QFormLayout()
        self.format = QLineEdit("POST")
        self.actor = QLineEdit("editor")
        self.reason = QLineEdit()
        form.addRow("Format", self.format)
        form.addRow("Người thực hiện", self.actor)
        form.addRow("Lý do sửa / review", self.reason)
        content_layout.addLayout(form)
        self.tabs.addTab(content, "Nội dung")

        citations = QWidget()
        citations_layout = QVBoxLayout(citations)
        citation_row = QHBoxLayout()
        self.claim = QComboBox()
        self.claim.setMinimumContentsLength(30)
        self.claim.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        citation_row.addWidget(self.claim, 1)
        self.use_type = QComboBox()
        for use in ("ASSERTED", "INSPIRATION", "QUOTE", "BACKGROUND"):
            self.use_type.addItem(use, use)
        citation_row.addWidget(self.use_type)
        self.add_claim_button = QPushButton("Thêm Claim Version")
        self.add_claim_button.clicked.connect(self.add_claim)
        citation_row.addWidget(self.add_claim_button)
        citations_layout.addLayout(citation_row)
        self.citations = _table(("Exact Claim Version", "Use type", "ID"))
        self.citations.setColumnWidth(0, 560)
        citations_layout.addWidget(self.citations)
        buttons = QHBoxLayout()
        self.remove_claim_button = QPushButton("Bỏ Claim khỏi bản mới")
        self.remove_claim_button.clicked.connect(self.remove_claim)
        buttons.addWidget(self.remove_claim_button)
        self.open_claim_button = QPushButton("Đóng và mở Claim")
        self.open_claim_button.clicked.connect(self.open_claim)
        buttons.addWidget(self.open_claim_button)
        citations_layout.addLayout(buttons)
        assertion_row = QHBoxLayout()
        self.start, self.end = QSpinBox(), QSpinBox()
        assertion_row.addWidget(QLabel("Offset codepoint [start, end)"))
        assertion_row.addWidget(self.start)
        assertion_row.addWidget(self.end)
        self.assertion_claim = QComboBox()
        assertion_row.addWidget(self.assertion_claim, 1)
        self.assertion_state = QComboBox()
        for state in ("NEEDS_SOURCE", "UNREVIEWED", "SUPPORTED", "EDITORIAL"):
            self.assertion_state.addItem(state, state)
        assertion_row.addWidget(self.assertion_state)
        self.add_assertion_button = QPushButton("Đánh dấu đoạn")
        self.add_assertion_button.clicked.connect(self.add_assertion)
        assertion_row.addWidget(self.add_assertion_button)
        citations_layout.addLayout(assertion_row)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        citations_layout.addWidget(self.preview)
        self.assertions = _table(
            ("Start", "End", "Đoạn đã đánh dấu", "Claim Version", "State")
        )
        citations_layout.addWidget(self.assertions)
        self.remove_assertion_button = QPushButton("Bỏ đoạn khỏi bản mới")
        self.remove_assertion_button.clicked.connect(self.remove_assertion)
        citations_layout.addWidget(self.remove_assertion_button)
        self.tabs.addTab(citations, "Claim / assertion")

        lineage = QWidget()
        lineage_layout = QVBoxLayout(lineage)
        self.parents = _table(("Parent Version", "Quan hệ"))
        lineage_layout.addWidget(QLabel("Parents của snapshot đã lưu"))
        lineage_layout.addWidget(self.parents)
        lineage_layout.addWidget(
            QLabel(
                "Chọn ít nhất hai phiên bản cùng Project, viết nội dung kết hợp rồi lưu A+B."
            )
        )
        self.candidates = QListWidget()
        lineage_layout.addWidget(self.candidates)
        self.tabs.addTab(lineage, "Parents A+B")
        decisions = QHBoxLayout()
        self.save_button = QPushButton("Tạo phương án")
        self.save_button.clicked.connect(self.save_version)
        decisions.addWidget(self.save_button)
        self.combine_button = QPushButton("Lưu phương án kết hợp A+B")
        self.combine_button.clicked.connect(self.combine_versions)
        decisions.addWidget(self.combine_button)
        self.review_action = QComboBox()
        for label, action in (
            ("Chấp nhận", "ACCEPT"),
            ("Từ chối", "REJECT"),
            ("Yêu cầu sửa", "REQUEST_CHANGES"),
            ("Mở lại review", "REOPEN"),
        ):
            self.review_action.addItem(label, action)
        decisions.addWidget(self.review_action)
        self.review_button = QPushButton("Review snapshot đã lưu")
        self.review_button.clicked.connect(self.review_version)
        decisions.addWidget(self.review_button)
        layout.addLayout(decisions)
        self.history = _table(("Action", "Người", "Lý do", "Thời điểm"))
        self.history.setMaximumHeight(140)
        layout.addWidget(self.history)
        layout.addWidget(
            QLabel("Review không xóa NEEDS_SOURCE/stale hoặc cấp quyền xuất bản.")
        )
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.project.currentIndexChanged.connect(lambda: self._load_project())
        self.version.currentIndexChanged.connect(self._load_version)
        self.body.textChanged.connect(self._body_changed)
        for field in (self.format, self.actor, self.reason):
            field.textChanged.connect(self._refresh_controls)
        self.review_action.currentIndexChanged.connect(self._refresh_controls)
        self.assertion_claim.currentIndexChanged.connect(self._refresh_controls)
        self.assertion_state.currentIndexChanged.connect(self._refresh_controls)
        self.start.valueChanged.connect(self._range_changed)
        self.end.valueChanged.connect(self._range_changed)
        self.citations.itemSelectionChanged.connect(self._refresh_controls)
        self.assertions.itemSelectionChanged.connect(self._refresh_controls)
        self.candidates.itemChanged.connect(self._refresh_controls)
        self.reload()

    def selected_version_id(self) -> str | None:
        value = self.version.currentData()
        return str(value) if value is not None else None

    def reload(self, version_id: str | None = None) -> bool:
        if self._worker is not None:
            return False
        project_id = self.project.currentData()
        selected = version_id or self.selected_version_id()
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
            index = self.project.findData(project_id)
            self.project.setCurrentIndex(max(index, 0))
            self.project.blockSignals(False)
            return self._load_project(selected)
        except Exception as exc:
            self.project.blockSignals(False)
            self._projects.clear()
            self.status.setText(f"Không thể đọc Creator: {exc}")
            QMessageBox.warning(self, "Creator", self.status.text())
            self._refresh_controls()
            return False

    def _load_project(self, version_id: str | None = None) -> bool:
        self._loaded = False
        self._versions.clear()
        self.version.blockSignals(True)
        self.version.clear()
        self.version.addItem("Phương án mới", None)
        self.candidates.clear()
        self.claim.clear()
        self._claim_labels.clear()
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                versions = db.execute(
                    """SELECT v.draft_version_id,v.draft_id,v.version_no,v.body_text,v.format,
                    d.status='ACTIVE' AND v.version_no=(SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)
                    FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
                    WHERE d.project_id=? ORDER BY d.created_at DESC,d.draft_id,v.version_no DESC""",
                    (self.project.currentData(),),
                ).fetchall()
                claims = db.execute("""SELECT v.claim_version_id,v.version_no,v.statement,c.deleted_at,
                    v.version_no=(SELECT MAX(version_no) FROM claim_versions WHERE claim_id=v.claim_id),
                    COALESCE((SELECT action FROM review_events WHERE claim_version_id=v.claim_version_id ORDER BY rowid DESC LIMIT 1),'PENDING')
                    FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
                    ORDER BY c.created_at DESC,c.claim_id,v.version_no DESC""").fetchall()
                for row in claims:
                    support = claim_support_review(db, row[0])
                    label = f"v{row[1]} / {row[5]} / stale {support.stale_links} — {row[2][:160]} [{row[0]}]"
                    if not row[4]:
                        label += " / bản cũ"
                    if row[3] is not None:
                        label += " / đã xóa mềm"
                    else:
                        self.claim.addItem(label, row[0])
                    self._claim_labels[row[0]] = label
            for row in versions:
                self._versions[row[0]] = (row[1], row[2], row[3], row[4], bool(row[5]))
                label = f"v{row[2]} — {row[3][:120]} [{row[0]}]"
                self.version.addItem(label, row[0])
                self.version.setItemData(
                    self.version.count() - 1, label, Qt.ItemDataRole.ToolTipRole
                )
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, row[0])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.candidates.addItem(item)
            index = self.version.findData(version_id)
            if version_id is not None and index < 0:
                raise ValueError(
                    "Draft Version đã ghi không còn hiển thị trong Project"
                )
            self.version.setCurrentIndex(max(index, 0))
            self.version.blockSignals(False)
            return self._load_version()
        except Exception as exc:
            self.version.blockSignals(False)
            self.status.setText(f"Không thể đọc Project/Draft: {exc}")
            QMessageBox.warning(self, "Creator", self.status.text())
            self._refresh_controls()
            return False

    def _load_version(self) -> bool:
        self._loaded = False
        self._links = []
        self._assertions = []
        self.parents.setRowCount(0)
        self.history.setRowCount(0)
        self.reason.clear()
        version_id = self.selected_version_id()
        try:
            if version_id is None:
                self._set_body(None)
                self.format.setText("POST")
                self.summary.setText(
                    "Tạo phương án mới; Claim links và assertion flags luôn được lưu theo Version."
                )
            else:
                info = self._versions[version_id]
                self._set_body(info[2])
                self.format.setText(info[3])
                with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                    self._links = [
                        DraftClaimLink(row[0], row[1])
                        for row in db.execute(
                            "SELECT claim_version_id,use_type FROM draft_claims WHERE draft_version_id=? ORDER BY claim_version_id",
                            (version_id,),
                        )
                    ]
                    self._assertions = [
                        (DraftAssertion(row[0], row[1], row[3], row[4]), row[2])
                        for row in db.execute(
                            "SELECT text_start,text_end,asserted_text,claim_version_id,review_state FROM draft_assertions WHERE draft_version_id=? ORDER BY text_start,text_end,assertion_id",
                            (version_id,),
                        )
                    ]
                    parents = db.execute(
                        "SELECT parent_draft_version_id,relation_type FROM draft_version_parents WHERE child_draft_version_id=? ORDER BY parent_draft_version_id",
                        (version_id,),
                    ).fetchall()
                    history = db.execute(
                        "SELECT action,actor_id,COALESCE(reason,''),created_at FROM review_events WHERE draft_version_id=? ORDER BY rowid",
                        (version_id,),
                    ).fetchall()
                    state = current_draft_review(db, version_id)
                needs_source = sum(
                    a.review_state == "NEEDS_SOURCE" for a, _ in self._assertions
                )
                self.summary.setText(
                    f"Draft {info[0]} / v{info[1]} / {state}; NEEDS_SOURCE {needs_source}. "
                    + (
                        "Sửa tạo snapshot mới."
                        if self._project_active() and info[4]
                        else "Snapshot lịch sử/archived: chỉ đọc."
                    )
                )
                self._fill_table(self.parents, parents)
                self._fill_table(self.history, history)
            self._loaded = True
            self._render_claims()
            self._body_changed()
            self.status.setText("Đã đọc Draft và lịch sử.")
            return True
        except Exception as exc:
            self.status.setText(f"Không thể đọc snapshot: {exc}")
            QMessageBox.warning(self, "Creator", self.status.text())
            self._refresh_controls()
            return False

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list[tuple[object, ...]]) -> None:
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate(row):
                table.setItem(index, column, QTableWidgetItem(str(value)))

    def _render_claims(self) -> None:
        self._fill_table(
            self.citations,
            [
                (
                    self._claim_labels.get(
                        link.claim_version_id, "Claim không khả dụng"
                    ),
                    link.use_type,
                    link.claim_version_id,
                )
                for link in self._links
            ],
        )
        selected = self.assertion_claim.currentData()
        self.assertion_claim.clear()
        self.assertion_claim.addItem("Không có Claim", None)
        for link in self._links:
            self.assertion_claim.addItem(
                self._claim_labels.get(link.claim_version_id, link.claim_version_id),
                link.claim_version_id,
            )
        self.assertion_claim.setCurrentIndex(
            max(self.assertion_claim.findData(selected), 0)
        )
        self._refresh_controls()

    def _set_body(self, body: str | None) -> None:
        self.body.blockSignals(True)
        self.body.setPlainText(body or "")
        self._raw_body = body
        self._rendered_body = self.body.toPlainText()
        self.body.blockSignals(False)

    def current_body(self) -> str:
        # Qt may normalize CRLF/NBSP when rendering plain text. Merely reading
        # an existing snapshot must preserve its exact stored offsets/bytes.
        rendered = self.body.toPlainText()
        if self._raw_body is not None and rendered == self._rendered_body:
            return self._raw_body
        return rendered

    def _body_changed(self) -> None:
        length = len(self.current_body())
        self.start.setMaximum(max(length - 1, 0))
        self.end.setMaximum(length)
        self._render_assertions()
        self._range_changed()

    def _span_matches(self, assertion: DraftAssertion, excerpt: str) -> bool:
        body = self.current_body()
        return (
            0 <= assertion.start < assertion.end <= len(body)
            and body[assertion.start : assertion.end] == excerpt
        )

    def _render_assertions(self) -> None:
        self._fill_table(
            self.assertions,
            [
                (
                    a.start,
                    a.end,
                    text
                    if self._span_matches(a, text)
                    else f"CẦN ĐÁNH DẤU LẠI: {text}",
                    a.claim_version_id or "",
                    a.review_state,
                )
                for a, text in self._assertions
            ],
        )
        self._refresh_controls()

    def _range_changed(self) -> None:
        self.preview.setText(
            self.current_body()[self.start.value() : self.end.value()]
            if self.start.value() < self.end.value()
            else "Chọn một đoạn hợp lệ trên body hiện tại."
        )
        self._refresh_controls()

    def _project_active(self) -> bool:
        return self._projects.get(str(self.project.currentData()), False)

    def _editable(self) -> bool:
        version_id = self.selected_version_id()
        return (
            self._loaded
            and self._project_active()
            and (
                version_id is None
                or self._versions.get(version_id, ("", 0, "", "", False))[4]
            )
        )

    def selected_parent_ids(self) -> tuple[str, ...]:
        return tuple(
            str(self.candidates.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.candidates.count())
            if self.candidates.item(index).checkState() == Qt.CheckState.Checked
        )

    def _refresh_controls(self) -> None:
        idle = self._worker is None
        editable = idle and self._editable()
        selected = self.selected_version_id() is not None
        widget: QWidget
        for widget in (
            self.project,
            self.version,
            self.new_button,
            self.reload_button,
            self.actor,
            self.reason,
            self.review_action,
            self.buttons,
        ):
            widget.setEnabled(idle)
        self.body.setReadOnly(not editable)
        for widget in (
            self.format,
            self.claim,
            self.use_type,
            self.start,
            self.end,
            self.assertion_claim,
            self.assertion_state,
            self.candidates,
        ):
            widget.setEnabled(editable)
        self.add_claim_button.setEnabled(
            editable and self.claim.currentData() is not None
        )
        self.remove_claim_button.setEnabled(
            editable and self.citations.currentRow() >= 0
        )
        self.open_claim_button.setEnabled(idle and self.citations.currentRow() >= 0)
        self.add_assertion_button.setEnabled(
            editable
            and self.start.value() < self.end.value()
            and (
                self.assertion_state.currentData() != "SUPPORTED"
                or self.assertion_claim.currentData() is not None
            )
        )
        self.remove_assertion_button.setEnabled(
            editable and self.assertions.currentRow() >= 0
        )
        complete = (
            editable
            and bool(self.current_body().strip())
            and bool(self.format.text().strip())
            and bool(self.actor.text().strip())
            and all(self._span_matches(a, text) for a, text in self._assertions)
        )
        self.save_button.setText(
            "Sửa → Version mới + CORRECT" if selected else "Tạo phương án"
        )
        self.save_button.setEnabled(
            complete and (not selected or bool(self.reason.text().strip()))
        )
        self.combine_button.setEnabled(
            complete and len(self.selected_parent_ids()) >= 2
        )
        self.review_button.setEnabled(
            editable
            and selected
            and bool(self.actor.text().strip())
            and (
                self.review_action.currentData() == "ACCEPT"
                or bool(self.reason.text().strip())
            )
        )

    def add_claim(self) -> None:
        if not self.add_claim_button.isEnabled():
            return
        claim_id = str(self.claim.currentData())
        if all(link.claim_version_id != claim_id for link in self._links):
            self._links.append(
                DraftClaimLink(claim_id, cast(UseType, self.use_type.currentData()))
            )
            self._render_claims()

    def remove_claim(self) -> None:
        if not self.remove_claim_button.isEnabled():
            return
        link = self._links.pop(self.citations.currentRow())
        self._assertions = [
            (DraftAssertion(a.start, a.end, None, "NEEDS_SOURCE"), text)
            if a.claim_version_id == link.claim_version_id
            else (a, text)
            for a, text in self._assertions
        ]
        self._render_claims()
        self._render_assertions()

    def open_claim(self) -> None:
        if self.open_claim_button.isEnabled():
            version_id = self._links[self.citations.currentRow()].claim_version_id
            self.accept()
            self.claim_requested.emit(version_id)

    def add_assertion(self) -> None:
        if not self.add_assertion_button.isEnabled():
            return
        claim_id = self.assertion_claim.currentData()
        assertion = DraftAssertion(
            self.start.value(),
            self.end.value(),
            str(claim_id) if claim_id is not None else None,
            cast(AssertionState, self.assertion_state.currentData()),
        )
        self._assertions.append(
            (assertion, self.current_body()[assertion.start : assertion.end])
        )
        self._render_assertions()

    def remove_assertion(self) -> None:
        if self.remove_assertion_button.isEnabled():
            del self._assertions[self.assertions.currentRow()]
            self._render_assertions()

    def _write_snapshot(self, action: str) -> None:
        self._start_worker(
            CreatorWorker(
                self.root,
                action,
                project_id=str(self.project.currentData()),
                version_id=self.selected_version_id() or "",
                body_text=self.current_body(),
                format=self.format.text(),
                actor=self.actor.text(),
                reason=self.reason.text(),
                claims=tuple(self._links),
                assertions=tuple(a for a, _ in self._assertions),
                parents=self.selected_parent_ids(),
            )
        )

    def save_version(self) -> None:
        if self.save_button.isEnabled():
            self._write_snapshot("correct" if self.selected_version_id() else "create")

    def combine_versions(self) -> None:
        if self.combine_button.isEnabled():
            self._write_snapshot("combine")

    def review_version(self) -> None:
        if self.review_button.isEnabled():
            self._start_worker(
                CreatorWorker(
                    self.root,
                    "review",
                    project_id=str(self.project.currentData()),
                    version_id=self.selected_version_id() or "",
                    actor=self.actor.text(),
                    reason=self.reason.text(),
                    review_action=cast(ReviewAction, self.review_action.currentData()),
                )
            )

    def _start_worker(self, worker: CreatorWorker) -> None:
        self._worker = worker
        self._result = None
        self._failure = None
        worker.completed.connect(self._store_result)
        worker.failed.connect(self._store_failure)
        worker.finished.connect(self._finish_worker)
        worker.finished.connect(worker.deleteLater)
        self.status.setText("Đang kiểm nguồn / ghi snapshot…")
        self._refresh_controls()
        worker.start()

    def _store_result(self, version_id: str) -> None:
        self._result = version_id

    def _store_failure(self, message: str) -> None:
        self._failure = message

    def _finish_worker(self) -> None:
        self._worker = None
        if self._failure is not None:
            self.status.setText(self._failure)
            QMessageBox.warning(self, "Creator", self._failure)
        elif self.reload(self._result):
            self.status.setText("Đã ghi snapshot và đọc lại lịch sử.")
        self._result = None
        self._failure = None
        self._refresh_controls()

    def done(self, result: int) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Creator kết thúc trước khi đóng.")
            return
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Creator kết thúc trước khi đóng.")
            event.ignore()
            return
        super().closeEvent(event)
