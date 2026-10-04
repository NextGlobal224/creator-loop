"""Explicit damaged-source review/raw preservation/staging/guarded copy UI."""

from __future__ import annotations

import hashlib
import json
import re
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


class DamagedRestoreWindow(QMainWindow):
    copy_ready = Signal(str, str)

    def __init__(
        self,
        root: Path,
        installation: str = "",
        candidate: str = "",
        backup_id: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.root = root.resolve(strict=True)
        self.proof: dict[str, Any] | None = None
        self.operation = ""
        self.pending_selection: tuple[str, ...] = ()
        self.command = MaintenanceCommand(self.root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.setWindowTitle("Creator Loop — Khôi phục từ nguồn DB hỏng")
        self.resize(1000, 740)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            "Đóng Library và các tác vụ. Giữ nguyên nguồn hỏng và các tệp phụ. "
            "Backup chỉ có DB, không chứa media; raw archive không phải backup SQLite nhất quán.\n"
            f"Dữ liệu: {self.root}"
        )
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(notice)
        self.installation = QLineEdit(installation)
        self.candidate = QLineEdit(candidate)
        self.backup_id = QLineEdit(backup_id)
        self.raw = QLineEdit()
        self.preparation = QLineEdit()
        self.fields = [
            self.installation,
            self.candidate,
            self.backup_id,
            self.raw,
            self.preparation,
        ]
        form = QFormLayout()
        self.browse_buttons = []
        for label, field, kind in (
            ("Thư mục các bản cài", self.installation, "directory"),
            ("Candidate đã staging", self.candidate, "directory"),
            ("Backup DB đã chọn (ID)", self.backup_id, "backup"),
            ("Raw manifest đã giữ", self.raw, "file"),
            ("Manifest staging đã chọn", self.preparation, "file"),
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
        self.buttons = {}
        row = QHBoxLayout()
        for kind, title in (
            ("review", "Đánh giá nguồn / backup / raw / stage"),
            ("preserve", "Giữ nguyên raw source"),
            ("prepare", "Staging từ backup đã đánh giá"),
            ("copy", "Copy đã xác nhận và giữ guard"),
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, k=kind: self._action(k))
            self.buttons[kind] = button
            row.addWidget(button)
        layout.addLayout(row)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setMinimumHeight(200)
        layout.addWidget(self.review, 1)
        self.loss = QCheckBox(
            "Tôi chấp nhận mất MỌI thay đổi DB sau thời điểm backup đã hiển thị."
        )
        self.media = QCheckBox(
            "Tôi đã xem media thiếu / thay đổi; backup DB không phục hồi media bytes."
        )
        for consent in (self.loss, self.media):
            consent.toggled.connect(self._refresh)
            layout.addWidget(consent)
        self.cancel = QPushButton("Hủy tác vụ đang chạy")
        self.cancel.clicked.connect(self.command.cancel)
        layout.addWidget(self.cancel)
        self.status = QLabel(
            "Chọn backup và candidate cụ thể rồi đánh giá. Không chọn theo timestamp."
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
                self, "Chọn manifest", "", "Tất cả tệp (*)"
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
        self.loss.setChecked(False)
        self.media.setChecked(False)
        self.review.clear()
        self._refresh()

    def _refresh(self) -> None:
        busy = self.command.busy
        installation, candidate, backup, raw, prep = self._selection()
        proof = self.proof
        issues = bool(proof and proof["assessment"]["media_issue_count"])
        selected = bool(installation and candidate and backup)
        consent = bool(
            proof and self.loss.isChecked() and (not issues or self.media.isChecked())
        )
        for field in [*self.fields, *self.browse_buttons]:
            field.setEnabled(not busy)
        for kind, ready in {
            "review": selected,
            "preserve": bool(proof),
            "prepare": bool(
                selected
                and raw
                and proof
                and proof["raw_source"]
                and proof["raw_source"]["damage_identity"]
                == proof["assessment"]["damage"]["damage_identity"]
                and consent
            ),
            "copy": bool(
                selected and prep and proof and proof["copy_supported"] and consent
            ),
        }.items():
            self.buttons[kind].setEnabled(not busy and ready)
        self.loss.setEnabled(not busy and bool(proof))
        self.media.setEnabled(not busy and issues)
        self.cancel.setEnabled(busy)

    def _action(self, kind: str) -> None:
        if self.command.busy or not self.buttons[kind].isEnabled():
            return
        installation, candidate, backup, raw, prep = self._selection()
        proof = self.proof
        if kind == "review":
            args = [
                "--review-damaged-restore",
                backup,
                "--installation-root",
                installation,
                "--restore-candidate",
                candidate,
            ]
            if raw:
                args += ["--review-raw-source", raw]
            if prep:
                args += ["--review-damaged-preparation", prep]
        elif proof is None:
            return
        elif kind == "preserve":
            args = [
                "--preserve-corrupt-source",
                proof["assessment"]["damage"]["damage_identity"],
            ]
        else:
            args = (
                ["--prepare-corrupt-restore", backup, "--raw-source-manifest", raw]
                if kind == "prepare"
                else [
                    "--copy-corrupt-restore",
                    prep,
                    "--reviewed-preparation",
                    proof["preparation"]["manifest_sha256"],
                ]
            )
            args += [
                "--installation-root",
                installation,
                "--restore-candidate",
                candidate,
                "--reviewed-restore",
                proof["assessment"]["assessment_identity"],
                "--confirm-lost-changes",
            ]
            if self.media.isChecked():
                args.append("--confirm-media-issues")
        self.operation = kind
        self.pending_selection = self._selection()
        self._invalidate()
        try:
            self.command.start(args, timeout_seconds=300)
        except (OSError, ValueError, RuntimeError) as exc:
            self._failed(f"Không thể bắt đầu: {type(exc).__name__}")
        else:
            self.status.setText(
                "Đang thực hiện; hủy có thể để lại partial bytes/guard. Giữ mọi log và journal."
            )
            self._refresh()

    def _receipt_path(self, value: Any, kind: str) -> Path:
        if not isinstance(value, str):
            raise ValueError("Receipt path required")
        relative = Path(value)
        patterns = {
            "raw": r"backups/raw-source-[0-9a-f]{32}/raw-source-manifest\.json",
            "prepare": r"backups/corrupt-restore-[0-9a-f]{32}/preparation\.json",
            "copy": r"manifests/corrupt-restore-[0-9a-f]{32}\.json",
        }
        if (
            relative.drive
            or relative.root
            or re.fullmatch(patterns[kind], relative.as_posix()) is None
        ):
            raise ValueError("Receipt escaped data root")
        return self.root / relative

    def _finished(self, code: int, stdout: str, stderr: str) -> None:
        if code:
            self._failed(
                f"Chưa hoàn tất (exit{code}). {stderr.strip()} Giữ mọi bytes/guard/journal."
            )
            return
        try:
            result = json.loads(stdout)
            if (
                not isinstance(result, dict)
                or self.pending_selection != self._selection()
            ):
                raise ValueError("Invalid/stale result")
            if self.operation == "review":
                assessment = result["assessment"]
                if (
                    result.get("damaged_restore_review_format") != 1
                    or result.get("data_root_identity")
                    != hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
                    or result.get("installation_root")
                    != str(Path(self.installation.text().strip()).resolve(strict=True))
                    or result.get("candidate_directory")
                    != str(Path(self.candidate.text().strip()).resolve(strict=True))
                    or result.get("restored") is not False
                    or result.get("activated") is not False
                    or assessment_identity(result) != result["assessment_identity"]
                    or not isinstance(assessment, dict)
                    or assessment.get("restore_assessment_format") != 2
                    or assessment.get("backup_id") != self.backup_id.text().strip()
                    or assessment.get("data_root_identity")
                    != result["data_root_identity"]
                    or assessment_identity(assessment)
                    != assessment["assessment_identity"]
                    or assessment.get("current_changes_assessable") is not False
                    or assessment.get("current_counts") is not None
                    or assessment.get("current_schema") is not None
                    or assessment.get("source_kind") != "DAMAGED_SQLITE"
                    or assessment.get("restored") is not False
                    or assessment.get("apply_supported") is not False
                    or assessment.get("media_included") is not False
                    or type(assessment.get("media_issue_count")) is not int
                    or assessment["media_issue_count"] < 0
                    or not isinstance(assessment.get("damage"), dict)
                    or not isinstance(assessment["damage"].get("damage_identity"), str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}", assessment["damage"]["damage_identity"]
                    )
                    is None
                    or type(result.get("copy_supported")) is not bool
                ):
                    raise ValueError("Invalid review")
                raw_proof = result.get("raw_source")
                stage_proof = result.get("preparation")
                if raw_proof is not None and (
                    not isinstance(raw_proof, dict)
                    or raw_proof.get("archive_revalidated") is not True
                    or raw_proof.get("consistent_backup") is not False
                    or not isinstance(raw_proof.get("damage_identity"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw_proof["damage_identity"])
                    is None
                    or not isinstance(raw_proof.get("manifest_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw_proof["manifest_sha256"])
                    is None
                ):
                    raise ValueError("Invalid raw proof")
                if stage_proof is not None and (
                    not isinstance(stage_proof, dict)
                    or stage_proof.get("stage_revalidated") is not True
                    or type(stage_proof.get("current_binding_matches")) is not bool
                    or stage_proof.get("apply_authorized") is not False
                    or stage_proof.get("activated") is not False
                    or stage_proof.get("restored") is not False
                    or not isinstance(stage_proof.get("manifest_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", stage_proof["manifest_sha256"])
                    is None
                ):
                    raise ValueError("Invalid stage proof")
                if result["copy_supported"] != bool(
                    stage_proof and stage_proof["current_binding_matches"]
                ):
                    raise ValueError("Invalid copy readiness")
                if raw_proof is not None:
                    raw_path = self._receipt_path(raw_proof["manifest"], "raw")
                    if (
                        self.raw.text().strip()
                        and raw_path != Path(self.raw.text().strip()).absolute()
                    ):
                        raise ValueError("Raw proof differs from selected archive")
                if stage_proof is not None:
                    if (
                        raw_proof is None
                        or self._receipt_path(stage_proof["manifest"], "prepare")
                        != Path(self.preparation.text().strip()).absolute()
                    ):
                        raise ValueError(
                            "Stage proof differs from selected preparation"
                        )
                    if (
                        result["copy_supported"]
                        and raw_proof["damage_identity"]
                        != (assessment["damage"]["damage_identity"])
                    ):
                        raise ValueError(
                            "Stage cannot authorize another damaged source"
                        )
                media_entries = assessment["media_assessment"]
                if (
                    not isinstance(media_entries, list)
                    or any(
                        not isinstance(entry, dict)
                        or any(
                            not isinstance(entry.get(key), str)
                            for key in ("status", "file_id", "role")
                        )
                        for entry in media_entries
                    )
                    or sum(entry["status"] != "valid" for entry in media_entries)
                    != (assessment["media_issue_count"])
                ):
                    raise ValueError("Invalid media review")
                summary = [
                    f"Backup {assessment['backup_id']} — thời điểm {assessment['backup_created_at']}",
                    f"Schema {assessment['backup_schema']} → {assessment['target_schema']}; candidate {assessment['candidate_name']}",
                    "Không thể xác định đầy đủ thay đổi sẽ mất trong DB hỏng; MỌI thay đổi sau backup có thể mất.",
                    f"Media cần xử lý: {assessment['media_issue_count']}; giữ originals và media mới.",
                    "Raw bytes không phải consistent SQLite backup. Chưa copy/activate/khôi phục.",
                ]
                if raw_proof is not None:
                    summary.append("Raw archive đã kiểm bytes và identity; giữ nguyên.")
                    if (
                        raw_proof["damage_identity"]
                        != assessment["damage"]["damage_identity"]
                    ):
                        summary.append(
                            "Raw cũ không khớp nguồn hiện tại; giữ raw mới trước staging."
                        )
                if result["preparation"]:
                    summary.append(
                        "Stage khớp review hiện tại; cần xác nhận trước copy."
                        if result["copy_supported"]
                        else "Stage cũ còn hợp lệ nhưng không khớp nguồn/media hiện tại; cần preparation mới."
                    )
                for media in assessment["media_assessment"]:
                    if media["status"] != "valid":
                        summary.append(
                            f"Media {media['file_id']} ({media['role']}): {media['status']}"
                        )
                self.review.setPlainText("\n".join(summary))
                self.proof = result
            elif self.operation == "preserve":
                if (
                    result.get("raw_source_preserved") is not True
                    or result.get("consistent_backup") is not False
                    or result.get("restored") is not False
                    or result.get("restore_authorized") is not False
                ):
                    raise ValueError("Invalid raw receipt")
                self.raw.setText(
                    str(self._receipt_path(result["raw_source_manifest"], "raw"))
                )
                self.preparation.clear()
            elif self.operation == "prepare":
                if (
                    result.get("raw_source_preserved") is not True
                    or result.get("apply_authorized") is not False
                    or result.get("restored") is not False
                    or result.get("activated") is not False
                ):
                    raise ValueError("Invalid staging receipt")
                self.preparation.setText(
                    str(
                        self._receipt_path(
                            result["corrupt_restore_preparation"], "prepare"
                        )
                    )
                )
            elif self.operation == "copy":
                if (
                    result.get("requires_recovery_health") is not True
                    or result.get("activated") is not False
                    or result.get("restored") is not False
                    or result.get("phase") != "CORRUPT_DB_COMMITTED_GUARDED"
                ):
                    raise ValueError("Invalid guarded copy receipt")
                journal = self._receipt_path(result["corrupt_restore_journal"], "copy")
                self.copy_ready.emit(str(journal), self.installation.text().strip())
                self.close()
                return
            else:
                raise ValueError("Unknown operation")
        except (OSError, ValueError, TypeError, KeyError):
            self._failed(
                "Kết quả không hợp lệ hoặc lựa chọn đã đổi; đánh giá lại trước xác nhận."
            )
            return
        self.status.setText(
            f"Đã kết thúc. Sau raw/staging hãy đánh giá lại; guard copy cần fresh health riêng. Log: {self.command.log_directory}"
        )
        self.operation = ""
        self._refresh()

    def _failed(self, message: str) -> None:
        self._invalidate()
        self.operation = ""
        self.status.setText(f"{message[:500]}\nLog: {self.command.log_directory}")
        self._refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.command.cancel()
        self.command.close()
        super().closeEvent(event)
