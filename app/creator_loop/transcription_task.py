"""Private owned I/O executor; no root initialization or automatic Evidence."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.audio_derivative import create_video_audio_derivative
from creator_loop.component_selection import load_component_selection
from creator_loop.owned_process import _api
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import process_identity
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.transcription_service import transcribe_audio_derivative
from creator_loop.whisper_adapter import WhisperSettings

TASK_COMPONENT = "video-transcription/1"
TASK_JOB_MEMORY = 1024 * 1024**2
TASK_TIMEOUT = 420


def _read_task_cancellation(path: Path, request_id: str) -> bool:
    try:
        with RuntimeHandle(path, read_only=True) as control:
            payload = control.read_json(reject_duplicates=True)
    except FileNotFoundError:
        return False
    except OSError as error:
        if getattr(error, "winerror", None) == 32:
            # CREATE_NEW publishes the pathname before its exclusive writer
            # fsyncs/closes. No partial bytes are readable; retry on the next
            # bounded worker poll. All other I/O and malformed records fail.
            return False
        raise
    if (
        set(payload) != {"format", "request_id", "cancel"}
        or type(payload["format"]) is not int
        or payload["format"] != 1
        or payload["request_id"] != request_id
        or payload["cancel"] is not True
    ):
        raise ValueError("Invalid task cancellation record")
    return True


def _emit(body: dict[str, object]) -> None:
    print(json.dumps(body, ensure_ascii=True, allow_nan=False), flush=True)


def run_transcription_task(request: Path) -> int:
    """A suspended native child is bound before resume; reject unowned requests."""
    result: dict[str, object] = {"format": 1, "event": "result", "status": "FAILED"}
    try:
        root = request.parent.parent.parent.resolve(strict=True)
        request_id = request.parent.name.removeprefix("transcribe-")
        if (
            re.fullmatch("[0-9a-f]{32}", request_id) is None
            or request
            != root / "runtime" / ("transcribe-" + request_id) / "request.json"
        ):
            raise ValueError("Invalid task workspace")
        result["request_id"] = request_id
        with ExitStack() as held:
            held.enter_context(
                RuntimeHandle(root, directory=True, allow_child_writes=True)
            )
            held.enter_context(
                RuntimeHandle(root / "runtime", directory=True, allow_child_writes=True)
            )
            held.enter_context(
                RuntimeHandle(request.parent, directory=True, allow_child_writes=True)
            )
            body = held.enter_context(RuntimeHandle(request, read_only=True)).read_json(
                reject_duplicates=True
            )
            marker = held.enter_context(
                RuntimeHandle(request.parent / "ownership.json")
            ).read_json(reject_duplicates=True)
            child = held.enter_context(
                RuntimeHandle(request.parent / "child-ownership.json")
            ).read_json(reject_duplicates=True)
            if (
                not isinstance(body, dict)
                or set(body)
                != {
                    "format",
                    "root",
                    "request_id",
                    "file_id",
                    "language",
                    "worker_memory_mib",
                }
                or type(body["format"]) is not int
                or body["format"] != 1
                or body["root"] != str(root)
                or body["request_id"] != request_id
                or not isinstance(body["file_id"], str)
                or re.fullmatch("[0-9a-f]{32}", body["file_id"]) is None
                or not isinstance(body["language"], str)
                or re.fullmatch("[a-z]{2}", body["language"]) is None
                or type(body["worker_memory_mib"]) is not int
                or body["worker_memory_mib"] not in (512, 768)
            ):
                raise ValueError("Invalid task request")
            kernel = _api()
            observed = process_identity(kernel, kernel.GetCurrentProcess())
            if (
                not isinstance(marker, dict)
                or type(marker.get("format")) is not int
                or marker.get("format") != 1
                or marker.get("kind") != "VIDEO_TRANSCRIPTION_TASK"
                or marker.get("data_root") != str(root)
                or marker.get("workspace") != request.parent.name
                or marker.get("component_version") != TASK_COMPONENT
                or not isinstance(marker.get("parent"), dict)
                or _identity_dead(marker["parent"])
                or not isinstance(child, dict)
                or type(child.get("record_format")) is not int
                or child.get("record_format") != 1
                or child.get("pid") != os.getpid()
                or child.get("parent_pid") != marker["parent"].get("pid")
                or child.get("component_version") != TASK_COMPONENT
                or any(child.get(key) != value for key, value in observed.items())
                or any(
                    not isinstance(child.get(key), str)
                    or re.fullmatch("[0-9a-f]{32}", child[key]) is None
                    for key in ("run_id", "job_id")
                )
            ):
                raise ValueError("Task executor ownership mismatch")
            kernel.IsProcessInJob.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            )
            kernel.IsProcessInJob.restype = wintypes.BOOL
            in_job = wintypes.BOOL()
            if (
                not kernel.IsProcessInJob(
                    kernel.GetCurrentProcess(), None, ctypes.byref(in_job)
                )
                or not in_job.value
            ):
                raise OSError("Task must execute in an owned Job")

            def cancelled() -> bool:
                return _read_task_cancellation(
                    request.parent / "cancel.json", request_id
                )

            # Qt/DB/component preflight, hashing and writes all occur off GUI.
            from PySide6.QtGui import QGuiApplication

            app = QGuiApplication.instance() or QGuiApplication([])
            with AppDataLock(root) as coordination:
                selection = load_component_selection(root)
                budget = body["worker_memory_mib"] * 1024**2
                if selection is None or selection.worker_memory_limit != budget:
                    raise ValueError(
                        "Select and review components with this worker budget first"
                    )
                _emit({"event": "phase", "request_id": request_id, "phase": "decode"})
                audio = create_video_audio_derivative(
                    body["file_id"], root, coordination, cancelled=cancelled
                )
                result.update(decode_run_id=audio.run_id, audio_file_id=audio.file_id)
                _emit(
                    {
                        "event": "phase",
                        "request_id": request_id,
                        "phase": "transcribe",
                        "decode_run_id": audio.run_id,
                        "audio_file_id": audio.file_id,
                    }
                )
                transcript = transcribe_audio_derivative(
                    audio.file_id,
                    root,
                    coordination,
                    selection.specs,
                    settings=WhisperSettings(
                        memory_limit_bytes=budget,
                        timeout_seconds=240,
                        threads=2,
                        language=body["language"],
                    ),
                    cancelled=cancelled,
                )
                result.update(
                    status="SUCCEEDED",
                    transcription_run_id=transcript.run_id,
                    raw_file_id=transcript.raw_file_id,
                    provenance_file_id=transcript.provenance_file_id,
                    raw_sha256=transcript.raw_sha256,
                )
            _ = app
        code = 0
    except DataRootBusy:
        result["error"] = "DATA_ROOT_BUSY"
        code = 3
    except InterruptedError:
        result.update(status="CANCELLED", error="CANCELLED")
        code = 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error, RecursionError) as error:
        result["error"] = type(error).__name__
        code = 4
    _emit(result)  # IDs/status only; no machine text in task logs.
    return code
