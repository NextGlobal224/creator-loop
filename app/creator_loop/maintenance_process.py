"""Responsive maintenance CLI execution with owned Windows trees and deadlines."""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from creator_loop import __version__
from creator_loop.owned_process import OwnedWindowsProcess

MAX_OUTPUT_BYTES = 1024 * 1024


class MaintenanceCommand(QObject):
    """Keep a retained process/Job handle; never kill a recorded PID."""

    finished = Signal(int, str, str)
    failed = Signal(str)

    def __init__(self, root: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = root.resolve(strict=True)
        self.process: OwnedWindowsProcess | None = None
        self.log_directory: Path | None = None
        self.deadline = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)

    @property
    def busy(self) -> bool:
        return self.process is not None

    def start(self, arguments: list[str], *, timeout_seconds: float = 180) -> None:
        if self.busy:
            raise RuntimeError("Maintenance command already running")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("Maintenance deadline must be positive and at most 600s")
        logs = self.root / "logs"
        if logs.is_symlink() or logs.is_junction() or not logs.is_dir():
            raise ValueError("Real user-data logs directory required")
        environment = os.environ.copy()
        environment["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        executable = Path(sys.executable)
        prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
        self.log_directory = logs / f"maintenance-{uuid4().hex}"
        self.process = OwnedWindowsProcess(
            executable,
            [*prefix, *arguments],
            self.log_directory,
            component_version=__version__,
            environment=environment,
        )
        self.deadline = time.monotonic() + timeout_seconds
        self.timer.start()

    def cancel(self) -> None:
        if self.busy:
            self._fail(
                "Maintenance cancelled; preserve backups/journals and inspect state"
            )

    def close(self) -> None:
        self.timer.stop()
        process, self.process = self.process, None
        if process is not None:
            process.close()

    def _fail(self, message: str) -> None:
        try:
            self.close()
        except (OSError, RuntimeError, TimeoutError):
            message = (
                "Owned command cleanup incomplete; preserve logs and recovery state"
            )
        self.failed.emit(message)

    def _read_output(self, path: Path) -> str:
        with path.open("rb") as stream:
            content = stream.read(MAX_OUTPUT_BYTES + 1)
        if len(content) > MAX_OUTPUT_BYTES:
            raise ValueError("Maintenance output exceeds the review budget")
        return content.decode("utf-8", errors="strict")

    def _poll(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            # A terminal process wins over an observation delayed by a busy UI.
            outcome = process.poll()
            if outcome is None:
                if any(
                    path.stat().st_size > MAX_OUTPUT_BYTES
                    for path in (process.stdout_path, process.stderr_path)
                ):
                    self._fail(
                        "Maintenance output exceeds the review budget; inspect retained logs"
                    )
                    return
                if time.monotonic() >= self.deadline:
                    self._fail(
                        "Maintenance timed out; preserve backups/journals and inspect state"
                    )
                return
            stdout_path, stderr_path = process.stdout_path, process.stderr_path
            self.close()
            stdout = self._read_output(stdout_path)
            stderr = self._read_output(stderr_path)
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            self._fail(f"Maintenance result unavailable: {type(exc).__name__}")
        else:
            self.finished.emit(outcome.exit_code, stdout, stderr)
