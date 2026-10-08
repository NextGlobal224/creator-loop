"""Hold and verify retained terminal audio witnesses; never delete/kill them."""

from __future__ import annotations

import re
import sqlite3
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_recovery import _identity_dead

_ID = re.compile(r"[0-9a-f]{32}")
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(
        a.get(k) == b.get(k)
        for k in ("pid", "creation_identity", "creation_identity_format", "executable")
    )


def _bound_child(marker: dict[str, Any], child: dict[str, Any]) -> None:
    parent = marker.get("parent")
    if isinstance(parent, dict):
        _identity_dead(parent)  # Validate native identity; GUI parent may still live.
    if (
        not isinstance(parent, dict)
        or type(child.get("record_format")) is not int
        or child["record_format"] != 1
        or type(child.get("parent_pid")) is not int
        or child.get("parent_pid") != parent.get("pid")
        or child.get("component_version") != marker["component_version"]
        or any(
            not isinstance(child.get(k), str) or not _ID.fullmatch(child[k])
            for k in ("run_id", "job_id")
        )
        or not _identity_dead(child)
    ):
        raise RuntimeError("Retained runtime child is unbound or still live")


@contextmanager
def hold_terminal_audio_runtime(
    root: Path, db: sqlite3.Connection
) -> Iterator[Callable[[], None]]:
    """Only known dead executors with terminal DB witnesses may coexist with backup.

    Caller holds app lock and DB writer reservation. Hold namespace/files through
    the snapshot; unknown/live/partial/link/tamper entries retain the old refusal.
    Neither runtime evidence nor registered files/rows are modified.
    """
    runtime = root / "runtime"
    entries = list(runtime.iterdir())
    extra = [p for p in entries if p.name != "app-data.lock"]

    def recheck_namespace() -> None:
        if {p.name for p in runtime.iterdir()} != {p.name for p in entries}:
            raise RuntimeError("Runtime namespace changed during validation")

    if not extra:
        if sys.platform == "win32":
            with RuntimeHandle(runtime, directory=True):
                if {p.name for p in runtime.iterdir()} != {p.name for p in entries}:
                    raise RuntimeError("Runtime namespace changed during validation")
                yield recheck_namespace
        else:
            yield recheck_namespace
        return
    if sys.platform != "win32":
        raise RuntimeError("Worker runtime records require recovery before backup")
    with ExitStack() as held:
        held.enter_context(RuntimeHandle(runtime, directory=True))
        records: dict[str, tuple[dict[str, Any], dict[str, Any], set[str]]] = {}
        metadata: dict[tuple[str, str], dict[str, Any]] = {}
        for directory in extra:
            if not re.fullmatch(
                r"(?:transcribe|processing)-[0-9a-f]{32}|whisper-[a-z0-9_]{8}",
                directory.name,
            ):
                raise RuntimeError(
                    "Worker runtime records require recovery before backup"
                )
            held.enter_context(RuntimeHandle(directory, directory=True))
            names = {p.name for p in directory.iterdir()}
            if directory.name.startswith("processing-"):
                required = allowed = {"ownership.json", "task.json"}
            elif directory.name.startswith("transcribe-"):
                required = {"ownership.json", "child-ownership.json", "request.json"}
                allowed = required | {"cancel.json"}
            else:
                required = {"ownership.json", "child-ownership.json"}
                allowed = required | {"raw.json"}
            if not required <= names or not names <= allowed:
                raise RuntimeError("Unknown or incomplete retained runtime files")
            if "ownership.json" not in names:
                raise RuntimeError("Runtime entry lacks ownership")
            files = {
                name: held.enter_context(
                    RuntimeHandle(directory / name, read_only=True)
                )
                for name in names
            }
            marker = files["ownership.json"].read_json(reject_duplicates=True)
            if (
                type(marker.get("format")) is not int
                or marker["format"] != 1
                or marker.get("data_root") != str(root)
                or marker.get("workspace") != directory.name
                or not isinstance(marker.get("parent"), dict)
                or not isinstance(marker.get("component_version"), str)
            ):
                raise RuntimeError("Runtime ownership binding mismatch")
            metadata[directory.name, "ownership.json"] = marker
            for name, handle in files.items():
                if name not in ("raw.json", "ownership.json"):
                    metadata[directory.name, name] = handle.read_json(
                        reject_duplicates=True
                    )
            records[directory.name] = (
                marker,
                metadata.get((directory.name, "child-ownership.json"), {}),
                names,
            )

        executors: list[dict[str, Any]] = []
        decode_inputs: list[tuple[dict[str, Any], str]] = []
        asr_executors: list[dict[str, Any]] = []
        for name, (marker, _, names) in records.items():
            if not name.startswith("processing-"):
                continue
            if (
                names != {"ownership.json", "task.json"}
                or marker.get("kind") != "PROCESSING_EXECUTOR_WITNESS"
            ):
                raise RuntimeError("Unknown processing witness")
            run_id = name.removeprefix("processing-")
            row = db.execute(
                "SELECT asset_id,input_file_id,task_type,status,finished_at,tool_name,tool_version,model_name,model_version "
                "FROM processing_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or row[3] not in _TERMINAL or not row[4]:
                raise RuntimeError("Processing witness is not terminal")
            asset, file_id, task, _, _, tool, version, model, model_version = row
            if (
                task == "VIDEO_AUDIO_DECODE"
                and tool == "PySide6 Qt audio decoder"
                and model is None
                and model_version is None
            ):
                component = "qt-pcm/" + version
                expected_role, expected_mime = "ORIGINAL", "video/mp4"
            elif (
                task == "AUDIO_TRANSCRIPTION"
                and tool == "whisper.cpp"
                and version == "1.8.7"
                and model == "Whisper base multilingual"
                and model_version == "5359861c739e955e79d9a303bcbc70fb988958b1"
            ):
                component = "whisper.cpp/1.8.7/base-multilingual"
                expected_role, expected_mime = "DERIVED_AUDIO", "audio/wav"
            else:
                raise RuntimeError("Unsupported processing witness component")
            source = db.execute(
                "SELECT asset_id,role,mime_type FROM asset_files WHERE file_id=?",
                (file_id,),
            ).fetchone()
            if source is None or tuple(source) != (asset, expected_role, expected_mime):
                raise RuntimeError("Processing input binding mismatch")
            expected = {
                "format": 1,
                "run_id": run_id,
                "asset_id": asset,
                "input_file_id": file_id,
                "task_type": task,
                "component_version": component,
            }
            if (
                type(metadata[name, "task.json"].get("format")) is not int
                or metadata[name, "task.json"] != expected
                or marker["component_version"] != component
                or not _identity_dead(marker["parent"])
            ):
                raise RuntimeError("Processing executor is unbound or still live")
            executors.append(marker["parent"])
            if task == "VIDEO_AUDIO_DECODE":
                decode_inputs.append((marker["parent"], file_id))
            if task == "AUDIO_TRANSCRIPTION":
                asr_executors.append(marker["parent"])

        task_children: list[dict[str, Any]] = []
        for name, (marker, child, names) in records.items():
            if not name.startswith("transcribe-"):
                continue
            required = {"ownership.json", "child-ownership.json", "request.json"}
            if (
                not required <= names
                or not names <= required | {"cancel.json"}
                or marker.get("kind") != "VIDEO_TRANSCRIPTION_TASK"
                or marker["component_version"] != "video-transcription/1"
            ):
                raise RuntimeError("Unknown retained transcription workspace")
            _bound_child(marker, child)
            if not any(_same(child, owner) for owner in executors):
                raise RuntimeError(
                    "Transcription child lacks terminal processing witness"
                )
            request = metadata[name, "request.json"]
            request_id = name.removeprefix("transcribe-")
            if (
                set(request)
                != {
                    "format",
                    "request_id",
                    "root",
                    "file_id",
                    "language",
                    "worker_memory_mib",
                }
                or type(request["format"]) is not int
                or request["format"] != 1
                or request["request_id"] != request_id
                or request["root"] != str(root)
                or request["language"] not in ("vi", "en")
                or type(request["worker_memory_mib"]) is not int
                or not 64 <= request["worker_memory_mib"] <= 4096
                or db.execute(
                    "SELECT 1 FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id WHERE f.file_id=? AND f.role='ORIGINAL' AND f.mime_type='video/mp4' AND a.media_type='VIDEO'",
                    (request["file_id"],),
                ).fetchone()
                is None
            ):
                raise RuntimeError("Transcription request binding mismatch")
            if "cancel.json" in names and (
                type(metadata[name, "cancel.json"].get("format")) is not int
                or metadata[name, "cancel.json"].get("cancel") is not True
                or metadata[name, "cancel.json"]
                != {
                    "format": 1,
                    "request_id": request_id,
                    "cancel": True,
                }
            ):
                raise RuntimeError("Invalid retained cancellation")
            if not any(
                _same(child, owner) and original_id == request["file_id"]
                for owner, original_id in decode_inputs
            ):
                raise RuntimeError(
                    "Task request does not match its terminal decoder input"
                )
            task_children.append(child)

        for name, (marker, child, names) in records.items():
            if not name.startswith("whisper-"):
                continue
            required = {"ownership.json", "child-ownership.json"}
            if (
                not required <= names
                or not names <= required | {"raw.json"}
                or marker.get("kind") != "WHISPER_TRANSCRIPTION_WORKSPACE"
                or marker["component_version"] != "whisper.cpp/1.8.7/base-multilingual"
            ):
                raise RuntimeError("Unknown retained engine workspace")
            _bound_child(marker, child)
            if not any(
                _same(marker["parent"], owner) for owner in asr_executors
            ) or not any(_same(marker["parent"], owner) for owner in task_children):
                raise RuntimeError("Engine parent lacks terminal task binding")

        def recheck_all() -> None:
            recheck_namespace()
            for name, (_, _, expected_names) in records.items():
                if {p.name for p in (runtime / name).iterdir()} != expected_names:
                    raise RuntimeError(
                        "Retained workspace namespace changed during backup"
                    )

        recheck_all()
        yield recheck_all
