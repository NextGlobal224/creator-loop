"""Explicit backup/update/restore UI, usable while normal launch is guarded."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
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


class MaintenanceWindow(QMainWindow):
    def __init__(self, root: Path, installation: Path | None = None) -> None:
        super().__init__()
        self.root = root.resolve(strict=True)
        self.assessment: dict[str, Any] | None = None
        self.copy_review: dict[str, Any] | None = None
        self.damaged_window: QMainWindow | None = None
        self.operation = ""
        self.pending_selection: tuple[str, ...] = ()
        self.command = MaintenanceCommand(self.root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.setWindowTitle("Creator Loop — Sao lưu, cập nhật và khôi phục")
        self.resize(1050, 800)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            "Đóng Library và các tác vụ trước khi thao tác. Backup chỉ chứa DB, "
            "không chứa media. Giữ mọi backup và bản cài cũ; chưa tự động xóa theo thời hạn.\n"
            f"Dữ liệu đang chọn: {self.root}"
        )
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(notice)
        form = QFormLayout()
        self.installation = QLineEdit(str(installation) if installation else "")
        self.zip_path = QLineEdit()
        self.manifest_path = QLineEdit()
        self.candidate = QLineEdit()
        self.backup_id = QLineEdit()
        self.journal = QLineEdit()
        self.fields = [
            self.installation,
            self.zip_path,
            self.manifest_path,
            self.candidate,
            self.backup_id,
            self.journal,
        ]
        self.browse_buttons: list[QPushButton] = []
        for label, field, kind in (
            ("Thư mục các bản cài", self.installation, "directory"),
            ("ZIP cập nhật", self.zip_path, "file"),
            ("Manifest đi cùng ZIP", self.manifest_path, "file"),
            ("Bản cài đã staging để khôi phục", self.candidate, "directory"),
            ("Backup DB đã chọn (ID)", self.backup_id, "backup"),
            ("Journal cập nhật / khôi phục", self.journal, "file"),
        ):
            row = QHBoxLayout()
            row.addWidget(field)
            browse = QPushButton("Chọn…")
            browse.clicked.connect(
                lambda _checked=False, target=field, selection=kind: self._browse(
                    target, selection
                )
            )
            self.browse_buttons.append(browse)
            row.addWidget(browse)
            form.addRow(label, row)
            field.textChanged.connect(lambda _text: self._invalidate())
        layout.addLayout(form)
        actions = QGridLayout()
        self.buttons: dict[str, QPushButton] = {}
        for index, (kind, title) in enumerate(
            (
                ("backup", "Tạo backup DB hiện tại"),
                ("stage-update", "Kiểm ZIP và staging"),
                ("prepare-update", "Backup và nâng schema DB"),
                ("inspect-update", "Kiểm trạng thái cập nhật"),
                ("resume-update", "Tiếp tục cập nhật đã chọn"),
                ("activate-update", "Activate và kiểm sức khỏe"),
                ("inspect-restore", "Đánh giá backup trước khôi phục"),
                ("recover-restore", "Giải quyết khôi phục bị gián đoạn"),
                ("review-corrupt-copy", "Đánh giá copy có guard"),
                ("resume-corrupt-copy", "Tiếp tục copy đã đánh giá"),
                ("recover-corrupt-copy", "Khôi phục copy và kiểm sức khỏe"),
                ("damaged-restore", "Nguồn hỏng: giữ raw, staging và copy"),
            )
        ):
            button = QPushButton(title)
            button.clicked.connect(
                lambda _checked=False, action=kind: self._action(action)
            )
            self.buttons[kind] = button
            actions.addWidget(button, index // 3, index % 3)
        layout.addLayout(actions)
        self.upgrade_consent = QCheckBox(
            "Tôi đồng ý nâng schema DB; không có downgrade tự động. Backup hiện tại sẽ được giữ."
        )
        self.upgrade_consent.toggled.connect(self._refresh)
        layout.addWidget(self.upgrade_consent)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setMinimumHeight(160)
        self.review.setPlaceholderText(
            "Chọn rõ backup và bản cài staging, rồi đánh giá. Không tự chọn bản mới nhất."
        )
        layout.addWidget(self.review, 1)
        self.loss_consent = QCheckBox(
            "Tôi chấp nhận mất MỌI thay đổi DB sau thời điểm backup đã hiển thị, kể cả sửa có số dòng không đổi."
        )
        self.media_consent = QCheckBox(
            "Tôi đã xem các media thiếu / thay đổi; backup DB không khôi phục bytes và activation có thể bị chặn."
        )
        for consent in (self.loss_consent, self.media_consent):
            consent.toggled.connect(self._refresh)
            layout.addWidget(consent)
        self.empty_consent = QCheckBox("Giữ riêng DB rỗng hiện tại trước khi copy mới.")
        self.unknown_consent = QCheckBox(
            "Giữ nguyên DB và các tệp phụ chưa rõ; đây không phải backup SQLite nhất quán."
        )
        self.health_consent = QCheckBox(
            "Tôi đồng ý chuyển sang bản cài đã đánh giá và kiểm sức khỏe."
        )
        for consent in (self.empty_consent, self.unknown_consent, self.health_consent):
            consent.toggled.connect(self._refresh)
            layout.addWidget(consent)
        bottom = QHBoxLayout()
        self.apply_button = QPushButton("Khôi phục DB theo đánh giá đã xác nhận")
        self.apply_button.clicked.connect(self._apply)
        self.cancel_button = QPushButton("Hủy tác vụ đang chạy")
        self.cancel_button.clicked.connect(self.command.cancel)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.status = QLabel(
            "Chưa thực hiện thao tác. Nếu đang có guard, giữ DB/backup/journal và dùng recovery."
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
                self, "Chọn tệp", "", "Tất cả tệp (*)"
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
        self.assessment = None
        self.copy_review = None
        self.loss_consent.setChecked(False)
        self.media_consent.setChecked(False)
        self.upgrade_consent.setChecked(False)
        for consent in (self.empty_consent, self.unknown_consent, self.health_consent):
            consent.setChecked(False)
        self.review.clear()
        self._refresh()

    def _refresh(self) -> None:
        busy = self.command.busy
        installation, zip_path, manifest, candidate, backup_id, journal = (
            self._selection()
        )
        copy = self.copy_review
        state = copy["inspection"]["actual_state"] if copy else ""
        copy_media_issues = bool(copy and copy["media_issue_count"])
        known_resume = state in (
            "SOURCE_NOT_MOVED_GUARDED",
            "SOURCE_PARTIALLY_RETAINED_GUARDED",
            "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
            "EMPTY_CURRENT_DATABASE_GUARDED",
        )
        partial_confirmed = (
            state != "EMPTY_CURRENT_DATABASE_GUARDED" or self.empty_consent.isChecked()
        )
        unknown_confirmed = bool(
            copy
            and state.startswith("UNKNOWN_")
            and copy["unknown_continuation_allowed"]
            and self.unknown_consent.isChecked()
        )
        for field in [*self.fields, *self.browse_buttons]:
            field.setEnabled(not busy)
        for kind, button in self.buttons.items():
            ready = {
                "backup": True,
                "damaged-restore": True,
                "stage-update": bool(installation and zip_path and manifest),
                "prepare-update": bool(
                    installation
                    and zip_path
                    and manifest
                    and self.upgrade_consent.isChecked()
                ),
                "inspect-update": bool(installation and journal),
                "resume-update": bool(
                    installation and journal and self.upgrade_consent.isChecked()
                ),
                "activate-update": bool(installation and journal),
                "inspect-restore": bool(installation and candidate and backup_id),
                "recover-restore": bool(installation and journal),
                "review-corrupt-copy": bool(installation and journal),
                "resume-corrupt-copy": bool(
                    installation
                    and journal
                    and copy
                    and self.loss_consent.isChecked()
                    and ((known_resume and partial_confirmed) or unknown_confirmed)
                    and (not copy_media_issues or self.media_consent.isChecked())
                ),
                "recover-corrupt-copy": bool(
                    installation
                    and journal
                    and copy
                    and state == "VALIDATED_COPY_GUARDED"
                    and not copy_media_issues
                    and self.health_consent.isChecked()
                ),
            }[kind]
            button.setEnabled(not busy and ready)
        media_issues = (
            bool(self.assessment and self.assessment["media_issue_count"])
            or copy_media_issues
        )
        self.upgrade_consent.setEnabled(not busy)
        self.loss_consent.setEnabled(
            not busy
            and (
                self.assessment is not None
                or bool(copy and state != "VALIDATED_COPY_GUARDED")
            )
        )
        self.media_consent.setEnabled(not busy and media_issues)
        self.empty_consent.setEnabled(
            not busy and bool(copy) and state == "EMPTY_CURRENT_DATABASE_GUARDED"
        )
        self.unknown_consent.setEnabled(
            not busy and bool(copy and copy["unknown_continuation_allowed"])
        )
        self.health_consent.setEnabled(
            not busy
            and bool(copy)
            and state == "VALIDATED_COPY_GUARDED"
            and not copy_media_issues
        )
        self.apply_button.setEnabled(
            not busy
            and self.assessment is not None
            and self.loss_consent.isChecked()
            and (not media_issues or self.media_consent.isChecked())
        )
        self.cancel_button.setEnabled(busy)

    def _action(self, kind: str) -> None:
        if kind == "damaged-restore":
            if self.command.busy:
                return
            if self.damaged_window is None:
                from .damaged_restore_ui import DamagedRestoreWindow

                dialog = DamagedRestoreWindow(
                    self.root,
                    self.installation.text().strip(),
                    self.candidate.text().strip(),
                    self.backup_id.text().strip(),
                    self,
                )
                dialog.copy_ready.connect(
                    lambda journal, installation: (
                        self.installation.setText(installation),
                        self.journal.setText(journal),
                    )
                )
                self.damaged_window = dialog
            self.damaged_window.show()
            self.damaged_window.raise_()
            return
        installation, zip_path, manifest, candidate, backup_id, journal = (
            self._selection()
        )
        args = [f"--{kind}"]
        if kind in ("resume-corrupt-copy", "recover-corrupt-copy"):
            if self.copy_review is None or not self.buttons[kind].isEnabled():
                return
            args += [
                journal,
                "--installation-root",
                installation,
                "--reviewed-inspection",
                self.copy_review["inspection"]["inspection_identity"],
            ]
            if kind == "recover-corrupt-copy":
                args.append("--confirm-recovery")
            else:
                args.append("--confirm-lost-changes")
                if self.empty_consent.isChecked():
                    args.append("--confirm-keep-partial")
                if self.unknown_consent.isChecked():
                    args.append("--confirm-preserve-unknown")
                if self.media_consent.isChecked():
                    args.append("--confirm-media-issues")
            self._start(kind, args)
            return
        if kind in ("stage-update", "prepare-update"):
            if not (installation and zip_path and manifest) or (
                kind == "prepare-update" and not self.upgrade_consent.isChecked()
            ):
                return
            args += [zip_path, "--release-manifest", manifest]
        elif kind == "inspect-restore":
            if not (installation and candidate and backup_id):
                return
            args += [backup_id, "--restore-candidate", candidate]
        elif kind != "backup":
            if not (installation and journal) or (
                kind == "resume-update" and not self.upgrade_consent.isChecked()
            ):
                return
            args.append(journal)
        if kind != "backup":
            args += ["--installation-root", installation]
        self._start(kind, args)

    def _apply(self) -> None:
        review = self.assessment
        if review is None or not self.apply_button.isEnabled():
            return
        args = [
            "--apply-restore",
            self.backup_id.text().strip(),
            "--restore-candidate",
            self.candidate.text().strip(),
            "--installation-root",
            self.installation.text().strip(),
            "--reviewed-restore",
            review["assessment_identity"],
            "--confirm-lost-changes",
        ]
        if self.media_consent.isChecked():
            args.append("--confirm-media-issues")
        self._start("apply-restore", args)

    def _start(self, kind: str, args: list[str]) -> None:
        if self.command.busy:
            return
        self.operation = kind
        self.pending_selection = self._selection()
        self._invalidate()
        try:
            self.command.start(args, timeout_seconds=300)
        except (OSError, ValueError, RuntimeError) as exc:
            self._failed(f"Không thể bắt đầu: {type(exc).__name__}")
        else:
            self.status.setText(
                "Đang thực hiện; UI vẫn phản hồi. Hủy có thể để lại guard cần recovery."
            )
            self._refresh()

    def _finished(self, code: int, stdout: str, stderr: str) -> None:
        if code != 0:
            self._failed(
                f"Chưa hoàn tất (exit {code}). {stderr.strip()} Giữ backup/DB/guard, kiểm journal; không xóa để bỏ qua lỗi."
            )
            return
        if self.operation == "review-corrupt-copy":
            try:
                review = json.loads(stdout)
                if not isinstance(review, dict) or not isinstance(
                    review.get("inspection"), dict
                ):
                    raise ValueError("Copy review object required")
                proof = review["inspection"]
                if (
                    review.get("corrupt_copy_review_format") != 1
                    or review.get("data_root_identity")
                    != hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
                    or review.get("copy_journal_name")
                    != Path(self.journal.text().strip()).name
                    or review.get("installation_root")
                    != str(Path(self.installation.text().strip()).resolve(strict=True))
                    or review.get("media_included") is not False
                    or review.get("restored") is not False
                    or review.get("activated") is not False
                    or assessment_identity(review) != review["assessment_identity"]
                    or self.pending_selection != self._selection()
                    or proof.get("corrupt_restore_inspection_format") != 2
                    or proof.get("data_root_identity") != review["data_root_identity"]
                    or proof.get("guard_retained") is not True
                    or not isinstance(proof.get("actual_state"), str)
                    or proof["actual_state"]
                    not in (
                        "SOURCE_NOT_MOVED_GUARDED",
                        "SOURCE_PARTIALLY_RETAINED_GUARDED",
                        "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
                        "EMPTY_CURRENT_DATABASE_GUARDED",
                        "VALIDATED_COPY_GUARDED",
                        "UNKNOWN_SOURCE_EVIDENCE",
                        "UNKNOWN_LIVE_SIDECARS",
                        "UNKNOWN_CURRENT_DATABASE",
                        "UNKNOWN_CURRENT_PHYSICAL_BYTES",
                    )
                    or not isinstance(proof.get("inspection_identity"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", proof["inspection_identity"])
                    is None
                    or type(review.get("media_issue_count")) is not int
                    or review["media_issue_count"] < 0
                    or type(review.get("unknown_continuation_allowed")) is not bool
                ):
                    raise ValueError("Invalid or stale copy review")
                inspection_fields = {
                    key: value
                    for key, value in proof.items()
                    if key != "inspection_identity"
                }
                if (
                    hashlib.sha256(
                        json.dumps(
                            inspection_fields, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                    ).hexdigest()
                    != proof["inspection_identity"]
                ):
                    raise ValueError("Invalid physical proof")
                state_text = {
                    "SOURCE_NOT_MOVED_GUARDED": "Nguồn hỏng còn ở vị trí cũ, chưa copy",
                    "SOURCE_PARTIALLY_RETAINED_GUARDED": "Một phần nguồn hỏng đã được giữ riêng",
                    "SOURCE_RETAINED_LIVE_MISSING_GUARDED": "Nguồn hỏng đã được giữ đủ, DB hiện tại đang thiếu",
                    "EMPTY_CURRENT_DATABASE_GUARDED": "Nguồn hỏng đã giữ đủ, partial target hiện tại đang rỗng",
                    "VALIDATED_COPY_GUARDED": "Bản copy thực tế đã xác minh; còn cần kiểm sức khỏe",
                }.get(
                    proof["actual_state"],
                    "Bytes hoặc inventory chưa rõ; giữ toàn bộ dữ liệu",
                )
                summary = [
                    f"Backup {review['backup_id']} — thời điểm {review['backup_created_at']}",
                    f"Schema {review['backup_schema']} → {review['target_schema']}; candidate {review['candidate_name']}",
                    f"Trạng thái: {state_text}. Guard vẫn chặn mở Library.",
                    "MỌI thay đổi DB sau backup có thể mất; không suy được đầy đủ thay đổi trong nguồn hỏng.",
                    "Backup chỉ chứa DB, không chứa media. Giữ originals/raw/partial/journals và bản cài cũ.",
                    f"Media cần xử lý: {review['media_issue_count']}; kiểm sức khỏe chỉ đạt khi mọi liên kết có đúng bytes.",
                ]
                for entry in proof["current_files"]:
                    summary.append(
                        f"Hiện tại {entry['name']}: {'có' if entry['present'] else 'vắng'}, {entry['byte_size']} bytes, SHA256 {entry['sha256']}"
                    )
                if proof["actual_state"].startswith("UNKNOWN_"):
                    summary.append(
                        "Tệp chưa rõ cần xác nhận giữ nguyên DB và các tệp phụ trước copy mới. Nguồn gốc thiếu/trùng hoặc copy hoàn tất rồi đổi bytes cần quyết định khôi phục mới."
                    )
                for media in review["media_assessment"]:
                    if media["status"] != "valid":
                        summary.append(
                            f"Media {media['file_id']} ({media['role']}): {media['status']}"
                        )
                self.review.setPlainText("\n".join(summary))
                self.copy_review = review
            except (OSError, ValueError, TypeError, KeyError):
                self._failed(
                    "Đánh giá copy không hợp lệ hoặc lựa chọn đã đổi; đánh giá lại trước xác nhận."
                )
                return
        elif self.operation == "inspect-restore":
            try:
                review = json.loads(stdout)
                if (
                    not isinstance(review, dict)
                    or review.get("data_root_identity")
                    != hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
                    or review.get("restore_assessment_format") != 1
                    or review.get("requires_lost_changes_confirmation") is not True
                    or review.get("media_included") is not False
                    or review.get("restored") is not False
                    or review.get("backup_id") != self.backup_id.text().strip()
                    or review.get("candidate_name")
                    != Path(self.candidate.text().strip()).name
                    or not isinstance(review.get("assessment_identity"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", review["assessment_identity"])
                    is None
                    or assessment_identity(review) != review["assessment_identity"]
                    or self.pending_selection != self._selection()
                    or type(review.get("media_issue_count")) is not int
                    or review["media_issue_count"] < 0
                ):
                    raise ValueError("Invalid or stale review")
                summary = [
                    f"Backup: {review['backup_id']} — thời điểm {review['backup_created_at']}",
                    f"Schema backup {review['backup_schema']} → {review['target_schema']}; hiện tại {review['current_schema']}.",
                    "MỌI thay đổi DB sau thời điểm này có thể mất. Số dòng không mô tả đầy đủ các sửa đổi.",
                    "Backup chỉ có DB; giữ media mới và mọi backup/bản cài cũ.",
                    f"Media cần xử lý: {review['media_issue_count']} (activation chỉ đạt khi bytes đúng).",
                ]
                for table, label in (
                    ("assets", "Asset"),
                    ("evidence_versions", "Evidence versions"),
                    ("claim_versions", "Claim versions"),
                    ("projects", "Project"),
                    ("draft_versions", "Draft versions"),
                    ("publication_packages", "Package"),
                    ("posts", "Post"),
                    ("observations", "Observation"),
                ):
                    summary.append(
                        f"{label}: hiện tại {review['current_counts'].get(table, 0)} / backup {review['backup_counts'].get(table, 0)}"
                    )
                for media in review["media_assessment"]:
                    if media["status"] != "valid":
                        summary.append(
                            f"Media {media['file_id']} ({media['role']}): {media['status']}; cần {media['expected_size']} bytes, SHA256 {media['expected_sha256']}"
                        )
                self.review.setPlainText("\n".join(summary))
                self.assessment = review
            except (ValueError, TypeError, KeyError):
                self._failed(
                    "Đánh giá không hợp lệ hoặc lựa chọn đã đổi; đánh giá lại trước khi xác nhận."
                )
                return
        else:
            self.review.setPlainText(stdout.strip())
        completion = {
            "review-corrupt-copy": "Đánh giá xong; chọn bước theo trạng thái hiển thị.",
            "resume-corrupt-copy": "Copy xong, guard vẫn giữ; đánh giá lại trước khôi phục và kiểm sức khỏe.",
            "recover-corrupt-copy": "Khôi phục xong; đối chiếu kết quả, guard và journal.",
        }.get(
            self.operation,
            "Lệnh đã kết thúc. Với recovery, kiểm journal để phân biệt NOT_APPLIED và COMPLETED.",
        )
        self.status.setText(f"{completion} Log: {self.command.log_directory}")
        self.operation = ""
        self._refresh()

    def _failed(self, message: str) -> None:
        if len(message) > 500:
            message = message[:500] + "… Chi tiết đầy đủ trong log được giữ."
        self.operation = ""
        self.assessment = None
        self.copy_review = None
        self.upgrade_consent.setChecked(False)
        self.loss_consent.setChecked(False)
        self.media_consent.setChecked(False)
        for consent in (self.empty_consent, self.unknown_consent, self.health_consent):
            consent.setChecked(False)
        self.status.setText(
            f"{message}\nLog được giữ tại: {self.command.log_directory}"
        )
        self._refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.damaged_window is not None:
            self.damaged_window.close()
        self.command.cancel()
        self.command.close()
        super().closeEvent(event)


def run_maintenance(
    root: Path, installation: Path | None = None, *, ui_smoke: bool = False
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MaintenanceWindow(root, installation)
    screen = QApplication.primaryScreen()
    if screen is not None:
        area = screen.availableGeometry()
        window.resize(min(1050, area.width() - 60), min(800, area.height() - 60))
    app.aboutToQuit.connect(window.command.close)
    window.show()
    if ui_smoke:
        QTimer.singleShot(200, window.close)
    return app.exec()
