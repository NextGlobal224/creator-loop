"""Component CLI runner with nonblocking owned-tree cancellation."""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from creator_loop import __version__
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.windows_paths import file_io_path

MAX_OUTPUT = 128 * 1024


class ComponentCommand(QObject):
    finished = Signal(int, str)
    failed = Signal(str)

    def __init__(self, root: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = root.resolve(strict=True)
        self.process: OwnedWindowsProcess | None = None
        self.log_directory: Path | None = None
        self.deadline = 0.0
        self.cleanup_deadline: float | None = None
        self.failure: str | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._poll)

    @property
    def busy(self) -> bool:
        return self.process is not None

    def start(self, arguments: list[str], *, timeout_seconds: float = 180) -> None:
        environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(self.root)}
        prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
        self._launch(
            Path(sys.executable),
            [*prefix, *arguments],
            environment,
            timeout_seconds=timeout_seconds,
        )

    def _launch(
        self,
        executable: Path,
        arguments: list[str],
        environment: dict[str, str],
        *,
        timeout_seconds: float,
        memory_limit_bytes: int = 256 * 1024**2,
        before_resume: Callable[[dict[str, object]], None] | None = None,
        component_version: str = __version__,
        log_prefix: str = "component-check",
        capture_output: bool = True,
        stdout_sink: BinaryIO | None = None,
    ) -> None:
        if self.busy:
            raise RuntimeError("Component command already running")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("Component command requires a bounded deadline")
        logs = self.root / "logs"
        if not logs.is_dir() or logs.is_symlink() or logs.is_junction():
            raise ValueError("Real logs folder required")
        self.log_directory = logs / f"{log_prefix}-{uuid4().hex}"
        self.process = OwnedWindowsProcess(
            executable,
            arguments,
            self.log_directory,
            component_version=component_version,
            environment=environment,
            memory_limit_bytes=memory_limit_bytes,
            before_resume=before_resume,
            capture_output=capture_output,
            stdout_sink=stdout_sink,
        )
        self.failure = None
        self.cleanup_deadline = None
        self.deadline = time.perf_counter() + timeout_seconds
        self.timer.start()

    def cancel(self) -> None:
        self._terminate(
            "Đã hủy. Lựa chọn có thể đã lưu trước khi hủy; kiểm lại trạng thái trước khi thử lại."
        )

    def _terminate(self, reason: str | None) -> None:
        if self.process is None or self.cleanup_deadline is not None:
            return
        self.failure = reason
        self.cleanup_deadline = time.perf_counter() + 8
        try:
            self.process.request_stop(124 if reason else 0)
        except OSError:
            self.failure = "Không gửi được yêu cầu hủy; cleanup chưa được xác minh."
            self.failed.emit(
                "Không gửi được yêu cầu hủy; giữ ownership/log và chờ xác minh tác vụ thoát."
            )

    def _poll(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.tree_finished():
                outcome = process.poll()
                assert outcome is not None
                stdout, stderr = process.stdout_path, process.stderr_path
                process.close()
                self.process = None
                self.timer.stop()
                if self.failure is not None:
                    self.failed.emit(self.failure)
                    return
                with file_io_path(stdout).open("rb") as stream:
                    raw = stream.read(MAX_OUTPUT + 1)
                if (
                    len(raw) > MAX_OUTPUT
                    or file_io_path(stderr).stat().st_size > MAX_OUTPUT
                ):
                    raise ValueError("Component output budget exceeded")
                self.finished.emit(
                    outcome.exit_code, raw.decode("utf-8", errors="strict")
                )
                return
            if self.cleanup_deadline is not None:
                if time.perf_counter() >= self.cleanup_deadline:
                    self.cleanup_deadline = float(
                        "inf"
                    )  # keep retained ownership, warn only once
                    self.failed.emit(
                        "Chưa xác minh tác vụ đã thoát; giữ cửa sổ và log, không tiếp tục lưu."
                    )
                return
            if process.kernel.WaitForSingleObject(process.process, 0) == 0:
                self._terminate(
                    None
                )  # root ended; retire any remaining owned descendants
            elif any(
                file_io_path(path).stat().st_size > MAX_OUTPUT
                for path in (process.stdout_path, process.stderr_path)
            ):
                self._terminate("Kết quả vượt giới hạn; kiểm lại file khai báo.")
            elif time.perf_counter() >= self.deadline:
                self._terminate(
                    "Hết thời gian kiểm; lựa chọn chưa xác nhận, đọc lại trạng thái trước khi thử lại."
                )
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            if self.process is not None:
                self._terminate(f"Không đọc được kết quả: {type(exc).__name__}")
            else:
                self.timer.stop()
                self.failed.emit(f"Không đọc được kết quả: {type(exc).__name__}")

    def shutdown(self) -> None:
        """Bounded fallback at app event-loop exit, not ordinary GUI cancel."""
        self.timer.stop()
        if self.process is not None:
            self.process.close()
            self.process = None
