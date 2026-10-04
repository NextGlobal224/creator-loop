"""Separate fresh completed-copy choice; copy retains guard for later health."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from creator_loop.maintenance_process import MaintenanceCommand
from creator_loop.restore_assessment import assessment_identity

_NAMES = tuple(
    "creator_loop.sqlite3" + suffix for suffix in ("", "-wal", "-shm", "-journal")
)


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_choice(
    choice: Any, root: Path, selection: tuple[str, ...]
) -> dict[str, Any]:
    """Validate bounded CLI metadata only; backend revalidates actual files."""
    installation, candidate, backup, journal = selection
    root_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    if not isinstance(choice, dict) or not isinstance(choice.get("copy_review"), dict):
        raise ValueError("Fresh choice object required")
    base = choice["copy_review"]
    proof = base.get("inspection")
    if not isinstance(proof, dict):
        raise ValueError("Actual copy inspection required")
    if (
        type(choice.get("fresh_completed_restore_review_format")) is not int
        or choice["fresh_completed_restore_review_format"] != 1
        or choice.get("data_root_identity") != root_id
        or choice.get("copy_journal_name") != Path(journal).name
        or re.fullmatch(r"corrupt-restore-[0-9a-f]{32}\.json", Path(journal).name)
        is None
        or Path(journal).resolve(strict=True).parent != root / "manifests"
        or choice.get("installation_root")
        != str(Path(installation).resolve(strict=True))
        or choice.get("candidate_directory")
        != str(Path(candidate).resolve(strict=True))
        or choice.get("backup_id") != backup
        or not _sha(choice.get("completed_database_sha256"))
        or not _sha(choice.get("assessment_identity"))
        or assessment_identity(choice) != choice["assessment_identity"]
        or choice.get("current_changes_assessable") is not False
        or "current_counts" not in choice
        or choice.get("current_counts") is not None
        or choice.get("consistent_current_backup") is not False
        or choice.get("requires_lost_changes_confirmation") is not True
        or choice.get("guard_retained") is not True
        or choice.get("apply_supported") is not True
        or choice.get("apply_authorized") is not False
        or choice.get("activated") is not False
        or choice.get("restored") is not False
        or not isinstance(choice.get("loss_warning"), str)
        or not choice["loss_warning"]
        or not isinstance(choice.get("backup_created_at"), str)
        or not choice["backup_created_at"]
        or type(choice.get("media_issue_count")) is not int
        or choice["media_issue_count"] < 0
        or choice.get("requires_media_issues_confirmation")
        is not (choice["media_issue_count"] > 0)
        or type(base.get("corrupt_copy_review_format")) is not int
        or base["corrupt_copy_review_format"] != 1
        or type(base.get("media_issue_count")) is not int
        or any(
            base.get(name) != choice[name]
            for name in (
                "data_root_identity",
                "copy_journal_name",
                "installation_root",
                "backup_id",
                "backup_created_at",
                "media_issue_count",
            )
        )
        or type(base.get("backup_schema")) is not int
        or base["backup_schema"] <= 0
        or type(base.get("target_schema")) is not int
        or base["target_schema"] < base["backup_schema"]
        or base.get("candidate_name") != Path(candidate).name
        or base.get("requires_fresh_restore_decision") is not True
        or base.get("originals_retained_complete") is not True
        or base.get("continuation_allowed") is not False
        or base.get("unknown_continuation_allowed") is not False
        or base.get("media_included") is not False
        or base.get("activated") is not False
        or base.get("restored") is not False
        or not _sha(base.get("assessment_identity"))
        or assessment_identity(base) != base["assessment_identity"]
        or type(proof.get("corrupt_restore_inspection_format")) is not int
        or proof["corrupt_restore_inspection_format"] != 2
        or proof.get("copy_id")
        != Path(journal).name.removeprefix("corrupt-restore-").removesuffix(".json")
        or not _sha(proof.get("journal_sha256"))
        or not _sha(proof.get("preparation_sha256"))
        or proof.get("data_root_identity") != root_id
        or proof.get("guard_retained") is not True
        or proof.get("requires_recovery_health") is not True
        or proof.get("activated") is not False
        or proof.get("restored") is not False
        or proof.get("actual_state")
        not in (
            "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
            "EMPTY_CURRENT_DATABASE_GUARDED",
            "UNKNOWN_SOURCE_EVIDENCE",
            "UNKNOWN_LIVE_SIDECARS",
            "UNKNOWN_CURRENT_DATABASE",
            "UNKNOWN_CURRENT_PHYSICAL_BYTES",
        )
        or not _sha(proof.get("inspection_identity"))
    ):
        raise ValueError(
            "Typed fresh choice, explicit selection and fresh proof required"
        )
    if datetime.fromisoformat(choice["backup_created_at"]).utcoffset() != timedelta(0):
        raise ValueError("Published UTC backup time required")
    inspection_fields = {
        key: value for key, value in proof.items() if key != "inspection_identity"
    }
    if (
        hashlib.sha256(
            json.dumps(inspection_fields, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        != proof["inspection_identity"]
    ):
        raise ValueError("Invalid current inspection identity")
    current = choice.get("current_source_files")
    if not isinstance(current, list) or len(current) != 4:
        raise ValueError("Whole typed current bundle required")
    plain = []
    for name, entry in zip(_NAMES, current, strict=True):
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"name", "present", "sha256", "byte_size", "device", "inode"}
            or entry.get("name") != name
            or type(entry.get("present")) is not bool
        ):
            raise ValueError("Typed current inventory required")
        if entry["present"]:
            if (
                not _sha(entry["sha256"])
                or type(entry["byte_size"]) is not int
                or entry["byte_size"] < 0
                or type(entry["device"]) is not int
                or entry["device"] < 0
                or type(entry["inode"]) is not int
                or entry["inode"] <= 0
            ):
                raise ValueError("Current bytes and native identity required")
        elif any(
            entry[key] is not None for key in ("sha256", "byte_size", "device", "inode")
        ):
            raise ValueError("Absent current file must not invent data")
        plain.append(
            {key: entry[key] for key in ("name", "present", "sha256", "byte_size")}
        )
    inspected = proof.get("current_files")
    if (
        not isinstance(inspected, list)
        or len(inspected) != 4
        or any(
            not isinstance(entry, dict)
            or set(entry) != {"name", "present", "sha256", "byte_size"}
            or type(entry.get("present")) is not bool
            or (entry["present"] and type(entry.get("byte_size")) is not int)
            for entry in inspected
        )
        or plain != inspected
    ):
        raise ValueError("Typed current bundle differs from bound inspection")
    media = base.get("media_assessment")
    if (
        not isinstance(media, list)
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("file_id"), str)
            or not isinstance(entry.get("role"), str)
            or entry.get("status")
            not in (
                "valid",
                "size_mismatch",
                "digest_mismatch",
                "unsafe_path",
                "unavailable_file",
            )
            or type(entry.get("expected_size")) is not int
            or entry["expected_size"] < 0
            or not _sha(entry.get("expected_sha256"))
            or (
                entry["status"] == "valid"
                and (
                    type(entry.get("actual_size")) is not int
                    or entry["actual_size"] != entry["expected_size"]
                    or entry.get("actual_sha256") != entry["expected_sha256"]
                )
            )
            for entry in media
        )
        or sum(entry["status"] != "valid" for entry in media)
        != choice["media_issue_count"]
    ):
        raise ValueError("Typed matching media assessment required")
    return choice


class FreshRestoreWindow(QMainWindow):
    copy_ready = Signal(str, str, str, str)
    state_changed = Signal()

    def __init__(
        self,
        root: Path,
        installation: str = "",
        candidate: str = "",
        backup_id: str = "",
        journal: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.root = root.resolve(strict=True)
        self.proof: dict[str, Any] | None = None
        self.operation = ""
        self.pending_selection: tuple[str, ...] = ()
        self.command = MaintenanceCommand(self.root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.setWindowTitle("Creator Loop — Quyết định khôi phục mới")
        self.resize(1000, 740)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            "Copy trước đã hoàn tất nhưng DB hiện tại đã đổi, thiếu hoặc có tệp phụ. "
            "Chọn lại backup và candidate cụ thể; không dùng consent của interrupted copy.\n"
            "Không thể xác định mọi thay đổi sẽ mất. Giữ toàn bộ current bundle, originals, "
            "archives, journals và bản cài cũ; raw bundle không phải backup SQLite nhất quán.\n"
            f"Dữ liệu: {self.root}"
        )
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(notice)
        self.installation = QLineEdit(installation)
        self.candidate = QLineEdit(candidate)
        self.backup_id = QLineEdit(backup_id)
        self.journal = QLineEdit(journal)
        self.fields = [self.installation, self.candidate, self.backup_id, self.journal]
        self.browse_buttons: list[QPushButton] = []
        form = QFormLayout()
        for label, field, kind in (
            ("Thư mục các bản cài", self.installation, "directory"),
            ("Candidate đã chọn", self.candidate, "directory"),
            ("Backup DB đã chọn (ID)", self.backup_id, "backup"),
            ("Original copy journal", self.journal, "file"),
        ):
            row = QHBoxLayout()
            row.addWidget(field)
            button = QPushButton("Chọn…")
            button.clicked.connect(
                lambda _checked=False, f=field, k=kind: self._browse(f, k)
            )
            self.browse_buttons.append(button)
            row.addWidget(button)
            form.addRow(label, row)
            field.textChanged.connect(lambda _text: self._invalidate())
        layout.addLayout(form)
        self.review_button = QPushButton("Đánh giá quyết định mới")
        self.review_button.clicked.connect(self._review)
        layout.addWidget(self.review_button)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setMinimumHeight(180)
        layout.addWidget(self.review, 1)
        self.fresh_consent = QCheckBox(
            "Tôi xác nhận đây là quyết định khôi phục mới từ backup/candidate đã hiển thị."
        )
        self.loss_consent = QCheckBox(
            "Tôi chấp nhận mất MỌI thay đổi DB sau thời điểm backup đã hiển thị."
        )
        self.media_consent = QCheckBox(
            "Tôi đã xem media thiếu / thay đổi; backup DB không phục hồi media bytes."
        )
        for checkbox in (self.fresh_consent, self.loss_consent, self.media_consent):
            checkbox.toggled.connect(self._refresh)
            layout.addWidget(checkbox)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy mới và giữ guard")
        self.copy_button.clicked.connect(self._copy)
        self.cancel_button = QPushButton("Hủy tác vụ đang chạy")
        self.cancel_button.clicked.connect(self.command.cancel)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status = QLabel(
            "Chọn original journal, backup và candidate rõ ràng rồi đánh giá."
        )
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        self._refresh()

    def _selection(self) -> tuple[str, ...]:
        return tuple(field.text().strip() for field in self.fields)

    def _browse(self, field: QLineEdit, kind: str) -> None:
        if kind == "file":
            value, _ = QFileDialog.getOpenFileName(
                self, "Chọn original copy journal", "", "Tất cả tệp (*)"
            )
        else:
            value = QFileDialog.getExistingDirectory(
                self,
                "Chọn thư mục",
                str(self.root / "backups") if kind == "backup" else "",
            )
        if value:
            field.setText(Path(value).name if kind == "backup" else value)

    def _invalidate(self) -> None:
        self.proof = None
        for checkbox in (self.fresh_consent, self.loss_consent, self.media_consent):
            checkbox.setChecked(False)
        self.review.clear()
        self._refresh()

    def _refresh(self) -> None:
        busy = self.command.busy
        for widget in [*self.fields, *self.browse_buttons]:
            widget.setEnabled(not busy)
        self.review_button.setEnabled(not busy and all(self._selection()))
        reviewed = self.proof is not None
        issues = bool(self.proof and self.proof["media_issue_count"])
        self.fresh_consent.setEnabled(not busy and reviewed)
        self.loss_consent.setEnabled(not busy and reviewed)
        self.media_consent.setEnabled(not busy and reviewed and issues)
        self.copy_button.setEnabled(
            not busy
            and reviewed
            and self.fresh_consent.isChecked()
            and self.loss_consent.isChecked()
            and (not issues or self.media_consent.isChecked())
        )
        self.cancel_button.setEnabled(busy)

    def _selectors(self) -> list[str]:
        installation, candidate, backup, journal = self._selection()
        return [
            journal,
            "--fresh-restore-backup",
            backup,
            "--installation-root",
            installation,
            "--restore-candidate",
            candidate,
        ]

    def _review(self) -> None:
        if self.review_button.isEnabled():
            self._start(
                "review", ["--review-completed-copy-restore", *self._selectors()]
            )

    def _copy(self) -> None:
        if self.proof is None or not self.copy_button.isEnabled():
            return
        args = [
            "--apply-completed-copy-restore",
            *self._selectors(),
            "--reviewed-fresh-restore",
            self.proof["assessment_identity"],
            "--confirm-fresh-restore",
            "--confirm-lost-changes",
        ]
        if self.media_consent.isChecked():
            args.append("--confirm-media-issues")
        self._start("copy", args)

    def _start(self, operation: str, arguments: list[str]) -> None:
        if self.command.busy:
            return
        self.operation = operation
        self.pending_selection = self._selection()
        self._invalidate()
        if operation == "copy":
            self.state_changed.emit()
        try:
            self.command.start(arguments, timeout_seconds=300)
        except (OSError, ValueError, RuntimeError) as exc:
            self._failed(f"Không thể bắt đầu: {type(exc).__name__}")
        else:
            self.status.setText(
                "Đang thực hiện; hủy/lỗi giữ guard và evidence để đánh giá lại."
            )
            self._refresh()

    def _finished(self, code: int, stdout: str, stderr: str) -> None:
        if code != 0:
            self._failed(
                f"Chưa hoàn tất (exit {code}). {stderr.strip()} Giữ guard và mọi evidence."
            )
            return
        try:
            result = json.loads(stdout)
            if self.pending_selection != self._selection():
                raise ValueError("Selection changed")
            if self.operation == "review":
                choice = _validate_choice(result, self.root, self._selection())
                base = choice["copy_review"]
                lines = [
                    f"Backup {choice['backup_id']} — thời điểm {choice['backup_created_at']}",
                    f"Schema {base['backup_schema']} → {base['target_schema']}; candidate {choice['candidate_directory']}",
                    "MỌI thay đổi DB sau backup có thể mất; không xác định được đầy đủ thay đổi hiện tại.",
                    "Current bundle sẽ được giữ nguyên; đây không phải backup SQLite nhất quán.",
                    f"Media cần xử lý: {choice['media_issue_count']}; consent copy không bỏ qua health.",
                    "Copy mới vẫn có guard; sau đó đánh giá actual state và xác nhận health riêng.",
                ]
                for entry in choice["current_source_files"]:
                    lines.append(
                        f"Hiện tại {entry['name']}: {'có' if entry['present'] else 'vắng'}, {entry['byte_size']} bytes, SHA256 {entry['sha256']}"
                    )
                for media in base["media_assessment"]:
                    if media["status"] != "valid":
                        lines.append(
                            f"Media {media['file_id']} ({media['role']}): {media['status']}"
                        )
                self.proof = choice
                self.review.setPlainText("\n".join(lines))
            elif self.operation == "copy":
                journal_name = Path(self.journal.text().strip()).name
                copy_id = journal_name.removeprefix("corrupt-restore-").removesuffix(
                    ".json"
                )
                if (
                    re.fullmatch(r"corrupt-restore-[0-9a-f]{32}\.json", journal_name)
                    is None
                    or not isinstance(result, dict)
                    or result.get("phase") != "FRESH_DB_COMMITTED_GUARDED"
                    or result.get("guard_retained") is not True
                    or result.get("activated") is not False
                    or result.get("restored") is not False
                    or not isinstance(result.get("journal_name"), str)
                    or re.fullmatch(
                        rf"corrupt-fresh-restore-{copy_id}-[0-9a-f]{{32}}\.json",
                        result["journal_name"],
                    )
                    is None
                ):
                    raise ValueError("Fresh guarded copy receipt required")
                self.state_changed.emit()
                self.copy_ready.emit(
                    self.journal.text().strip(),
                    self.installation.text().strip(),
                    self.candidate.text().strip(),
                    self.backup_id.text().strip(),
                )
                self.close()
                return
            else:
                raise ValueError("Unknown operation")
        except (OSError, ValueError, TypeError, KeyError):
            self._failed(
                "Đánh giá hoặc receipt không hợp lệ / đã cũ; chọn lại và đánh giá trước xác nhận."
            )
            return
        self.operation = ""
        self.status.setText(
            f"Đánh giá readonly đã kết thúc. Log: {self.command.log_directory}"
        )
        self._refresh()

    def _failed(self, message: str) -> None:
        self._invalidate()
        if self.operation == "copy":
            self.state_changed.emit()
        self.operation = ""
        self.status.setText(f"{message[:500]}\nLog: {self.command.log_directory}")
        self._refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.command.cancel()
        self.command.close()
        self._invalidate()
        super().closeEvent(event)
