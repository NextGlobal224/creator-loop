"""Human Package review and manual publication with exact immutable snapshots."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from creator_loop.claims import _timestamp
from creator_loop.database import open_readonly
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.projects import list_projects
from creator_loop.publication import (
    Approval,
    ApprovalDecision,
    PackageSnapshot,
    Post,
    list_approvals,
    list_posts,
    package_snapshot,
)
from creator_loop.publication_media import hold_registered_file
from creator_loop.whole_evidence import WholeSource, read_verified_whole_source
from creator_loop.whole_evidence_ui import WholeEvidenceView


@dataclass(frozen=True)
class PublicationTask:
    action: str
    package_id: str = ""
    fingerprint: str = ""
    project_id: str = ""
    draft_version_id: str = ""
    platform: str = ""
    format: str = ""
    items: tuple[PackageItem, ...] = ()
    actor: str = ""
    reason: str = ""
    post_id: str = ""
    external_id: str | None = None
    external_url: str | None = None
    published_at: str = ""
    file_id: str = ""
    digest: str = ""
    text_to_copy: str = ""


@dataclass(frozen=True)
class PackageMediaPreview:
    source: WholeSource
    handles: ExitStack


class PublicationWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, task: PublicationTask) -> None:
        super().__init__()
        self.root, self.task = root, task

    def run(self) -> None:
        try:
            task = self.task
            repo = PublicationRepository(self.root / "creator_loop.sqlite3")
            result: object
            if task.action == "CREATE":
                repo.create_package(
                    package_id=task.package_id,
                    project_id=task.project_id,
                    draft_version_id=task.draft_version_id,
                    platform=task.platform,
                    format=task.format,
                    created_at=_timestamp(),
                    items=list(task.items),
                    data_root=self.root,
                )
                result = task.package_id
            elif task.action == "CHECK":
                result = repo.publication_blockers(
                    package_id=task.package_id, data_root=self.root
                )
            elif task.action in ("APPROVED", "REJECTED", "REVOKED"):
                result = repo.record_approval(
                    package_id=task.package_id,
                    expected_fingerprint=task.fingerprint,
                    decision=cast(ApprovalDecision, task.action),
                    actor=task.actor,
                    reason=task.reason,
                    data_root=self.root,
                )
            elif task.action in ("PREPARE", "COPY_TEXT"):
                result = repo.prepare_manual_post(
                    post_id=task.post_id,
                    package_id=task.package_id,
                    expected_fingerprint=task.fingerprint,
                    data_root=self.root,
                )
                if (
                    task.action == "COPY_TEXT"
                    and cast(Post, result).status != "PENDING"
                ):
                    raise ValueError("Copy requires this Post to remain PENDING")
            elif task.action == "PUBLISHED":
                result = repo.record_manual_post(
                    post_id=task.post_id,
                    package_id=task.package_id,
                    expected_fingerprint=task.fingerprint,
                    external_post_id=task.external_id,
                    external_url=task.external_url,
                    published_at=task.published_at,
                    data_root=self.root,
                )
            elif task.action == "PREVIEW":
                with closing(open_readonly(repo.db_path)) as db, ExitStack() as handles:
                    db.execute("BEGIN")
                    hold_registered_file(
                        db,
                        file_id=task.file_id,
                        expected_digest=task.digest,
                        data_root=self.root,
                        handles=handles,
                    )
                    source = read_verified_whole_source(db, task.file_id, self.root)
                    result = PackageMediaPreview(source, handles.pop_all())
            else:
                raise ValueError("Unknown Publication operation")
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class PublicationDialog(QDialog):
    draft_requested = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._worker: PublicationWorker | None = None
        self._result: object | None = None
        self._failure: str | None = None
        self._loaded = False
        self._checked = False
        self._blockers: tuple[str, ...] = ()
        self._snapshot: PackageSnapshot | None = None
        self._approvals: tuple[Approval, ...] = ()
        self._posts: tuple[Post, ...] = ()
        self._projects: dict[str, bool] = {}
        self._drafts: dict[str, tuple[str, bool]] = {}
        self._files: dict[str, tuple[str, str]] = {}
        self.setWindowTitle("Package / Publication")
        self.resize(970, 800)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.project = QComboBox()
        self.packages = QComboBox()
        self.reload_button = QPushButton("Đọc lại")
        self.new_button = QPushButton("Package mới")
        for widget in (
            self.project,
            self.packages,
            self.reload_button,
            self.new_button,
        ):
            header.addWidget(widget)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        compose = QWidget()
        form = QFormLayout(compose)
        self.draft = QComboBox()
        self.caption = QTextEdit()
        self.caption.setReadOnly(True)
        self.platform = QLineEdit("FB")
        self.format = QLineEdit("POST")
        form.addRow("Draft Version", self.draft)
        form.addRow("Caption từ Draft đã chọn", self.caption)
        form.addRow("Nền tảng", self.platform)
        form.addRow("Định dạng", self.format)
        file_bar = QHBoxLayout()
        self.file = QComboBox()
        self.kind = QComboBox()
        self.kind.addItems(("MEDIA", "THUMBNAIL"))
        self.add_button = QPushButton("Thêm tệp")
        for widget in (self.file, self.kind, self.add_button):
            file_bar.addWidget(widget)
        form.addRow(file_bar)
        self.media = QListWidget()
        self.media.setMaximumHeight(100)
        form.addRow("Tệp và thứ tự", self.media)
        order = QHBoxLayout()
        self.up_button, self.down_button, self.remove_button = (
            QPushButton(t) for t in ("Lên", "Xuống", "Bỏ tệp")
        )
        for widget in (self.up_button, self.down_button, self.remove_button):
            order.addWidget(widget)
        form.addRow(order)
        self.cta, self.alt = QLineEdit(), QLineEdit()
        form.addRow("CTA (nếu có)", self.cta)
        form.addRow("ALT_TEXT (nếu có)", self.alt)
        self.create_button = QPushButton("Đóng Package mới")
        self.open_draft_button = QPushButton("Đóng và mở Draft để xem/sửa")
        form.addRow(self.create_button)
        form.addRow(self.open_draft_button)
        self.tabs.addTab(compose, "Package mới")
        review = QWidget()
        review_layout = QVBoxLayout(review)
        self.snapshot_label = QLabel("Chọn Package đã lưu để xem exact snapshot.")
        self.snapshot_label.setWordWrap(True)
        review_layout.addWidget(self.snapshot_label)
        self.items = QTableWidget(0, 4)
        self.items.setHorizontalHeaderLabels(
            ("Loại", "Thứ tự", "Nội dung / tệp", "SHA-256")
        )
        self.items.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.items.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.items.setMinimumHeight(90)
        self.items.setMaximumHeight(150)
        review_layout.addWidget(self.items, 1)
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setMinimumHeight(70)
        self.text_preview.setMaximumHeight(100)
        review_layout.addWidget(self.text_preview)
        self.preview_button = QPushButton("Xem tệp của item đã chọn")
        review_layout.addWidget(self.preview_button)
        self.copy_button = QPushButton("Sao chép nguyên văn text item cho Post PENDING")
        review_layout.addWidget(self.copy_button)
        self.check_button = QPushButton("Kiểm điều kiện hiện tại")
        self.eligibility = QTextEdit("Chưa kiểm; mọi bước đều kiểm lại trước khi ghi.")
        self.eligibility.setReadOnly(True)
        self.eligibility.setMaximumHeight(90)
        review_layout.addWidget(self.check_button)
        review_layout.addWidget(self.eligibility)
        decisions = QFormLayout()
        self.actor, self.reason = QLineEdit(), QLineEdit()
        decisions.addRow("Người duyệt", self.actor)
        decisions.addRow("Lý do (bắt buộc khi từ chối/rút duyệt)", self.reason)
        review_layout.addLayout(decisions)
        self.review_confirmed = QCheckBox(
            "Đã xem toàn bộ nội dung/media và cách diễn đạt giả thuyết editorial"
        )
        review_layout.addWidget(self.review_confirmed)
        actions = QHBoxLayout()
        self.approve_button, self.reject_button, self.revoke_button = (
            QPushButton(t) for t in ("APPROVED", "REJECTED", "REVOKED")
        )
        for button in (self.approve_button, self.reject_button, self.revoke_button):
            actions.addWidget(button)
        review_layout.addLayout(actions)
        self.history = QTableWidget(0, 3)
        self.history.setHorizontalHeaderLabels(
            ("Quyết định / thời điểm", "Người duyệt", "Lý do")
        )
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setMaximumHeight(110)
        review_layout.addWidget(self.history)
        review_scroll = QScrollArea()
        review_scroll.setWidgetResizable(True)
        review_scroll.setWidget(review)
        self.tabs.addTab(review_scroll, "Snapshot / Approval")
        post_tab = QWidget()
        post_layout = QVBoxLayout(post_tab)
        self.prepare_button = QPushButton("Chuẩn bị Post thủ công (PENDING)")
        self.new_post_button = QPushButton("Bài đăng khác của Package này")
        post_layout.addWidget(
            QLabel(
                "App không gửi bài ra nền tảng. Chuẩn bị khi đã duyệt; xác nhận sau khi đăng đúng snapshot."
            )
        )
        post_layout.addWidget(self.prepare_button)
        post_layout.addWidget(self.new_post_button)
        self.posts = QTableWidget(0, 4)
        self.posts.setHorizontalHeaderLabels(
            ("Post", "Trạng thái", "External ID / URL", "Đã đăng lúc UTC")
        )
        self.posts.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.posts.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        post_layout.addWidget(self.posts, 1)
        post_form = QFormLayout()
        self.external_id, self.external_url, self.published_at = (
            QLineEdit(),
            QLineEdit(),
            QLineEdit(_timestamp()),
        )
        post_form.addRow("External ID (thiếu để trống)", self.external_id)
        post_form.addRow("URL (thiếu để trống)", self.external_url)
        post_form.addRow("Thời điểm có timezone", self.published_at)
        post_layout.addLayout(post_form)
        self.post_confirmed = QCheckBox(
            "Tôi xác nhận đã đăng thủ công đúng Package/fingerprint này"
        )
        self.publish_button = QPushButton("Ghi nhận PUBLISHED")
        post_layout.addWidget(self.post_confirmed)
        post_layout.addWidget(self.publish_button)
        self.tabs.addTab(post_tab, "Post thủ công")
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.project.currentIndexChanged.connect(lambda: self._load_project())
        self.packages.currentIndexChanged.connect(self._load_snapshot)
        self.draft.currentIndexChanged.connect(self._draft_changed)
        self.reload_button.clicked.connect(
            lambda: self.reload(self.selected_package_id())
        )
        self.new_button.clicked.connect(self.new_package)
        self.add_button.clicked.connect(self.add_media)
        self.up_button.clicked.connect(lambda: self.move_media(-1))
        self.down_button.clicked.connect(lambda: self.move_media(1))
        self.remove_button.clicked.connect(self.remove_media)
        self.create_button.clicked.connect(self.create_package)
        self.open_draft_button.clicked.connect(self.open_draft)
        self.preview_button.clicked.connect(self.preview_file)
        self.copy_button.clicked.connect(self.copy_text)
        self.check_button.clicked.connect(self.check)
        self.approve_button.clicked.connect(lambda: self.decide("APPROVED"))
        self.reject_button.clicked.connect(lambda: self.decide("REJECTED"))
        self.revoke_button.clicked.connect(lambda: self.decide("REVOKED"))
        self.prepare_button.clicked.connect(self.prepare)
        self.new_post_button.clicked.connect(lambda: self.posts.setCurrentCell(-1, -1))
        self.publish_button.clicked.connect(self.publish)
        for widget in (
            self.actor,
            self.reason,
            self.platform,
            self.format,
            self.external_id,
            self.external_url,
            self.published_at,
        ):
            widget.textChanged.connect(self._refresh)
        self.review_confirmed.toggled.connect(self._refresh)
        self.post_confirmed.toggled.connect(self._refresh)
        self.items.itemSelectionChanged.connect(self._item_changed)
        self.media.currentRowChanged.connect(self._refresh)
        self.posts.itemSelectionChanged.connect(self._post_changed)
        for combo in (self.project, self.packages, self.draft, self.file):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(18)
            combo.currentTextChanged.connect(combo.setToolTip)
        self.reload()

    def selected_package_id(self) -> str | None:
        return self.packages.currentData()

    def selected_post(self) -> Post | None:
        row = self.posts.currentRow()
        return self._posts[row] if 0 <= row < len(self._posts) else None

    def reload(self, package_id: str | None = None, post_id: str | None = None) -> bool:
        if self._worker is not None:
            return False
        previous = self.project.currentData()
        self.project.blockSignals(True)
        self.project.clear()
        self._projects.clear()
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                projects = list_projects(db, include_archived=True)
            for project in projects:
                self.project.addItem(
                    f"{project.title} — {project.status}", project.project_id
                )
                self._projects[project.project_id] = project.status == "ACTIVE"
            index = self.project.findData(previous)
            if index >= 0:
                self.project.setCurrentIndex(index)
        except Exception as exc:
            self._loaded = False
            self.status.setText(f"Không thể đọc Project: {exc}")
            self._refresh()
            return False
        finally:
            self.project.blockSignals(False)
        return self._load_project(package_id, post_id)

    def _load_project(
        self, package_id: str | None = None, post_id: str | None = None
    ) -> bool:
        self._loaded = False
        self._snapshot = None
        self._drafts.clear()
        self._files.clear()
        self.draft.blockSignals(True)
        self.packages.blockSignals(True)
        self.draft.clear()
        self.packages.clear()
        self.file.clear()
        self.media.clear()
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                db.execute("BEGIN")
                drafts = db.execute(
                    """SELECT v.draft_version_id,v.version_no,v.body_text,d.status
                FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
                JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
                WHERE d.project_id=? ORDER BY d.rowid,v.version_no""",
                    (self.project.currentData(),),
                ).fetchall()
                files = db.execute("""SELECT f.file_id,a.display_name,f.role,f.sha256 FROM asset_files f
                JOIN assets a ON a.asset_id=f.asset_id WHERE a.deleted_at IS NULL ORDER BY a.rowid,f.rowid""").fetchall()
                packages = db.execute(
                    "SELECT package_id,created_at,platform FROM publication_packages WHERE project_id=? AND sealed_at IS NOT NULL ORDER BY rowid",
                    (self.project.currentData(),),
                ).fetchall()
            for version, number, body, state in drafts:
                self._drafts[version] = (body, state == "ACTIVE")
                self.draft.addItem(
                    f"v{number} — {body[:45]} — {state} — {version}", version
                )
            for identity, name, role, digest in files:
                self._files[identity] = (f"{name} — {role}", digest)
                self.file.addItem(f"{name} — {role} — {identity}", identity)
            self.packages.addItem("Package mới", None)
            for identity, created, platform in packages:
                self.packages.addItem(f"{created} — {platform} — {identity}", identity)
            index = self.packages.findData(package_id)
            self.packages.setCurrentIndex(max(index, 0))
            self._loaded = True
        except Exception as exc:
            self.status.setText(f"Không thể đọc workspace: {exc}")
        finally:
            self.draft.blockSignals(False)
            self.packages.blockSignals(False)
        self._draft_changed()
        if self._loaded:
            return self._load_snapshot(post_id=post_id)
        self._refresh()
        return False

    def _draft_changed(self) -> None:
        self.caption.setPlainText(
            self._drafts.get(str(self.draft.currentData()), ("", False))[0]
        )
        self._refresh()

    def _load_snapshot(self, _index: int = 0, *, post_id: str | None = None) -> bool:
        self._snapshot = None
        self._checked = False
        self._blockers = ()
        self._approvals = self._posts = ()
        self.review_confirmed.setChecked(False)
        self.post_confirmed.setChecked(False)
        self.items.setRowCount(0)
        self.history.setRowCount(0)
        self.posts.setRowCount(0)
        self.eligibility.setPlainText("Chưa kiểm; mọi bước đều kiểm lại trước khi ghi.")
        identity = self.selected_package_id()
        try:
            if identity is not None:
                with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                    db.execute("BEGIN")
                    self._snapshot = package_snapshot(db, identity)
                    if self._snapshot.project_id != self.project.currentData():
                        raise ValueError(
                            "Package does not belong to the selected Project"
                        )
                    self._approvals = list_approvals(db, identity)
                    self._posts = list_posts(db, identity)
                snapshot = self._snapshot
                self.snapshot_label.setText(
                    f"Package {identity}\nDraft Version {snapshot.draft_version_id} • {snapshot.platform}/{snapshot.format}\nFingerprint {snapshot.fingerprint}"
                )
                for kind, position, text, file_id, digest in snapshot.items:
                    row = self.items.rowCount()
                    self.items.insertRow(row)
                    payload = (
                        text
                        if text is not None
                        else f"{self._files.get(file_id or '', ('Unavailable', ''))[0]} • {file_id}"
                    )
                    for col, value in enumerate((kind, str(position), payload, digest)):
                        self.items.setItem(row, col, QTableWidgetItem(value))
                    self.items.item(row, 0).setData(
                        Qt.ItemDataRole.UserRole, (file_id, digest)
                    )
                if self.items.rowCount():
                    self.items.setCurrentCell(0, 0)
                for approval in self._approvals:
                    row = self.history.rowCount()
                    self.history.insertRow(row)
                    for col, value in enumerate(
                        (
                            f"{approval.decision} • {approval.decided_at}",
                            approval.actor_id,
                            approval.reason or "",
                        )
                    ):
                        self.history.setItem(row, col, QTableWidgetItem(value))
                for post in self._posts:
                    row = self.posts.rowCount()
                    self.posts.insertRow(row)
                    for col, value in enumerate(
                        (
                            post.post_id,
                            post.status,
                            f"{post.external_post_id or 'NULL'} • {post.external_url or 'NULL'}",
                            post.published_at or "NULL",
                        )
                    ):
                        self.posts.setItem(row, col, QTableWidgetItem(value))
                    if post.post_id == post_id:
                        self.posts.setCurrentCell(row, 0)
                self.tabs.setCurrentIndex(1)
            else:
                self.snapshot_label.setText(
                    "Chọn Package đã lưu để xem exact snapshot."
                )
                self.tabs.setCurrentIndex(0)
            self._loaded = True
        except Exception as exc:
            self._loaded = False
            self._snapshot = None
            self.status.setText(f"Không thể đọc exact Package: {exc}")
        self._refresh()
        return self._loaded

    def _item_changed(self) -> None:
        row = self.items.currentRow()
        text = (
            self._snapshot.items[row][2]
            if self._snapshot and 0 <= row < len(self._snapshot.items)
            else None
        )
        self.text_preview.setPlainText(text or "")
        self._refresh()

    def _refresh(self, *_args: object) -> None:
        idle = self._worker is None
        base = idle and self._loaded
        active = self._projects.get(str(self.project.currentData()), False)
        composing = (
            base
            and active
            and self._snapshot is None
            and self.selected_package_id() is None
        )
        for widget in (
            self.project,
            self.packages,
            self.reload_button,
            self.new_button,
            self.tabs,
            self.buttons,
        ):
            widget.setEnabled(idle)
        self.packages.setEnabled(base)
        self.new_button.setEnabled(base)
        for widget in (
            self.draft,
            self.platform,
            self.format,
            self.file,
            self.kind,
            self.add_button,
            self.media,
            self.up_button,
            self.down_button,
            self.remove_button,
            self.cta,
            self.alt,
        ):
            widget.setEnabled(composing)
        self.create_button.setEnabled(
            composing
            and self._drafts.get(str(self.draft.currentData()), ("", False))[1]
            and bool(self.platform.text().strip())
            and bool(self.format.text().strip())
        )
        version = (
            self._snapshot.draft_version_id
            if self._snapshot
            else self.draft.currentData()
        )
        self.open_draft_button.setEnabled(base and version in self._drafts)
        snapshot = base and self._snapshot is not None
        self.check_button.setEnabled(snapshot)
        self.actor.setEnabled(snapshot)
        self.reason.setEnabled(snapshot)
        self.review_confirmed.setEnabled(snapshot)
        ready = snapshot and active and self._checked and not self._blockers
        actor = bool(self.actor.text().strip())
        self.approve_button.setEnabled(
            ready and actor and self.review_confirmed.isChecked()
        )
        self.reject_button.setEnabled(
            snapshot and actor and bool(self.reason.text().strip())
        )
        self.revoke_button.setEnabled(self.reject_button.isEnabled())
        row = self.items.currentRow()
        cell = self.items.item(row, 0) if row >= 0 else None
        payload = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        media = isinstance(payload, tuple) and payload[0] is not None
        self.preview_button.setEnabled(snapshot and media)
        approved = bool(self._approvals and self._approvals[-1].decision == "APPROVED")
        post = self.selected_post()
        self.prepare_button.setEnabled(ready and approved and post is None)
        self.new_post_button.setEnabled(snapshot)
        pending = bool(post and post.status == "PENDING")
        text_item = bool(
            self._snapshot
            and 0 <= row < len(self._snapshot.items)
            and self._snapshot.items[row][2] is not None
        )
        self.copy_button.setEnabled(ready and approved and pending and text_item)
        for widget in (
            self.external_id,
            self.external_url,
            self.published_at,
            self.post_confirmed,
        ):
            widget.setEnabled(snapshot and pending)
        self.publish_button.setEnabled(
            ready
            and approved
            and pending
            and self.post_confirmed.isChecked()
            and bool(self.published_at.text().strip())
        )

    def new_package(self) -> None:
        if self._worker is None and self._loaded:
            self.packages.setCurrentIndex(0)
            self._load_snapshot()

    def add_media(self) -> None:
        if not self.add_button.isEnabled() or self.file.currentData() is None:
            return
        file_id = str(self.file.currentData())
        kind = self.kind.currentText()
        item = QListWidgetItem(f"{kind} • {self._files[file_id][0]} • {file_id}")
        item.setData(Qt.ItemDataRole.UserRole, (kind, file_id, self._files[file_id][1]))
        self.media.addItem(item)

    def move_media(self, delta: int) -> None:
        if self._worker is not None or not self.media.isEnabled():
            return
        row = self.media.currentRow()
        target = row + delta
        if 0 <= row < self.media.count() and 0 <= target < self.media.count():
            item = self.media.takeItem(row)
            self.media.insertItem(target, item)
            self.media.setCurrentRow(target)

    def remove_media(self) -> None:
        if self._worker is None and self.media.isEnabled():
            self.media.takeItem(self.media.currentRow())

    def create_package(self) -> None:
        if not self.create_button.isEnabled():
            return
        # Qt's plain text display normalizes CRLF/NBSP. Persist the raw sealed body.
        body = self._drafts[str(self.draft.currentData())][0]
        items = [PackageItem("CAPTION", 0, text_payload=body)]
        for row in range(self.media.count()):
            kind, file_id, digest = self.media.item(row).data(Qt.ItemDataRole.UserRole)
            items.append(PackageItem(kind, row, file_id=file_id, content_digest=digest))
        for kind, field in (("CTA", self.cta), ("ALT_TEXT", self.alt)):
            if field.text():
                items.append(PackageItem(kind, 0, text_payload=field.text()))
        self._start(
            PublicationTask(
                "CREATE",
                package_id=uuid4().hex,
                project_id=str(self.project.currentData()),
                draft_version_id=str(self.draft.currentData()),
                platform=self.platform.text(),
                format=self.format.text(),
                items=tuple(items),
            )
        )

    def _task(
        self,
        action: str,
        *,
        post_id: str = "",
        external_id: str | None = None,
        external_url: str | None = None,
        published_at: str = "",
        file_id: str = "",
        digest: str = "",
        text_to_copy: str = "",
    ) -> PublicationTask:
        assert self._snapshot is not None
        return PublicationTask(
            action,
            package_id=self._snapshot.package_id,
            fingerprint=self._snapshot.fingerprint,
            actor=self.actor.text(),
            reason=self.reason.text(),
            post_id=post_id,
            external_id=external_id,
            external_url=external_url,
            published_at=published_at,
            file_id=file_id,
            digest=digest,
            text_to_copy=text_to_copy,
        )

    def check(self) -> None:
        if self.check_button.isEnabled():
            self._start(self._task("CHECK"))

    def decide(self, decision: ApprovalDecision) -> None:
        button = {
            "APPROVED": self.approve_button,
            "REJECTED": self.reject_button,
            "REVOKED": self.revoke_button,
        }[decision]
        if button.isEnabled():
            self._start(self._task(decision))

    def prepare(self) -> None:
        if self.prepare_button.isEnabled():
            self._start(self._task("PREPARE", post_id=uuid4().hex))

    def publish(self) -> None:
        post = self.selected_post()
        if self.publish_button.isEnabled() and post is not None:
            self._start(
                self._task(
                    "PUBLISHED",
                    post_id=post.post_id,
                    external_id=self.external_id.text(),
                    external_url=self.external_url.text(),
                    published_at=self.published_at.text(),
                )
            )

    def preview_file(self) -> None:
        if self.preview_button.isEnabled():
            file_id, digest = self.items.item(self.items.currentRow(), 0).data(
                Qt.ItemDataRole.UserRole
            )
            self._start(self._task("PREVIEW", file_id=file_id, digest=digest))

    def copy_text(self) -> None:
        post = self.selected_post()
        if (
            self.copy_button.isEnabled()
            and self._snapshot is not None
            and post is not None
        ):
            text = self._snapshot.items[self.items.currentRow()][2]
            assert text is not None
            self._start(
                self._task("COPY_TEXT", post_id=post.post_id, text_to_copy=text)
            )

    def open_draft(self) -> None:
        if not self.open_draft_button.isEnabled():
            return
        version = (
            self._snapshot.draft_version_id
            if self._snapshot
            else str(self.draft.currentData())
        )
        self.accept()
        self.draft_requested.emit(version)

    def _post_changed(self) -> None:
        self.post_confirmed.setChecked(False)
        post = self.selected_post()
        self.external_id.setText(post.external_post_id or "" if post else "")
        self.external_url.setText(post.external_url or "" if post else "")
        self.published_at.setText(
            post.published_at or _timestamp() if post else _timestamp()
        )
        self._refresh()

    def _start(self, task: PublicationTask) -> None:
        if self._worker is not None:
            return
        self._result, self._failure = None, None
        self._worker = PublicationWorker(self.root, task)
        self._worker.completed.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finish)
        self.status.setText("Đang kiểm/ghi exact snapshot…")
        self._refresh()
        self._worker.start()

    def _completed(self, result: object) -> None:
        self._result = result

    def _failed(self, message: str) -> None:
        self._failure = message

    def _finish(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        task = worker.task
        worker.deleteLater()
        if self._failure is not None:
            self._checked = False
            self.status.setText(f"Không thể thực hiện: {self._failure[:240]}")
            self.eligibility.setPlainText(self._failure)
            self._refresh()
            QMessageBox.warning(self, "Package / Publication", self.status.text())
        elif task.action == "CHECK" and isinstance(self._result, tuple):
            self._blockers = cast(tuple[str, ...], self._result)
            self._checked = True
            self.eligibility.setPlainText(
                "\n".join(self._blockers)
                or "Điều kiện hiện tại đạt; duyệt/chuẩn bị/ghi Post vẫn kiểm lại."
            )
            self.status.setText("Đã kiểm điều kiện hiện tại.")
            self._refresh()
        elif isinstance(self._result, PackageMediaPreview):
            self._refresh()
            with self._result.handles:
                viewer = WholeEvidenceView(
                    "Media của Package; xem exact file, không tạo Evidence hay cấp quyền sử dụng.",
                    self._result.source,
                )
                viewer.setWindowTitle("Media của exact Package")
                viewer.exec()
        elif self._result is not None:
            post_id = self._result.post_id if isinstance(self._result, Post) else None
            identity = (
                self._result if isinstance(self._result, str) else task.package_id
            )
            if task.action == "COPY_TEXT":
                QApplication.clipboard().setText(task.text_to_copy)
            if self.reload(str(identity), post_id):
                self.status.setText(
                    f"Đã ghi {task.action}: {post_id or identity}. Kiểm lại trước bước tiếp theo."
                )
                if post_id is not None:
                    self.tabs.setCurrentIndex(2)
            else:
                self._loaded = False
                self.status.setText(
                    f"Đã ghi {task.action} {post_id or identity}, nhưng chưa đọc lại được; đọc lại trước khi tiếp tục."
                )
                self._refresh()
        else:
            self._loaded = False
            self.status.setText("Worker chưa trả kết quả; cần đọc lại dữ liệu.")
            self._refresh()

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
