"""Responsive Source collection, isolated browser ownership and bounded stop."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from creator_loop.facebook_browser import LOGIN_SECONDS, OPEN_SECONDS, SCAN_SECONDS
from creator_loop.facebook_sources import post_link, save_capture, saved_sources
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.owned_thread import OwnedThreadExit
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker
from creator_loop.windows_paths import file_io_path, resolve_file_path


def read_terminal(
    path: Path, run_id: str, exit_code: int, limit: int
) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 2 * 1024**2:
        raise ValueError("Invalid browser result")
    body = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(body, dict)
        or type(body.get("format")) is not int
        or body.get("format") != 1
        or body.get("run_id") != run_id
    ):
        raise ValueError("Browser result identity mismatch")
    codes = {
        "SUCCEEDED": 0,
        "SESSION_READY": 0,
        "CLOSED": 0,
        "CANCELLED": 2,
        "AUTH_REQUIRED": 3,
        "CAPTCHA": 3,
        "TIMED_OUT": 4,
        "BROWSER_FAILED": 5,
    }
    status = body.get("status")
    if status not in codes or exit_code != codes[status]:
        raise ValueError("Browser terminal status/exit mismatch")
    if status == "SUCCEEDED":
        links = body.get("links")
        if (
            not isinstance(links, list)
            or not 0 <= len(links) <= limit
            or body.get("count") != len(links)
        ):
            raise ValueError("Invalid bounded post result")
        ids = set()
        for item in links:
            if not isinstance(item, dict) or set(item) != {
                "url",
                "excerpt",
                "publisher",
            }:
                raise ValueError("Invalid post result")
            link = post_link(item["url"])
            if link is None or link.url != item["url"] or link.external_id in ids:
                raise ValueError("Invalid/duplicate permalink")
            ids.add(link.external_id)
            for key, budget in (("excerpt", 8192), ("publisher", 200)):
                if item[key] is not None and (
                    not isinstance(item[key], str) or not 1 <= len(item[key]) <= budget
                ):
                    raise ValueError("Invalid post text")
    return body


class FacebookWorker(QThread):
    progress = Signal(str)
    completed = Signal(str)

    def __init__(
        self, root: Path, mode: str, keyword: str, limit: int, url: str | None = None
    ) -> None:
        super().__init__()
        self.root = root
        self.mode = mode
        self.keyword = keyword
        self.limit = limit
        self.url = url
        self.workspace: Path | None = None
        self.session_ready = False

    def run(self) -> None:
        self.progress.emit("Đang mở trình duyệt riêng…")
        saved = duplicates = 0
        try:
            root = resolve_file_path(self.root, strict=True)
            for name in ("runtime", "logs"):
                folder = root / name
                folder.mkdir(exist_ok=True)
                if folder.is_symlink() or folder.is_junction():
                    raise OSError("Real runtime/log directories required")
            run_id = uuid4().hex
            workspace = root / "runtime" / ("facebook-" + run_id)
            workspace.mkdir()
            self.workspace = workspace
            component = "facebook-browser/1.63.0/chromium-1243"
            marker = create_workspace_marker(
                root, workspace, component, kind="FACEBOOK_BROWSER_WORKSPACE"
            )
            request = workspace / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "run_id": run_id,
                        "mode": self.mode,
                        "keyword": self.keyword,
                        "limit": self.limit,
                        "url": self.url,
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("PLAYWRIGHT_NODEJS_PATH", None)
            env.pop("PWDEBUG", None)
            env.pop("NODE_OPTIONS", None)
            env.pop("DEBUG", None)  # no Playwright diagnostic dumps/session secrets
            prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
            deadline = (
                time.monotonic()
                + (
                    {
                        "scan": SCAN_SECONDS,
                        "login": LOGIN_SECONDS,
                        "open": OPEN_SECONDS,
                    }[self.mode]
                )
                + 35
            )
            last = ""
            body: dict[str, Any] = {}
            with OwnedWindowsProcess(
                Path(sys.executable),
                [*prefix, "--facebook-browser-task", str(request)],
                root / "logs" / ("facebook-" + run_id),
                component_version=component,
                environment=env,
                capture_output=False,
                memory_limit_bytes=2 * 1024**3,
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
            ) as owned:
                stopped_at: float | None = None
                while True:
                    if self.isInterruptionRequested():
                        file_io_path(workspace / "stop").touch(exist_ok=True)
                        if stopped_at is None:
                            stopped_at = time.monotonic()
                        elif time.monotonic() - stopped_at > 3:
                            owned.request_stop(2)
                    if self.session_ready:
                        file_io_path(workspace / "ready").touch(exist_ok=True)
                    result = workspace / "result.json"
                    try:
                        if result.is_file() and result.stat().st_size < 2 * 1024**2:
                            raw = result.read_text(encoding="utf-8")
                            update = json.loads(raw)
                            if (
                                update.get("run_id") == run_id
                                and update.get("format") == 1
                            ):
                                body = update
                                if raw != last:
                                    last = raw
                                    status = str(body.get("status", ""))
                                    if status == "SCANNING":
                                        self.progress.emit(
                                            f"Đã tìm {body.get('count', 0)}/{self.limit} link; đang quét…"
                                        )
                                    elif status == "WAIT_LOGIN":
                                        self.progress.emit(
                                            "Đăng nhập trực tiếp trong trình duyệt riêng, rồi bấm Đã đăng nhập. Không gửi mật khẩu cho ứng dụng."
                                        )
                                    elif status == "OPENED":
                                        self.progress.emit(
                                            "Nguồn đang mở trong trình duyệt riêng; đóng cửa sổ hoặc bấm Dừng."
                                        )
                    except (OSError, ValueError):
                        pass  # bounded IPC replace/read race; DB is never inferred from this
                    outcome = owned.poll()
                    if outcome is not None:
                        break
                    if time.monotonic() >= deadline:
                        owned.stop(124)
                        raise TimeoutError()
                    self.msleep(100)
            # Retained Job is empty here. Re-read the final replacement after exit.
            if self.isInterruptionRequested():
                status = "CANCELLED"
            else:
                body = read_terminal(
                    workspace / "result.json", run_id, outcome.exit_code, self.limit
                )
                status = str(body["status"])
            if status == "SUCCEEDED":
                for item in body.get("links", []):
                    if self.isInterruptionRequested():
                        status = "CANCELLED"
                        break
                    _, new = save_capture(
                        root,
                        keyword=self.keyword,
                        url=item["url"],
                        excerpt=item.get("excerpt"),
                        publisher=item.get("publisher"),
                        cancelled=self.isInterruptionRequested,
                    )
                    saved += int(new)
                    duplicates += int(not new)
                    self.progress.emit(
                        f"Đã lưu {saved}; bỏ qua {duplicates} link trùng…"
                    )
            messages = {
                "SUCCEEDED": f"Hoàn tất: lưu {saved}, bỏ qua {duplicates} link trùng.",
                "CANCELLED": f"Đã dừng. Giữ {saved} tư liệu đã lưu; không đổi dữ liệu cũ.",
                "AUTH_REQUIRED": "Phiên hết hạn hoặc cần đăng nhập. Đã dừng; bấm Đăng nhập riêng.",
                "CAPTCHA": "Facebook yêu cầu xác minh/CAPTCHA. Đã dừng; không tự retry.",
                "TIMED_OUT": "Đã dừng vì hết thời gian quét.",
                "SESSION_READY": "Đã lưu phiên riêng. Có thể Quét.",
                "CLOSED": "Trình duyệt đã đóng.",
                "OPENED": "Nguồn đã đóng.",
                "BROWSER_FAILED": "Trình duyệt không hoàn tất. Đã đóng cây tiến trình riêng.",
            }
            self.completed.emit(messages.get(status, messages["BROWSER_FAILED"]))
        except InterruptedError:
            self.completed.emit(
                f"Đã dừng. Giữ {saved} tư liệu đã lưu; dữ liệu cũ không đổi."
            )
        except Exception:
            self.completed.emit(
                "Không thể hoàn tất; dữ liệu đã lưu được giữ. Kiểm tra trình duyệt/dependencies và thử lại khi có thay đổi."
            )
        finally:
            # Private request/results contain captured text; no cookies or credentials.
            # Leave owned receipts for diagnostics; do not recursively delete user data.
            pass


class FacebookDialog(QDialog):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.worker: FacebookWorker | None = None
        self.guard: OwnedThreadExit | None = None
        self.close_after = False
        self.setWindowTitle("Tư liệu Facebook — trình duyệt local riêng")
        self.resize(1050, 670)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Lưu link và trích đoạn nhìn thấy; không tải video/ảnh, không tự tạo Evidence ACCEPT."
            )
        )
        controls = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("Từ khóa tìm bài Facebook")
        self.keyword.setMaxLength(200)
        self.keyword.setObjectName("facebookKeyword")
        self.limit = QSpinBox()
        self.limit.setRange(1, 100)
        self.limit.setValue(20)
        self.limit.setObjectName("facebookLimit")
        controls.addWidget(self.keyword)
        controls.addWidget(self.limit)
        self.scan = QPushButton("Quét")
        self.scan.setObjectName("facebookScan")
        self.scan.clicked.connect(lambda: self.start("scan"))
        self.login = QPushButton("Đăng nhập riêng")
        self.login.clicked.connect(lambda: self.start("login"))
        self.ready = QPushButton("Đã đăng nhập")
        self.ready.setEnabled(False)
        self.ready.clicked.connect(self.login_ready)
        self.stop = QPushButton("Dừng")
        self.stop.setObjectName("facebookStop")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        for b in (self.scan, self.login, self.ready, self.stop):
            controls.addWidget(b)
        layout.addLayout(controls)
        self.status = QLabel("Sẵn sàng. Trình duyệt dùng phiên riêng trong data root.")
        self.status.setWordWrap(True)
        self.status.setObjectName("facebookStatus")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("facebookSources")
        self.table.setHorizontalHeaderLabels(
            ("URL bài thật", "Nguồn", "Trích đoạn", "Từ khóa", "Thu thập UTC")
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.open = QPushButton("Mở nguồn")
        self.open.clicked.connect(lambda: self.start("open"))
        layout.addWidget(self.open)
        close = QPushButton("Đóng")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.reap)
        self.reload()

    def reload(self) -> None:
        self.rows = saved_sources(self.root)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            for j, key in enumerate(
                ("canonical_url", "publisher_name", "excerpt", "keyword", "captured_at")
            ):
                item = QTableWidgetItem(str(row[key] or ""))
                item.setToolTip(str(row[key] or ""))
                self.table.setItem(i, j, item)
        self.table.setColumnWidth(0, 310)
        self.table.setColumnWidth(2, 300)

    def start(self, mode: str) -> None:
        if self.worker is not None:
            return
        keyword = self.keyword.text().strip()
        url = None
        if mode == "scan" and not keyword:
            self.status.setText("Nhập từ khóa trước khi Quét.")
            return
        if mode == "open":
            row = self.table.currentRow()
            if not 0 <= row < len(self.rows):
                self.status.setText("Chọn một nguồn đã lưu.")
                return
            url = str(self.rows[row]["canonical_url"])
        worker = FacebookWorker(self.root, mode, keyword, self.limit.value(), url)
        self.worker = worker
        self.guard = OwnedThreadExit()
        worker.started.connect(self.guard.capture, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(self.status.setText)
        worker.completed.connect(self.status.setText)
        for b in (self.scan, self.login, self.open):
            b.setEnabled(False)
        self.stop.setEnabled(True)
        self.ready.setEnabled(mode == "login")
        worker.start()
        self.timer.start()

    def login_ready(self) -> None:
        if self.worker is not None:
            self.worker.session_ready = True

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText("Đang đóng trình duyệt do ứng dụng sở hữu…")
            self.stop.setEnabled(False)

    def reap(self) -> None:
        if self.worker is None:
            return
        if (
            self.guard is None
            or self.guard.error
            or not self.guard.terminal()
            or not self.worker.wait(0)
        ):
            return
        self.guard.close()
        self.guard = None
        self.worker.wait(0)
        self.worker.deleteLater()
        self.worker = None
        self.timer.stop()
        self.ready.setEnabled(False)
        self.stop.setEnabled(False)
        for b in (self.scan, self.login, self.open):
            b.setEnabled(True)
        self.reload()
        if self.close_after:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None:
            self.close_after = True
            self.cancel()
            event.ignore()
        else:
            event.accept()
