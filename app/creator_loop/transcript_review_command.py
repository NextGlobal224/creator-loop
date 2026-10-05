"""GUI facade for owned historical RAW I/O over a bounded private pipe."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import PySide6

from creator_loop.component_command import ComponentCommand
from creator_loop.private_pipe import PrivatePipe
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker
from creator_loop.transcript_review_task import (
    MAX_RESPONSE,
    REVIEW_COMPONENT,
    REVIEW_MEMORY,
    REVIEW_TIMEOUT,
    _validate_request,
)

_RETAINED_COMMANDS: list[object] = []


class TranscriptReviewCommand(ComponentCommand):
    def __init__(self, root: Path, parent=None) -> None:
        super().__init__(root, parent)
        self.pipe: PrivatePipe | None = None
        self.received = bytearray()
        self.request_id = ""
        self.action = ""
        self.workspace: Path | None = None
        self.leases = ExitStack()
        self.finished.connect(self._release_terminal)
        self.failed.connect(self._release_terminal)

    def _release_terminal(self, *_args: object) -> None:
        if not self.busy:
            self.leases.close()

    def start_review(
        self,
        raw_file_id: str,
        *,
        action: str = "page",
        index: int = 0,
        raw_sha256: str | None = None,
    ) -> None:
        if self.busy or self.pipe is not None:
            raise RuntimeError(
                "Transcript review already running or cleanup unconfirmed"
            )
        request_id = uuid4().hex
        body = {
            "format": 1,
            "request_id": request_id,
            "root": str(self.root),
            "raw_file_id": raw_file_id,
            "action": action,
            "index": index,
            "raw_sha256": raw_sha256,
        }
        _validate_request(body, self.root, request_id)
        self.request_id, self.action = request_id, action
        self.received = bytearray()
        self.leases = ExitStack()
        self.workspace = self.root / "runtime" / ("transcript-review-" + request_id)
        try:
            for path in (self.root, self.root / "runtime"):
                self.leases.enter_context(
                    RuntimeHandle(path, directory=True, allow_child_writes=True)
                )
            self.workspace.mkdir()
            self.leases.enter_context(
                RuntimeHandle(self.workspace, directory=True, allow_child_writes=True)
            )
            marker = create_workspace_marker(
                self.root,
                self.workspace,
                REVIEW_COMPONENT,
                kind="TRANSCRIPT_REVIEW_TASK",
            )
            request = self.workspace / "request.json"
            with request.open("x", encoding="utf-8") as stream:
                json.dump(body, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            self.leases.enter_context(RuntimeHandle(request, read_only=True))
            environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(self.root)}
            executable = Path(sys.executable)
            prefix: list[str] = []
            if not getattr(sys, "frozen", False):
                executable = Path(getattr(sys, "_base_executable", sys.executable))
                prefix = ["-m", "creator_loop"]
                environment.pop("PYTHONHOME", None)
                environment["PYTHONPATH"] = os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[1]),
                        str(Path(PySide6.__file__).resolve().parent.parent),
                    )
                )
            self.pipe = PrivatePipe(eager_reads=True)
            workspace = self.workspace
            self._launch(
                executable,
                [*prefix, "--transcript-review-task", str(request)],
                environment,
                timeout_seconds=REVIEW_TIMEOUT,
                memory_limit_bytes=(
                    512 * 1024**2 if action == "preview" else REVIEW_MEMORY
                ),
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
                component_version=REVIEW_COMPONENT,
                log_prefix="transcript-review",
                capture_output=False,
                stdout_sink=self.pipe.writer,
            )
            assert self.process is not None
            # OwnedWindowsProcess duplicated the borrowed sink. Close BOTH
            # parent writers after ResumeThread; only the exact child retains it.
            self.process.streams[1].close()
            self.pipe.writer.close()
        except BaseException:
            if self.process is not None:
                self._terminate(
                    "Không khởi chạy được; giữ workspace và kiểm tra lịch sử."
                )
            else:
                if self.pipe is not None and self.pipe.try_close():
                    self.pipe = None
                self.leases.close()
            raise

    def _response_text(self) -> str:
        text = self.received.decode("utf-8", errors="strict")

        def unique(pairs):
            body = {}
            for key, value in pairs:
                if key in body:
                    raise ValueError("Duplicate private response field")
                body[key] = value
            return body

        body = json.loads(text, object_pairs_hook=unique)
        if (
            not isinstance(body, dict)
            or type(body.get("format")) is not int
            or body.get("format") != 1
            or body.get("request_id") != self.request_id
            or body.get("action") != self.action
            or body.get("status") not in ("SUCCEEDED", "FAILED")
        ):
            raise ValueError("Private response does not match this request")
        return text

    def _poll(self) -> None:
        process, pipe = self.process, self.pipe
        if process is None or pipe is None:
            return
        try:
            if self.failure is None:
                for _ in range(4):
                    part = pipe.read_available(32768)
                    if len(self.received) + len(part) > MAX_RESPONSE:
                        raise ValueError("Private response budget exceeded")
                    self.received.extend(part)
                    if not part:
                        break
            terminal = process.tree_finished()
            if terminal and (pipe.eof or self.failure is not None):
                if not pipe.try_close():
                    if self.cleanup_deadline is None:
                        self.cleanup_deadline = time.perf_counter() + 8
                    elif time.perf_counter() >= self.cleanup_deadline:
                        self.cleanup_deadline = float("inf")
                        self.failed.emit(
                            "Chưa xác minh IPC đã đóng; giữ cửa sổ/workspace."
                        )
                    return  # retain native operation, process, root and request leases
                outcome = process.poll()
                assert outcome is not None
                process.close()
                self.pipe = None
                self.process = None
                self.timer.stop()
                text = None if self.failure is not None else self._response_text()
                self.received.clear()
                try:
                    if self.failure is not None:
                        self.failed.emit(self.failure)
                    else:
                        assert text is not None
                        self.finished.emit(outcome.exit_code, text)
                finally:
                    self.received.clear()  # no transcript retained in default diagnostics
                return
            if self.cleanup_deadline is not None:
                if time.perf_counter() >= self.cleanup_deadline:
                    self.cleanup_deadline = float("inf")
                    self.failed.emit(
                        "Chưa xác minh cleanup; giữ cửa sổ/workspace, chưa lưu tiếp."
                    )
            elif time.perf_counter() >= self.deadline:
                self._terminate(
                    "Hết thời gian; kiểm tra lịch sử trước khi thử tạo Evidence lại."
                )
            elif (
                not terminal
                and process.kernel.WaitForSingleObject(process.process, 0) == 0
            ):
                self._terminate(None)
        except (
            OSError,
            ValueError,
            RuntimeError,
            UnicodeError,
            RecursionError,
        ) as error:
            if self.process is not None:
                self._terminate(
                    f"Không xác nhận kết quả: {type(error).__name__}; kiểm tra lịch sử."
                )
            else:
                self.timer.stop()
                self.failed.emit(
                    f"Không xác nhận kết quả: {type(error).__name__}; kiểm tra lịch sử."
                )
                self.received.clear()

    def shutdown(self) -> None:
        self.timer.stop()
        if self.process is not None:
            self.process.close()
        if self.pipe is not None:
            if not self.pipe.try_close():
                if self.pipe.event is not None:
                    self.pipe.kernel.WaitForSingleObject(self.pipe.event, 1000)
                if not self.pipe.try_close():
                    if self not in _RETAINED_COMMANDS:
                        _RETAINED_COMMANDS.append(self)
                    raise TimeoutError(
                        "Private response cancellation unconfirmed; retain resources"
                    )
            self.pipe = None
        self.process = None
        self.received.clear()
        self.leases.close()
