"""Owned RAW review executor; bounded private stdout, no default text logs."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import sys
from contextlib import ExitStack, closing
from ctypes import wintypes as w
from pathlib import Path
from typing import Any

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.owned_process import _api
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import process_identity
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.transcript_evidence import (
    create_transcript_evidence,
    load_transcript_suggestions,
)

REVIEW_COMPONENT = "transcript-review/1"
REVIEW_TIMEOUT = 120
REVIEW_MEMORY = 256 * 1024**2
MAX_RESPONSE = 1024 * 1024
PAGE_SIZE = 25


def _validate_request(body: dict[str, Any], root: Path, request_id: str) -> None:
    if (
        set(body)
        != {
            "format",
            "request_id",
            "root",
            "raw_file_id",
            "action",
            "index",
            "raw_sha256",
        }
        or type(body["format"]) is not int
        or body["format"] != 1
        or body["request_id"] != request_id
        or body["root"] != str(root)
        or not isinstance(body["raw_file_id"], str)
        or re.fullmatch("[0-9a-f]{32}", body["raw_file_id"]) is None
        or body["action"] not in ("page", "segment", "preview", "create")
        or type(body["index"]) is not int
        or not 0 <= body["index"] <= 10000
        or (
            body["raw_sha256"] is not None
            and (
                not isinstance(body["raw_sha256"], str)
                or re.fullmatch("[0-9a-f]{64}", body["raw_sha256"]) is None
            )
        )
        or (body["action"] != "page" and body["raw_sha256"] is None)
    ):
        raise ValueError("Invalid bounded transcript review request")


def _validate_owner(marker: dict, child: dict, root: Path, workspace: Path) -> None:
    kernel = _api()
    observed = process_identity(kernel, kernel.GetCurrentProcess())
    if (
        type(marker.get("format")) is not int
        or marker.get("format") != 1
        or marker.get("kind") != "TRANSCRIPT_REVIEW_TASK"
        or marker.get("data_root") != str(root)
        or marker.get("workspace") != workspace.name
        or marker.get("component_version") != REVIEW_COMPONENT
        or not isinstance(marker.get("parent"), dict)
        or _identity_dead(marker["parent"])
        or type(child.get("record_format")) is not int
        or child.get("record_format") != 1
        or child.get("pid") != os.getpid()
        or child.get("parent_pid") != marker["parent"].get("pid")
        or child.get("component_version") != REVIEW_COMPONENT
        or any(child.get(key) != value for key, value in observed.items())
        or any(
            not isinstance(child.get(key), str)
            or re.fullmatch("[0-9a-f]{32}", child[key]) is None
            for key in ("run_id", "job_id")
        )
    ):
        raise ValueError("Transcript review executor ownership mismatch")
    kernel.IsProcessInJob.argtypes = (w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL))
    kernel.IsProcessInJob.restype = w.BOOL
    in_job = w.BOOL()
    if (
        not kernel.IsProcessInJob(
            kernel.GetCurrentProcess(), None, ctypes.byref(in_job)
        )
        or not in_job.value
    ):
        raise OSError("Transcript review requires an owned Job")


def _execute(body: dict[str, Any], root: Path, coordination: AppDataLock) -> dict:
    suggestions = load_transcript_suggestions(body["raw_file_id"], root, coordination)
    if body["raw_sha256"] is not None and body["raw_sha256"] != suggestions.raw_sha256:
        raise ValueError("RAW changed after review")
    index = body["index"]
    common = {
        "raw_file_id": suggestions.raw_file_id,
        "raw_sha256": suggestions.raw_sha256,
        "original_file_id": suggestions.original_file_id,
        "run_id": suggestions.run_id,
        "language": suggestions.language,
        "total": len(suggestions.segments),
    }
    if body["action"] == "page":
        if index > len(suggestions.segments):
            raise ValueError("Page begins after retained segments")
        return {
            **common,
            "offset": index,
            "segments": [
                {
                    "index": number,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "preview": segment.text[:200],
                }
                for number, segment in enumerate(
                    suggestions.segments[index : index + PAGE_SIZE], start=index
                )
            ],
        }
    if index >= len(suggestions.segments):
        raise ValueError("No selected retained segment")
    segment = suggestions.segments[index]
    if body["action"] == "preview":
        from PySide6.QtGui import QGuiApplication

        from creator_loop.audio_evidence import decode_audio_segment
        from creator_loop.database import open_readonly

        app = QGuiApplication.instance() or QGuiApplication([])
        with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
            _asset, decoded = decode_audio_segment(
                db,
                suggestions.original_file_id,
                root,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
            )
        _ = app
        return {
            **common,
            "index": index,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "decoded": {
                "duration_ms": decoded.duration_ms,
                "buffer_start_ms": decoded.buffer_start_ms,
                "buffer_end_ms": decoded.buffer_end_ms,
                "anchor_path": str(decoded.anchor_path),
                "data_root": str(root),
                "expected_size": decoded.expected_size,
                "expected_sha256": decoded.expected_sha256,
            },
        }
    if body["action"] == "segment":
        return {
            **common,
            "index": index,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
        }
    version = create_transcript_evidence(
        body["raw_file_id"], index, body["raw_sha256"], root, coordination
    )
    return {
        **common,
        "index": index,
        "evidence_id": version.evidence_id,
        "evidence_version_id": version.evidence_version_id,
        "producer_type": "MODEL",
    }


def run_transcript_review_task(request: Path) -> int:
    result: dict[str, object] = {"format": 1, "status": "FAILED"}
    try:
        root = request.parent.parent.parent.resolve(strict=True)
        request_id = request.parent.name.removeprefix("transcript-review-")
        if (
            re.fullmatch("[0-9a-f]{32}", request_id) is None
            or request
            != root / "runtime" / ("transcript-review-" + request_id) / "request.json"
        ):
            raise ValueError("Invalid transcript review workspace")
        result["request_id"] = request_id
        with ExitStack() as held:
            for path in (root, root / "runtime", request.parent):
                held.enter_context(
                    RuntimeHandle(path, directory=True, allow_child_writes=True)
                )
            body = held.enter_context(RuntimeHandle(request, read_only=True)).read_json(
                reject_duplicates=True
            )
            _validate_request(body, root, request_id)
            result["action"] = body["action"]
            marker = held.enter_context(
                RuntimeHandle(request.parent / "ownership.json")
            ).read_json(reject_duplicates=True)
            child = held.enter_context(
                RuntimeHandle(request.parent / "child-ownership.json")
            ).read_json(reject_duplicates=True)
            _validate_owner(marker, child, root, request.parent)
            with AppDataLock(root) as coordination:
                result.update(
                    status="SUCCEEDED", data=_execute(body, root, coordination)
                )
        code = 0
    except DataRootBusy:
        result["error"] = "DATA_ROOT_BUSY"
        code = 3
    except (OSError, ValueError, RuntimeError, sqlite3.Error, RecursionError) as error:
        result["error"] = type(error).__name__
        code = 4
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_RESPONSE:
        # A bounded receipt never claims rollback after a potentially committed create.
        payload = json.dumps(
            {
                "format": 1,
                "request_id": result.get("request_id"),
                "status": "FAILED",
                "error": "RESPONSE_BUDGET",
            }
        ).encode("utf-8")
        code = 4
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return code
