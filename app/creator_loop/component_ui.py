"""Review and save explicitly supplied local component declarations."""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_loop.component_command import ComponentCommand
from creator_loop.local_components import (
    ComponentSpec,
    component_review_fingerprint,
    parse_component_manifest,
)
from creator_loop.paths import ensure_data_root


class ComponentWindow(QMainWindow):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root.resolve(strict=True)
        self.command = ComponentCommand(self.root, self)
        self.command.finished.connect(self._finished)
        self.command.failed.connect(self._failed)
        self.fingerprint: str | None = None
        self.review_path = ""
        self.reviewed_ids: tuple[str, ...] = ()
        self.operation = ""
        self.close_pending = False
        self.setWindowTitle("Creator Loop — Engine/model cục bộ")
        self.resize(1100, 650)
        central = QWidget()
        layout = QVBoxLayout(central)
        notice = QLabel(
            "Chọn file khai báo cho engine/model bạn đã có và được phép dùng cục bộ. "
            "Kiểm file, đối chiếu phiên bản/nguồn/license rồi lưu lựa chọn.\n"
            "Lượt kiểm này chưa xác nhận engine/model chạy tương thích. Dữ liệu: "
            + str(self.root)
        )
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        row = QHBoxLayout()
        self.manifest = QLineEdit()
        self.manifest.setPlaceholderText("File JSON khai báo component đã chọn")
        self.manifest.textChanged.connect(self._invalidate)
        self.browse = QPushButton("Chọn file…")
        self.browse.clicked.connect(self._browse)
        self.check = QPushButton("Kiểm file đã chọn")
        self.check.clicked.connect(self._check)
        row.addWidget(self.manifest, 1)
        row.addWidget(self.browse)
        row.addWidget(self.check)
        layout.addLayout(row)
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("Giới hạn bộ nhớ worker khi xử lý:"))
        self.worker_budget = QComboBox()
        self.worker_budget.addItem("512 MiB (mặc định)", 512)
        self.worker_budget.addItem("768 MiB (Whisper base CPU đã chọn)", 768)
        self.worker_budget.currentIndexChanged.connect(self._invalidate)
        budget_row.addWidget(self.worker_budget)
        budget_row.addStretch()
        layout.addLayout(budget_row)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Component",
                "Loại",
                "Phiên bản khai báo",
                "File cục bộ",
                "Nguồn",
                "License",
                "SHA-256",
                "Bytes",
            ]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self._size_columns()
        layout.addWidget(self.table)
        self.consent = QCheckBox(
            "Tôi đã đối chiếu file, phiên bản, nguồn/license và quyền dùng cục bộ của lựa chọn này"
        )
        self.consent.toggled.connect(self._refresh)
        layout.addWidget(self.consent)
        buttons = QHBoxLayout()
        self.save = QPushButton("Kiểm lại và lưu lựa chọn")
        self.save.clicked.connect(self._save)
        self.cancel = QPushButton("Hủy tác vụ")
        self.cancel.clicked.connect(self.command.cancel)
        buttons.addWidget(self.save)
        buttons.addWidget(self.cancel)
        self.inspect = QPushButton("Xem lựa chọn đã lưu")
        self.inspect.clicked.connect(self._inspect)
        buttons.addWidget(self.inspect)
        self.runtime_check = QPushButton("Kiểm phiên bản engine đã lưu")
        self.runtime_check.clicked.connect(self._check_runtime)
        buttons.addWidget(self.runtime_check)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = QLabel(
            "Chưa có lựa chọn được kiểm. Hướng dẫn khai báo: docs/LOCAL_COMPONENTS.md."
        )
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self._refresh()

    def _browse(self) -> None:
        path, _kind = QFileDialog.getOpenFileName(
            self, "Chọn khai báo component cục bộ", "", "JSON (*.json)"
        )
        if path:
            self.manifest.setText(path)

    def _invalidate(self, *_args: object) -> None:
        self.fingerprint = None
        self.review_path = ""
        self.reviewed_ids = ()
        self.consent.setChecked(False)
        self.table.setRowCount(0)
        self._refresh()

    def _refresh(self, *_args: object) -> None:
        busy = self.command.busy
        self.manifest.setEnabled(not busy)
        self.worker_budget.setEnabled(not busy)
        self.browse.setEnabled(not busy)
        self.check.setEnabled(not busy and bool(self.manifest.text().strip()))
        self.consent.setEnabled(not busy and self.fingerprint is not None)
        self.save.setEnabled(
            not busy and self.fingerprint is not None and self.consent.isChecked()
        )
        self.cancel.setEnabled(busy)
        self.inspect.setEnabled(not busy)
        self.runtime_check.setEnabled(not busy)

    def _start(self, operation: str, arguments: list[str]) -> None:
        self.operation = operation
        if operation in ("check", "save"):
            arguments = [
                *arguments,
                "--component-worker-memory-mib",
                str(self.worker_budget.currentData()),
            ]
        try:
            self.command.start(arguments)
        except (OSError, RuntimeError, ValueError) as exc:
            self._failed(f"Không khởi chạy được: {type(exc).__name__}")
            return
        self.status.setText("Đang kiểm trong tác vụ riêng…")
        self._refresh()

    def _check(self) -> None:
        path = self.manifest.text().strip()
        self._invalidate()
        self.review_path = path
        self._start("check", ["--check-components", path])

    def _save(self) -> None:
        if not self.save.isEnabled() or self.fingerprint is None:
            return
        fingerprint = self.fingerprint
        path = self.review_path
        self.consent.setChecked(False)
        self._start(
            "save", ["--select-components", path, "--reviewed-components", fingerprint]
        )

    def _inspect(self) -> None:
        self._invalidate()
        self._start("inspect", ["--inspect-components"])

    def _check_runtime(self) -> None:
        self._invalidate()
        self._start("runtime", ["--check-whisper-runtime"])

    def _display(self, specs: tuple[ComponentSpec, ...]) -> None:
        self.table.setRowCount(len(specs))
        for row, spec in enumerate(specs):
            values = [
                spec.component_id,
                spec.kind,
                spec.version,
                str(spec.path),
                spec.source_url,
                spec.license,
                spec.sha256,
                str(spec.byte_size),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                tip = value + "\n" + spec.license_url if column == 5 else value
                item.setToolTip("<qt>" + escape(tip).replace("\n", "<br>") + "</qt>")
                self.table.setItem(row, column, item)
        self._size_columns()

    def _size_columns(self) -> None:
        header = self.table.horizontalHeader()
        for column, width in enumerate((110, 70, 140, 220, 150, 150, 140, 90)):
            self.table.setColumnWidth(
                column, max(width, header.sectionSizeHint(column))
            )

    def _finished(self, code: int, raw: str) -> None:
        if self.close_pending:
            self.close()
            return
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Component result must be an object")
            if code != 0:
                message = body.get("reason", "Lựa chọn không đạt; kiểm lại khai báo.")
                self._failed(str(message))
                return
            if self.operation == "check":
                specs = parse_component_manifest(
                    json.dumps(body["review_manifest"]).encode("utf-8")
                )
                fingerprint = component_review_fingerprint(specs)
                if (
                    body["check"] != "LOCAL_ARTIFACTS_VERIFIED"
                    or body["runtime_compatibility_verified"] is not False
                    or body["review_fingerprint"] != fingerprint
                    or type(body["worker_memory_limit"]) is not int
                    or body["worker_memory_limit"]
                    != self.worker_budget.currentData() * 1024**2
                    or self.review_path != self.manifest.text().strip()
                ):
                    raise ValueError("Review changed or result mismatch")
                self._display(specs)
                self.fingerprint = fingerprint
                self.reviewed_ids = tuple(spec.component_id for spec in specs)
                self.status.setText(
                    "Bytes/digest file đã khớp. Đối chiếu các khai báo trước khi lưu; chưa kiểm khả năng chạy engine/model."
                )
            elif self.operation == "inspect":
                if (
                    body["freshly_verified"] is not False
                    or body["runtime_compatibility_verified"] is not False
                ):
                    raise ValueError("History cannot grant compatibility")
                if body["selection"] == "HISTORY":
                    specs = parse_component_manifest(
                        json.dumps(body["manifest"]).encode("utf-8")
                    )
                    self._display(specs)
                    self.status.setText(
                        "Lựa chọn đã lưu, mốc kiểm "
                        + body["checked_at"]
                        + ". Chưa kiểm lại file hiện tại; chọn khai báo và kiểm lại trước khi lưu/xử lý."
                    )
                elif body["selection"] == "NOT_CONFIGURED":
                    self.status.setText("Chưa lưu lựa chọn component cho dữ liệu này.")
                else:
                    raise ValueError("Invalid historical selection")
            elif self.operation == "runtime":
                if (
                    body["check"] != "ENGINE_CLI_VERSION_CONFIRMED"
                    or body["engine_version"] != "1.8.7"
                    or body["pe_amd64_verified"] is not True
                    or body["model_executed"] is not False
                    or body["model_compatibility_verified"] is not False
                    or body["runtime_compatibility_verified"] is not False
                ):
                    raise ValueError("Version startup cannot grant compatibility")
                self.status.setText(
                    "Engine đã khởi động và báo phiên bản 1.8.7. "
                    "Chưa chạy model; chưa xác nhận tương thích, chất lượng hoặc máy 8 GB."
                )
            elif (
                self.operation == "save"
                and body["selection"] == "SAVED"
                and body["runtime_compatibility_verified"] is False
                and body["component_ids"] == list(self.reviewed_ids)
                and type(body["worker_memory_limit"]) is int
                and body["worker_memory_limit"]
                == self.worker_budget.currentData() * 1024**2
            ):
                self.status.setText(
                    "Đã lưu lựa chọn tại "
                    + body["checked_at"]
                    + ". Khi xử lý phải kiểm lại file; engine/model chưa được nghiệm thu chạy."
                )
                self.fingerprint = None
            else:
                raise ValueError("Unexpected component result")
        except (ValueError, TypeError, KeyError, RecursionError):
            self._failed("Kết quả không hợp lệ; kiểm lại lựa chọn trước khi lưu.")
            return
        self.operation = ""
        self._refresh()

    def _failed(self, reason: str) -> None:
        self._invalidate()
        self.operation = ""
        self.status.setText(reason[:500] + "\nLog: " + str(self.command.log_directory))
        self._refresh()
        if self.close_pending and not self.command.busy:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.command.busy:
            self.close_pending = True
            self.command.cancel()
            self.status.setText("Đang chờ tác vụ đã hủy thoát trước khi đóng…")
            event.ignore()
            return
        super().closeEvent(event)


def run_components(root: Path, *, ui_smoke: bool = False) -> int:
    ensure_data_root(root)  # coordination folders only; no DB initialize or migration
    app = QApplication.instance() or QApplication(sys.argv)
    window = ComponentWindow(root)
    app.aboutToQuit.connect(window.command.shutdown)
    screen = QApplication.primaryScreen()
    if screen is not None:
        area = screen.availableGeometry()
        window.resize(min(1100, area.width() - 60), min(650, area.height() - 60))
    window.show()
    if ui_smoke:
        QTimer.singleShot(200, window.close)
    return app.exec()
