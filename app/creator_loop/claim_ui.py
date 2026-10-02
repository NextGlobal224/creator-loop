"""Create, correct and review exact Claim Versions without rewriting history."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import cast

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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from creator_loop.claim_review import (
    correct_claim,
    current_claim_review,
    record_claim_review,
)
from creator_loop.claims import (
    ClaimType,
    EvidenceLink,
    RelationType,
    claim_support_review,
    create_claim,
)
from creator_loop.database import _connect_write, open_readonly
from creator_loop.evidence_review import ReviewAction


class ClaimWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        action: str,
        *,
        version_id: str = "",
        statement: str = "",
        claim_type: ClaimType = "FACTUAL",
        links: tuple[EvidenceLink, ...] = (),
        actor: str = "",
        reason: str = "",
        review_action: ReviewAction = "ACCEPT",
    ) -> None:
        super().__init__()
        self.root, self.action, self.version_id = root, action, version_id
        self.statement, self.claim_type, self.links = statement, claim_type, links
        self.actor, self.reason, self.review_action = actor, reason, review_action

    def run(self) -> None:
        try:
            with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
                if self.action == "create":
                    saved = create_claim(
                        db,
                        claim_type=self.claim_type,
                        statement=self.statement,
                        actor=self.actor,
                        links=self.links,
                    )
                    version_id = saved.claim_version_id
                elif self.action == "correct":
                    saved = correct_claim(
                        db,
                        claim_version_id=self.version_id,
                        statement=self.statement,
                        actor=self.actor,
                        reason=self.reason,
                        links=self.links,
                    )
                    version_id = saved.claim_version_id
                elif self.action == "review":
                    record_claim_review(
                        db,
                        claim_version_id=self.version_id,
                        data_root=self.root,
                        action=self.review_action,
                        actor=self.actor,
                        reason=self.reason,
                    )
                    version_id = self.version_id
                else:
                    raise ValueError("Unknown Claim action")
            self.completed.emit(version_id)
        except Exception as exc:
            self.failed.emit(str(exc))


class ClaimDialog(QDialog):
    evidence_requested = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: ClaimWorker | None = None
        self._versions: dict[str, tuple[str, int, str, str, bool]] = {}
        self._evidence_labels: dict[str, str] = {}
        self._links: list[EvidenceLink] = []
        self._result_version: str | None = None
        self._failure: str | None = None
        self._loaded = False
        self.setWindowTitle("Knowledge — Claim Versions")
        self.resize(1000, 760)
        layout = QVBoxLayout(self)
        chooser = QHBoxLayout()
        self.version = QComboBox()
        self.version.setMinimumContentsLength(32)
        self.version.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        chooser.addWidget(self.version, 1)
        self.new_button = QPushButton("Claim mới")
        self.new_button.clicked.connect(lambda: self.version.setCurrentIndex(0))
        chooser.addWidget(self.new_button)
        self.reload_button = QPushButton("Đọc lại")
        self.reload_button.clicked.connect(lambda: self.reload())
        chooser.addWidget(self.reload_button)
        layout.addLayout(chooser)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        form = QFormLayout()
        self.claim_type = QComboBox()
        for kind in ("FACTUAL", "INTERPRETIVE", "EDITORIAL_HYPOTHESIS"):
            self.claim_type.addItem(kind, kind)
        form.addRow("Loại nhận định", self.claim_type)
        self.statement = QTextEdit()
        self.statement.setPlaceholderText(
            "Nhận định dựa trên Evidence; không chép nguyên nguồn thành kết luận."
        )
        self.statement.setMaximumHeight(100)
        form.addRow("Nhận định", self.statement)
        layout.addLayout(form)
        citation_row = QHBoxLayout()
        self.evidence = QComboBox()
        self.evidence.setMinimumContentsLength(30)
        self.evidence.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        citation_row.addWidget(self.evidence, 1)
        self.relation = QComboBox()
        for relation in ("SUPPORTS", "CONTRADICTS", "CONTEXT"):
            self.relation.addItem(relation, relation)
        citation_row.addWidget(self.relation)
        self.add_button = QPushButton("Thêm citation")
        self.add_button.clicked.connect(self.add_citation)
        citation_row.addWidget(self.add_button)
        layout.addLayout(citation_row)
        self.citations = QTableWidget(0, 3)
        self.citations.setHorizontalHeaderLabels(
            ("Evidence Version / nội dung", "Quan hệ", "ID chính xác")
        )
        self.citations.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.citations.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.citations.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.citations.setColumnWidth(0, 500)
        self.citations.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.citations)
        citation_actions = QHBoxLayout()
        self.remove_button = QPushButton("Bỏ citation khỏi bản mới")
        self.remove_button.clicked.connect(self.remove_citation)
        citation_actions.addWidget(self.remove_button)
        self.open_button = QPushButton("Đóng và mở Evidence trong Library")
        self.open_button.clicked.connect(self.open_citation)
        citation_actions.addWidget(self.open_button)
        layout.addLayout(citation_actions)
        identity = QFormLayout()
        self.actor = QLineEdit("editor")
        self.reason = QLineEdit()
        identity.addRow("Người thực hiện", self.actor)
        identity.addRow("Lý do sửa / review", self.reason)
        layout.addLayout(identity)
        decisions = QHBoxLayout()
        self.save_button = QPushButton("Tạo Claim Version")
        self.save_button.clicked.connect(self.save_version)
        decisions.addWidget(self.save_button)
        self.review_action = QComboBox()
        for label, action in (
            ("Chấp nhận", "ACCEPT"),
            ("Từ chối", "REJECT"),
            ("Yêu cầu sửa", "REQUEST_CHANGES"),
            ("Mở lại review", "REOPEN"),
        ):
            self.review_action.addItem(label, action)
        decisions.addWidget(self.review_action)
        self.review_button = QPushButton("Review phiên bản đã lưu")
        self.review_button.clicked.connect(self.review_version)
        decisions.addWidget(self.review_button)
        layout.addLayout(decisions)
        layout.addWidget(
            QLabel(
                "Review Claim không cấp quyền xuất bản. Citation stale vẫn cần đối chiếu lại nguồn."
            )
        )
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(
            ("Action", "Người", "Lý do", "Thời điểm")
        )
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.version.currentIndexChanged.connect(self._selected_version_changed)
        self.statement.textChanged.connect(self._refresh_controls)
        self.actor.textChanged.connect(self._refresh_controls)
        self.reason.textChanged.connect(self._refresh_controls)
        self.review_action.currentIndexChanged.connect(self._refresh_controls)
        self.citations.itemSelectionChanged.connect(self._refresh_controls)
        self.reload()

    def selected_version_id(self) -> str | None:
        value = self.version.currentData()
        return str(value) if value is not None else None

    def reload(self, version_id: str | None = None) -> bool:
        if self._worker is not None:
            return False
        selected = version_id or self.selected_version_id()
        self._loaded = False
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                versions = db.execute("""SELECT v.claim_version_id,v.claim_id,v.version_no,v.statement,c.claim_type,
                    c.deleted_at IS NULL AND v.version_no=(SELECT MAX(version_no) FROM claim_versions WHERE claim_id=v.claim_id)
                    FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
                    JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
                    ORDER BY c.created_at DESC,c.claim_id,v.version_no DESC""").fetchall()
                evidence = db.execute("""SELECT v.evidence_version_id,v.version_no,v.content,e.deleted_at,a.deleted_at,
                    v.version_no=(SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id),
                    COALESCE((SELECT action FROM review_events WHERE evidence_version_id=v.evidence_version_id ORDER BY rowid DESC LIMIT 1),'PENDING')
                    FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id JOIN assets a ON a.asset_id=e.asset_id
                    ORDER BY e.created_at DESC,e.evidence_id,v.version_no DESC""").fetchall()
            self._versions = {
                row[0]: (row[1], row[2], row[3], row[4], bool(row[5]))
                for row in versions
            }
            self.version.blockSignals(True)
            self.version.clear()
            self.version.addItem("Claim mới", None)
            for row in versions:
                label = f"v{row[2]} — {row[3]} [{row[0]}]"
                self.version.addItem(label, row[0])
                self.version.setItemData(
                    self.version.count() - 1, label, Qt.ItemDataRole.ToolTipRole
                )
            index = self.version.findData(selected)
            self.version.setCurrentIndex(max(index, 0))
            self.version.blockSignals(False)
            self.evidence.clear()
            self._evidence_labels = {}
            for row in evidence:
                label = f"v{row[1]} {'hiện hành' if row[5] else 'STALE'} / {row[6]} — {row[2]} [{row[0]}]"
                if row[3] is not None or row[4] is not None:
                    label += " / đã xóa mềm"
                else:
                    self.evidence.addItem(label, row[0])
                    self.evidence.setItemData(
                        self.evidence.count() - 1, label, Qt.ItemDataRole.ToolTipRole
                    )
                self._evidence_labels[row[0]] = label
            return self._selected_version_changed()
        except Exception as exc:
            self.version.blockSignals(False)
            self._versions.clear()
            self.version.clear()
            self.status.setText(f"Không thể đọc Claim: {exc}")
            self.save_button.setEnabled(False)
            self.review_button.setEnabled(False)
            QMessageBox.warning(self, "Claim", self.status.text())
            return False

    def _selected_version_changed(self) -> bool:
        self._loaded = False
        self._links = []
        self.history.setRowCount(0)
        version_id = self.selected_version_id()
        self.reason.clear()
        try:
            if version_id is None:
                self.statement.clear()
                self.summary.setText(
                    "Tạo Claim mới và chọn ít nhất một exact Evidence Version."
                )
            else:
                info = self._versions[version_id]
                self.statement.setPlainText(info[2])
                self.claim_type.setCurrentIndex(self.claim_type.findData(info[3]))
                with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                    self._links = [
                        EvidenceLink(row[0], row[1])
                        for row in db.execute(
                            "SELECT evidence_version_id,relation_type FROM claim_evidence WHERE claim_version_id=? ORDER BY evidence_version_id,relation_type",
                            (version_id,),
                        )
                    ]
                    support = claim_support_review(db, version_id)
                    state = current_claim_review(db, version_id)
                    events = db.execute(
                        "SELECT action,actor_id,COALESCE(reason,''),created_at FROM review_events WHERE claim_version_id=? ORDER BY rowid",
                        (version_id,),
                    ).fetchall()
                self.summary.setText(
                    f"Claim {info[0]} / v{info[1]} / {state}; SUPPORTS {support.support_links}, đã duyệt hiện hành {support.accepted_current_supports}, STALE {support.stale_links}. "
                    + (
                        "Sửa tạo version mới."
                        if info[4]
                        else "Bản lịch sử/đã xóa: chỉ đọc."
                    )
                )
                self.history.setRowCount(len(events))
                for index, event in enumerate(events):
                    for column, value in enumerate(event):
                        self.history.setItem(
                            index, column, QTableWidgetItem(str(value))
                        )
            self._loaded = True
            self._render_citations()
            self.status.setText("Đã đọc phiên bản và lịch sử.")
            return True
        except Exception as exc:
            self.status.setText(f"Không thể đọc phiên bản: {exc}")
            self._links = []
            self._render_citations()
            QMessageBox.warning(self, "Claim", self.status.text())
            return False
        finally:
            self._refresh_controls()

    def _editable(self) -> bool:
        version_id = self.selected_version_id()
        return self._loaded and (
            version_id is None
            or self._versions.get(version_id, ("", 0, "", "", False))[4]
        )

    def _render_citations(self) -> None:
        self.citations.setRowCount(len(self._links))
        for index, link in enumerate(self._links):
            for column, value in enumerate(
                (
                    self._evidence_labels.get(
                        link.evidence_version_id, "Evidence không còn khả dụng"
                    ),
                    link.relation_type,
                    link.evidence_version_id,
                )
            ):
                self.citations.setItem(index, column, QTableWidgetItem(value))
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        idle = self._worker is None
        editable = idle and self._editable()
        selected = self.selected_version_id() is not None
        for widget in (
            self.version,
            self.new_button,
            self.reload_button,
            self.actor,
            self.reason,
            self.review_action,
            self.buttons,
        ):
            widget.setEnabled(idle)
        self.statement.setReadOnly(not editable)
        self.claim_type.setEnabled(editable and not selected)
        self.evidence.setEnabled(editable)
        self.relation.setEnabled(editable)
        self.add_button.setEnabled(editable and self.evidence.currentData() is not None)
        self.remove_button.setEnabled(editable and self.citations.currentRow() >= 0)
        self.open_button.setEnabled(idle and self.citations.currentRow() >= 0)
        self.save_button.setText(
            "Sửa → Version mới + CORRECT" if selected else "Tạo Claim Version"
        )
        self.save_button.setEnabled(
            editable
            and bool(self.actor.text().strip())
            and bool(self.statement.toPlainText().strip())
            and bool(self._links)
            and (not selected or bool(self.reason.text().strip()))
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

    def add_citation(self) -> None:
        if not self.add_button.isEnabled():
            return
        link = EvidenceLink(
            str(self.evidence.currentData()),
            cast(RelationType, self.relation.currentData()),
        )
        if link not in self._links:
            self._links.append(link)
            self._render_citations()

    def remove_citation(self) -> None:
        if self.remove_button.isEnabled():
            del self._links[self.citations.currentRow()]
            self._render_citations()

    def open_citation(self) -> None:
        if self.open_button.isEnabled():
            version_id = self._links[self.citations.currentRow()].evidence_version_id
            self.accept()
            self.evidence_requested.emit(version_id)

    def save_version(self) -> None:
        if not self.save_button.isEnabled():
            return
        self._start_worker(
            ClaimWorker(
                self.root,
                "correct" if self.selected_version_id() else "create",
                version_id=self.selected_version_id() or "",
                statement=self.statement.toPlainText(),
                claim_type=cast(ClaimType, self.claim_type.currentData()),
                links=tuple(self._links),
                actor=self.actor.text(),
                reason=self.reason.text(),
            )
        )

    def review_version(self) -> None:
        if not self.review_button.isEnabled():
            return
        self._start_worker(
            ClaimWorker(
                self.root,
                "review",
                version_id=self.selected_version_id() or "",
                actor=self.actor.text(),
                reason=self.reason.text(),
                review_action=cast(ReviewAction, self.review_action.currentData()),
            )
        )

    def _start_worker(self, worker: ClaimWorker) -> None:
        self._worker = worker
        self._result_version = None
        self._failure = None
        worker.completed.connect(self._store_result)
        worker.failed.connect(self._store_failure)
        worker.finished.connect(self._finish_worker)
        worker.finished.connect(worker.deleteLater)
        self.status.setText("Đang kiểm tra nguồn / ghi Claim…")
        self._refresh_controls()
        worker.start()

    def _store_result(self, version_id: str) -> None:
        self._result_version = version_id

    def _store_failure(self, message: str) -> None:
        self._failure = message

    def _finish_worker(self) -> None:
        self._worker = None
        if self._failure is not None:
            self.status.setText(self._failure)
            QMessageBox.warning(self, "Claim", self._failure)
        elif self.reload(self._result_version):
            self.status.setText("Đã ghi Claim và đọc lại lịch sử.")
        self._result_version = None
        self._failure = None
        self._refresh_controls()

    def done(self, result: int) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Claim kết thúc trước khi đóng.")
            return
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác Claim kết thúc trước khi đóng.")
            event.ignore()
            return
        super().closeEvent(event)
