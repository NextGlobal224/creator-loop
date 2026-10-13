"""Pinned local FFmpeg/ffprobe adapter with retained Windows Job ownership."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from creator_loop.backend.runtime.local_components import (
    ComponentPreflightError,
    ComponentSpec,
    hold_verified_components,
)
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
from creator_loop.backend.runtime.runtime_ownership import (
    bind_workspace_child,
    create_workspace_marker,
)
from creator_loop.backend.runtime.windows_paths import file_io_path

ToolName = Literal["ffmpeg", "ffprobe"]
MAX_TOOL_OUTPUT_BYTES = 256 * 1024


class LocalToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolOutput:
    stdout: str
    exit_code: int = 0


class VideoTools(Protocol):
    version: str

    def lease(
        self, cancelled: Callable[[], bool], timeout_seconds: float
    ) -> AbstractContextManager[object]: ...

    def run(
        self,
        tool: ToolName,
        arguments: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        cancelled: Callable[[], bool],
        progress_file: Path | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> ToolOutput: ...


def _progress_ms(path: Path) -> int | None:
    try:
        with file_io_path(path).open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 16384))
            content = stream.read(16384).decode("ascii", errors="ignore")
        values = [
            line.partition("=")[2]
            for line in content.splitlines()
            if line.startswith("out_time_us=")
        ]
        return max(0, int(values[-1]) // 1000) if values else None
    except (OSError, ValueError):
        return None


class LocalVideoTools:
    """No child process or download is started by construction/import."""

    def __init__(
        self,
        root: Path,
        specs: tuple[ComponentSpec, ...],
        *,
        worker_memory_limit: int = 512 * 1024 * 1024,
    ) -> None:
        selected = {spec.component_id: spec for spec in specs}
        try:
            self.specs = (selected["ffmpeg-exe"], selected["ffprobe-exe"])
        except KeyError as exc:
            raise ComponentPreflightError(
                "Chưa chọn component local ffmpeg-exe và ffprobe-exe có version/digest."
            ) from exc
        if any(spec.kind != "ENGINE" for spec in self.specs):
            raise ComponentPreflightError("FFmpeg/ffprobe phải là component ENGINE.")
        if self.specs[0].version != self.specs[1].version:
            raise ComponentPreflightError("FFmpeg và ffprobe phải cùng phiên bản.")
        self.root = root.resolve(strict=True)
        self.worker_memory_limit = worker_memory_limit
        self.version = self.specs[0].version
        self._leased = False

    @contextmanager
    def lease(
        self, cancelled: Callable[[], bool], timeout_seconds: float
    ) -> Iterator[object]:
        if self._leased:
            raise RuntimeError("Một adapter chỉ chạy một render tại một thời điểm.")
        with hold_verified_components(
            self.specs,
            worker_memory_limit=self.worker_memory_limit,
            timeout_seconds=min(timeout_seconds, 120),
            cancelled=cancelled,
        ):
            self._leased = True
            try:
                yield self
            finally:
                self._leased = False

    def run(
        self,
        tool: ToolName,
        arguments: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        cancelled: Callable[[], bool],
        progress_file: Path | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> ToolOutput:
        if not self._leased:
            raise RuntimeError("Cần giữ lease component trong toàn bộ render.")
        if sys.platform != "win32":
            raise OSError("Renderer có ownership native yêu cầu Windows.")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("Tool deadline phải hữu hạn, tối đa 600 giây.")
        if cancelled():
            raise InterruptedError("Đã hủy trước khi khởi chạy công cụ video.")
        spec = self.specs[0 if tool == "ffmpeg" else 1]
        for name in ("runtime", "logs"):
            directory = self.root / name
            if directory.is_symlink() or directory.is_junction():
                raise OSError("Renderer cần thư mục runtime/logs thật.")
            directory.mkdir(exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        component = f"{tool}/{spec.version}/{spec.sha256}"
        with tempfile.TemporaryDirectory(
            prefix="video-tool-", dir=self.root / "runtime"
        ) as directory_name:
            directory = Path(directory_name)
            marker = create_workspace_marker(
                self.root, directory, component, kind="LOCAL_VIDEO_TOOL_WORKSPACE"
            )
            log_directory = self.root / "logs" / f"{tool}-{uuid4().hex}"
            with OwnedWindowsProcess(
                spec.path,
                arguments,
                log_directory,
                component_version=component,
                cwd=cwd,
                memory_limit_bytes=self.worker_memory_limit,
                before_resume=lambda record: bind_workspace_child(
                    directory, marker, record
                ),
            ) as process:
                last_ms = -1
                while True:
                    if cancelled():
                        process.stop(125)
                        raise InterruptedError("Đã hủy worker video local.")
                    outcome = process.poll()
                    if outcome is not None:
                        break
                    if time.monotonic() >= deadline:
                        process.stop(124)
                        raise TimeoutError("Công cụ video đã vượt deadline của render.")
                    if progress_file is not None and on_progress is not None:
                        current = _progress_ms(progress_file)
                        if current is not None and current > last_ms:
                            last_ms = current
                            on_progress(current)
                    time.sleep(0.02)
            if cancelled():
                raise InterruptedError("Đã hủy trước khi xác nhận kết quả công cụ.")
            if outcome.exit_code:
                # Native logs retain diagnostics; do not leak captions/paths into UI.
                raise LocalToolError(f"{tool} thất bại (exit {outcome.exit_code}).")
            stdout_path = file_io_path(log_directory / "stdout.log")
            if stdout_path.stat().st_size > MAX_TOOL_OUTPUT_BYTES:
                raise LocalToolError("Kết quả công cụ vượt giới hạn metadata.")
            return ToolOutput(stdout_path.read_text(encoding="utf-8", errors="replace"))


def probe_arguments(path: Path) -> list[str]:
    return [
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,duration,nb_frames:format=duration,size,format_name",
        "-of",
        "json",
        str(path),
    ]


@dataclass(frozen=True)
class ProbeResult:
    duration_ms: int | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    frame_count: int | None
    byte_size: int | None
    format_name: str


def parse_probe(stdout: str) -> ProbeResult:
    if len(stdout.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
        raise LocalToolError("Metadata ffprobe vượt giới hạn.")
    try:
        payload = json.loads(stdout)
        streams = payload["streams"]
        fmt = payload.get("format", {})
        if (
            not isinstance(streams, list)
            or len(streams) > 32
            or not isinstance(fmt, dict)
        ):
            raise ValueError("Invalid stream count")
        video: dict[str, Any] = next(
            (item for item in streams if item.get("codec_type") == "video"), {}
        )
        audio: dict[str, Any] = next(
            (item for item in streams if item.get("codec_type") == "audio"), {}
        )

        def integer(value: object) -> int | None:
            return int(str(value)) if value not in (None, "N/A") else None

        duration = fmt.get("duration", video.get("duration", audio.get("duration")))
        seconds = float(duration) if duration not in (None, "N/A") else None
        if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
            raise ValueError("Invalid duration")
        return ProbeResult(
            round(seconds * 1000) if seconds is not None else None,
            integer(video.get("width")),
            integer(video.get("height")),
            video.get("codec_name"),
            audio.get("codec_name"),
            integer(video.get("nb_frames")),
            integer(fmt.get("size")),
            str(fmt.get("format_name", "")),
        )
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        raise LocalToolError("Metadata ffprobe không hợp lệ.") from exc
