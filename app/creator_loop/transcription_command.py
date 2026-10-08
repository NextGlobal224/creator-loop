"""Nonblocking owned task with cooperative cancellation and bounded fallback."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import PySide6
from PySide6.QtCore import Signal

from creator_loop.component_command import MAX_OUTPUT, ComponentCommand
from creator_loop.owned_process import _ExtendedLimits
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker
from creator_loop.transcription_task import (
    TASK_COMPONENT,
    TASK_JOB_MEMORY,
    TASK_TIMEOUT,
)
from creator_loop.windows_owned_file import OwnedWindowsFile
from creator_loop.windows_paths import file_io_path


class TranscriptionCommand(ComponentCommand):
    phase_changed = Signal(str)

    def __init__(self, root: Path, parent=None) -> None:
        super().__init__(root, parent)
        self.workspace: Path | None = None
        self.request_id = ""
        self.cancel_deadline: float | None = None
        self.last_phase = ""
        self.peak_job_commit_bytes = 0
        self.leases = ExitStack()
        self.finished.connect(self._release_terminal)
        self.failed.connect(self._release_terminal)

    def _release_terminal(self, *_args: object) -> None:
        if not self.busy:
            self.leases.close()

    def start_task(
        self, file_id: str, *, language: str = "vi", worker_memory_mib: int = 768
    ) -> None:
        if self.busy:
            raise RuntimeError("Transcription task already running")
        if (
            re.fullmatch("[0-9a-f]{32}", file_id) is None
            or re.fullmatch("[a-z]{2}", language) is None
            or type(worker_memory_mib) is not int
            or worker_memory_mib not in (512, 768)
        ):
            raise ValueError("Invalid bounded task settings")
        self.leases = ExitStack()
        self.request_id = uuid4().hex
        self.workspace = self.root / "runtime" / ("transcribe-" + self.request_id)
        try:
            for path in (self.root, self.root / "runtime"):
                self.leases.enter_context(
                    RuntimeHandle(path, directory=True, allow_child_writes=True)
                )
            file_io_path(self.workspace).mkdir()
            self.leases.enter_context(
                RuntimeHandle(self.workspace, directory=True, allow_child_writes=True)
            )
            marker = create_workspace_marker(
                self.root,
                self.workspace,
                TASK_COMPONENT,
                kind="VIDEO_TRANSCRIPTION_TASK",
            )
            request = self.workspace / "request.json"
            with file_io_path(request).open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "format": 1,
                        "request_id": self.request_id,
                        "root": str(self.root),
                        "file_id": file_id,
                        "language": language,
                        "worker_memory_mib": worker_memory_mib,
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            self.leases.enter_context(RuntimeHandle(request, read_only=True))
            environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(self.root)}
            prefix: list[str] = []
            executable = Path(sys.executable)
            if not getattr(sys, "frozen", False):
                # Avoid the venv launcher indirection: native child binding must
                # identify the executor itself, including its creation FILETIME.
                executable = Path(getattr(sys, "_base_executable", sys.executable))
                prefix = ["-m", "creator_loop"]
                environment.pop("PYTHONHOME", None)
                environment["PYTHONPATH"] = os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[1]),
                        str(Path(PySide6.__file__).resolve().parent.parent),
                    )
                )
            self.cancel_deadline = None
            self.last_phase = ""
            self.peak_job_commit_bytes = 0
            workspace = self.workspace
            self._launch(
                executable,
                [*prefix, "--transcribe-video-task", str(request)],
                environment,
                timeout_seconds=TASK_TIMEOUT,
                memory_limit_bytes=TASK_JOB_MEMORY,
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
                component_version=TASK_COMPONENT,
                log_prefix="video-transcription",
            )
        except BaseException:
            self.leases.close()
            raise  # request/log/witness files retained; no guessed cleanup.

    def cancel(self) -> None:
        if not self.busy or self.cancel_deadline is not None:
            return
        assert self.workspace is not None
        try:
            owned = OwnedWindowsFile.create_new(self.workspace / "cancel.json")
            try:
                payload = json.dumps(
                    {"format": 1, "request_id": self.request_id, "cancel": True}
                ).encode("utf-8")
                if owned.stream.write(payload) != len(payload):
                    raise OSError("Incomplete cancellation record")
                owned.stream.flush()
                os.fsync(owned.stream.fileno())
            finally:
                owned.close()
            self.cancel_deadline = time.perf_counter() + 12
            self.phase_changed.emit("cancelling")
        except (OSError, ValueError):
            self._terminate(
                "Không gửi được yêu cầu hủy; đang dừng cây tác vụ owned, giữ dữ liệu/log."
            )

    def _poll(self) -> None:
        process = self.process
        if process is not None:
            try:
                if sys.platform != "win32":
                    raise OSError("Task process metrics require Windows")
                if process.tree_finished():
                    metrics = _ExtendedLimits()
                    if not process.kernel.QueryInformationJobObject(
                        process.job,
                        9,
                        ctypes.byref(metrics),
                        ctypes.sizeof(metrics),
                        None,
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.peak_job_commit_bytes = metrics.peak_job
                with file_io_path(process.stdout_path).open("rb") as stream:
                    raw = stream.read(MAX_OUTPUT + 1)
                if len(raw) <= MAX_OUTPUT:
                    latest_phase = self.last_phase
                    for line in raw.splitlines():
                        try:
                            body = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue  # partial line while child is writing.
                        if (
                            isinstance(body, dict)
                            and body.get("event") == "phase"
                            and body.get("request_id") == self.request_id
                            and body.get("phase") in ("decode", "transcribe")
                        ):
                            latest_phase = body["phase"]
                    if latest_phase != self.last_phase:
                        self.last_phase = latest_phase
                        if self.cancel_deadline is None:
                            self.phase_changed.emit(latest_phase)
                if (
                    self.cancel_deadline is not None
                    and time.perf_counter() >= self.cancel_deadline
                    and not process.tree_finished()
                ):
                    self._terminate(
                        "Hủy không hoàn tất trong thời gian cho phép; giữ RAW/log, cần kiểm trạng thái run khi mở lại."
                    )
            except (OSError, ValueError, RecursionError):
                self._terminate(
                    "Không đọc được tiến độ; giữ dữ liệu và chờ cây tác vụ thoát."
                )
        super()._poll()

    def shutdown(self) -> None:
        try:
            super().shutdown()
        finally:
            self.leases.close()
