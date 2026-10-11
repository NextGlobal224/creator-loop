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
    QComboBox,
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
from creator_loop.facebook_cdp import TARGET_ID
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
        "CONNECTION_REQUIRED": 7,
        "FACEBOOK_TAB_REQUIRED": 8,
        "SELECT_TAB": 0,
        "OPENED_KEEP_BROWSER": 0,
        "SESSION_UNCONFIRMED": 0,
    }
    status = body.get("status")
    if status not in codes or exit_code != codes[status]:
        raise ValueError("Browser terminal status/exit mismatch")
    if status == "SESSION_READY" and "target_id" in body:
        target = body["target_id"]
        if not isinstance(target, str) or not TARGET_ID.fullmatch(target):
            raise ValueError("Invalid connected target")
    if status == "SELECT_TAB":
        tabs = body.get("tabs")
        if not isinstance(tabs, list) or not 2 <= len(tabs) <= 50:
            raise ValueError("Invalid Facebook tab list")
        ids = set()
        for tab in tabs:
            if (
                not isinstance(tab, dict)
                or set(tab) != {"target_id", "title"}
                or not isinstance(tab["target_id"], str)
                or not TARGET_ID.fullmatch(tab["target_id"])
                or tab["target_id"] in ids
                or not isinstance(tab["title"], str)
                or len(tab["title"]) > 200
            ):
                raise ValueError("Invalid Facebook tab")
            ids.add(tab["target_id"])
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
        self,
        root: Path,
        mode: str,
        keyword: str,
        limit: int,
        url: str | None = None,
        transport: str = "private",
        target_id: str | None = None,
    ) -> None:
        super().__init__()
        self.root = root
        self.mode = mode
        self.keyword = keyword
        self.limit = limit
        self.url = url
        self.transport = transport
        self.target_id = target_id
        self.connection_result: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.session_ready = False

    def run(self) -> None:
        self.progress.emit(
            "Đang kết nối Chrome; chọn Allow nếu Chrome hỏi…"
            if self.transport == "existing"
            else "Đang mở trình duyệt riêng…"
        )
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
            component = "facebook-browser/1.63.0/" + (
                "existing-cdp" if self.transport == "existing" else "chromium-1243"
            )
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
                        "transport": self.transport,
                        "target_id": self.target_id,
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
                        "connect": LOGIN_SECONDS,
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
            self.connection_result = (
                body if body.get("status") == status else {"status": status}
            )
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
                "CANCELLED": f"Đã dừng. Giữ {saved} tư liệu đã lưu; Chrome đang mở của bạn được giữ nguyên.",
                "AUTH_REQUIRED": (
                    "Phiên Facebook đang mở cần xác thực lại. Đã dừng; Chrome được giữ nguyên."
                    if self.transport == "existing"
                    else "Phiên hết hạn hoặc cần đăng nhập. Đã dừng; bấm Đăng nhập riêng."
                ),
                "CAPTCHA": "Facebook yêu cầu xác minh/CAPTCHA. Đã dừng; không tự retry.",
                "TIMED_OUT": "Đã dừng vì hết thời gian quét.",
                "SESSION_READY": (
                    "Đã kết nối đúng tab Facebook trong Chrome đang mở. Có thể Quét."
                    if self.transport == "existing"
                    else "Đã lưu phiên riêng. Có thể Quét."
                ),
                "CLOSED": "Trình duyệt đã đóng.",
                "OPENED": "Nguồn đã đóng.",
                "BROWSER_FAILED": (
                    "Không kết nối được; Chrome của bạn vẫn mở. Kiểm tra Remote Debugging và chọn Allow khi Chrome hỏi."
                    if self.transport == "existing"
                    else "Trình duyệt không hoàn tất. Đã đóng cây tiến trình riêng."
                ),
                "SESSION_UNCONFIRMED": "Chưa xác nhận được phiên Facebook trên tab đã chọn. Chrome được giữ mở; chưa cho phép Quét.",
                "CONNECTION_REQUIRED": "Chưa kết nối được. Bật Remote Debugging tại chrome://inspect/#remote-debugging trong Chrome đang mở, rồi cho phép kết nối.",
                "FACEBOOK_TAB_REQUIRED": "Tab Facebook đã đóng hoặc chuyển sang trang khác. Giữ tab Facebook mở rồi kết nối lại.",
                "SELECT_TAB": "Có nhiều tab Facebook. Chọn đúng tab bên trên rồi bấm Kết nối Chrome đang mở.",
                "OPENED_KEEP_BROWSER": "Đã mở nguồn trong tab Facebook đã chọn; Chrome và phiên đăng nhập vẫn mở.",
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
        self.verified_target: str | None = None
        self.setWindowTitle("Tư liệu Facebook — trình duyệt local riêng")
        self.resize(1050, 670)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Lưu link và trích đoạn nhìn thấy; không tải video/ảnh, không tự tạo Evidence ACCEPT."
            )
        )
        connection = QHBoxLayout()
        self.browser_mode = QComboBox()
        self.browser_mode.setObjectName("facebookBrowserMode")
        self.browser_mode.addItem("Chrome đang mở", "existing")
        self.browser_mode.addItem("Trình duyệt riêng", "private")
        self.chrome_tabs = QComboBox()
        self.chrome_tabs.setObjectName("facebookChromeTab")
        self.chrome_tabs.addItem("Kết nối để chọn tab Facebook", None)
        self.chrome_tabs.currentIndexChanged.connect(
            lambda: setattr(self, "verified_target", None)
        )
        connection.addWidget(self.browser_mode)
        connection.addWidget(self.chrome_tabs, 1)
        layout.addLayout(connection)
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
        self.login = QPushButton("Kết nối Chrome đang mở")
        self.login.setObjectName("facebookConnect")
        self.login.clicked.connect(
            lambda: self.start(
                "connect" if self.browser_mode.currentData() == "existing" else "login"
            )
        )
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
        self.status = QLabel(
            "Sẵn sàng. Kết nối tab Facebook trong Chrome đang mở trước khi Quét."
        )
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
        self.open_source = QPushButton("Mở nguồn")
        self.open_source.setObjectName("facebookOpen")
        self.open_source.clicked.connect(lambda: self.start("open"))
        layout.addWidget(self.open_source)
        close = QPushButton("Đóng")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.reap)
        self.browser_mode.currentIndexChanged.connect(self.mode_changed)
        self.reload()

    def mode_changed(self) -> None:
        self.verified_target = None
        self.chrome_tabs.clear()
        self.chrome_tabs.addItem("Kết nối để chọn tab Facebook", None)
        existing = self.browser_mode.currentData() == "existing"
        self.chrome_tabs.setEnabled(existing)
        self.login.setText("Kết nối Chrome đang mở" if existing else "Đăng nhập riêng")
        self.status.setText(
            "Kết nối tab Facebook trong Chrome đang mở trước khi Quét."
            if existing
            else "Trình duyệt riêng dùng phiên trong data root."
        )

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
        transport = str(self.browser_mode.currentData())
        target_id = self.chrome_tabs.currentData() if transport == "existing" else None
        if mode == "open":
            row = self.table.currentRow()
            if not 0 <= row < len(self.rows):
                self.status.setText("Chọn một nguồn đã lưu.")
                return
            url = str(self.rows[row]["canonical_url"])
        if (
            transport == "existing"
            and mode in ("scan", "open")
            and (target_id is None or target_id != self.verified_target)
        ):
            self.status.setText(
                "Kết nối đúng tab Facebook trước khi Quét hoặc Mở nguồn."
            )
            return
        worker = FacebookWorker(
            self.root, mode, keyword, self.limit.value(), url, transport, target_id
        )
        self.worker = worker
        self.guard = OwnedThreadExit()
        worker.started.connect(self.guard.capture, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(self.status.setText)
        worker.completed.connect(self.status.setText)
        for b in (self.scan, self.login, self.open_source):
            b.setEnabled(False)
        self.stop.setEnabled(True)
        self.ready.setEnabled(mode == "login")
        self.browser_mode.setEnabled(False)
        self.chrome_tabs.setEnabled(False)
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
        if self.worker.transport == "existing":
            body = self.worker.connection_result
            if body.get("status") == "SELECT_TAB":
                self.chrome_tabs.clear()
                for tab in body["tabs"]:
                    self.chrome_tabs.addItem(
                        tab["title"] or "Facebook", tab["target_id"]
                    )
            elif body.get("status") == "SESSION_READY" and body.get("target_id"):
                self.chrome_tabs.clear()
                self.chrome_tabs.addItem("Tab Facebook đã kết nối", body["target_id"])
                self.verified_target = str(body["target_id"])
            elif body.get("status") in (
                "AUTH_REQUIRED",
                "CAPTCHA",
                "FACEBOOK_TAB_REQUIRED",
                "CONNECTION_REQUIRED",
                "SESSION_UNCONFIRMED",
            ):
                self.verified_target = None
        self.worker.deleteLater()
        self.worker = None
        self.timer.stop()
        self.ready.setEnabled(False)
        self.stop.setEnabled(False)
        self.browser_mode.setEnabled(True)
        self.chrome_tabs.setEnabled(self.browser_mode.currentData() == "existing")
        for b in (self.scan, self.login, self.open_source):
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
