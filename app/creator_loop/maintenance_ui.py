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
        self.setCentralWidget(central)
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
        self.loss_consent.setChecked(False)
        self.media_consent.setChecked(False)
        self.upgrade_consent.setChecked(False)
        self.review.clear()
        self._refresh()

    def _refresh(self) -> None:
        busy = self.command.busy
        installation, zip_path, manifest, candidate, backup_id, journal = (
            self._selection()
        )
        for field in [*self.fields, *self.browse_buttons]:
            field.setEnabled(not busy)
        for kind, button in self.buttons.items():
            ready = {
                "backup": True,
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
            }[kind]
            button.setEnabled(not busy and ready)
        media_issues = bool(self.assessment and self.assessment["media_issue_count"])
        self.upgrade_consent.setEnabled(not busy)
        self.loss_consent.setEnabled(not busy and self.assessment is not None)
        self.media_consent.setEnabled(not busy and media_issues)
        self.apply_button.setEnabled(
            not busy
            and self.assessment is not None
            and self.loss_consent.isChecked()
            and (not media_issues or self.media_consent.isChecked())
        )
        self.cancel_button.setEnabled(busy)

    def _action(self, kind: str) -> None:
        installation, zip_path, manifest, candidate, backup_id, journal = (
            self._selection()
        )
        args = [f"--{kind}"]
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
        if self.operation == "inspect-restore":
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
        self.status.setText(
            f"Lệnh đã kết thúc. Với recovery, kiểm journal để phân biệt NOT_APPLIED và COMPLETED. Log: {self.command.log_directory}"
        )
        self.operation = ""
        self._refresh()

    def _failed(self, message: str) -> None:
        if len(message) > 500:
            message = message[:500] + "… Chi tiết đầy đủ trong log được giữ."
        self.operation = ""
        self.assessment = None
        self.loss_consent.setChecked(False)
        self.media_consent.setChecked(False)
        self.status.setText(
            f"{message}\nLog được giữ tại: {self.command.log_directory}"
        )
        self._refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
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
