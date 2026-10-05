"""Explicitly selected Whisper PCM adapter; no DB writes or human acceptance.

Call from an owned I/O worker, never the GUI: a process deadline must also
bound filesystem preflight. Retain RAW workspaces until the pipeline durably
registers them; this module never discards transcripts or external components.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
import wave
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.local_components import ComponentSpec, hold_verified_components
from creator_loop.owned_process import (
    OwnedWindowsProcess,
    ProcessOutcome,
    _ExtendedLimits,
)
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker

RUNTIME_FILES = {
    "whisper-cli.exe",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "whisper.dll",
}
MAX_PCM_BYTES = 64 * 1024 * 1024
MAX_RAW_BYTES = 8 * 1024 * 1024
_sequential = threading.Lock()


@dataclass(frozen=True)
class WhisperSettings:
    memory_limit_bytes: int  # Explicit task budget, never inferred from model size.
    language: str = "vi"
    threads: int = 2
    timeout_seconds: float = 120


@dataclass(frozen=True)
class WhisperSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class WhisperTranscript:
    raw_json: bytes
    raw_sha256: str
    segments: tuple[WhisperSegment, ...]
    input_sha256: str
    input_bytes: int
    workspace: Path
    ownership: dict[str, object]
    component_ids_and_hashes: tuple[tuple[str, str], ...]
    peak_job_commit_bytes: int
    elapsed_seconds: float


class WhisperRunError(RuntimeError):
    """The retained workspace/logs identify the failed native attempt."""

    def __init__(self, message: str, workspace: Path, exit_code: int):
        super().__init__(message)
        self.workspace = workspace
        self.exit_code = exit_code


def _record_outcome(
    worker: OwnedWindowsProcess,
    logs: Path,
    outcome: ProcessOutcome,
    status: str,
    settings: WhisperSettings,
    started: float,
) -> int:
    """Native stdout/stderr may contain transcript; log metadata only."""
    if sys.platform != "win32":
        raise OSError("Whisper native outcome requires Windows")
    metrics = _ExtendedLimits()
    if not worker.kernel.QueryInformationJobObject(
        worker.job, 9, ctypes.byref(metrics), ctypes.sizeof(metrics), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    with (logs / "outcome.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "format": 1,
                "status": status,
                "exit_code": outcome.exit_code,
                "run_id": worker.record["run_id"],
                "job_id": worker.record["job_id"],
                "memory_limit_bytes": settings.memory_limit_bytes,
                "peak_job_commit_bytes": metrics.peak_job,
                "elapsed_seconds": time.perf_counter() - started,
                "native_output_logged": False,
            },
            stream,
            allow_nan=False,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return metrics.peak_job


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate RAW JSON key")
        result[key] = value
    return result


def parse_whisper_raw(
    raw: bytes, *, duration_ms: int, language: str
) -> tuple[WhisperSegment, ...]:
    """Validate suggested offsets; preserve the engine's exact RAW bytes/text.

    Empty output is valid. Neither a successful parse nor nonempty text proves
    speech, transcription quality, an exact locator or human acceptance.
    """
    if (
        not raw
        or len(raw) > MAX_RAW_BYTES
        or type(duration_ms) is not int
        or duration_ms <= 0
    ):
        raise ValueError("Invalid RAW size or PCM duration")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid RAW transcription JSON") from exc
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("model"), dict)
        or body["model"].get("type") != "base"
        or body["model"].get("multilingual") is not True
        or not isinstance(body.get("params"), dict)
        or body["params"].get("language") != language
        or body["params"].get("translate") is not False
        or not isinstance(body.get("result"), dict)
        or body["result"].get("language") != language
        or not isinstance(body.get("transcription"), list)
        or len(body["transcription"]) > 10000
    ):
        raise ValueError("Unexpected model/language or RAW structure")
    segments = []
    previous = 0
    for segment in body["transcription"]:
        if not isinstance(segment, dict) or not isinstance(
            segment.get("offsets"), dict
        ):
            raise ValueError("Invalid RAW segment")
        start, end = segment["offsets"].get("from"), segment["offsets"].get("to")
        text = segment.get("text")
        if (
            type(start) is not int
            or type(end) is not int
            or not previous <= start < end <= duration_ms
            or not isinstance(text, str)
            or len(text) > 100000
            or any(0xD800 <= ord(char) <= 0xDFFF or char == "\x00" for char in text)
        ):
            raise ValueError("Invalid RAW offset/text; no silent clipping")
        segments.append(WhisperSegment(start, end, text))
        previous = end
    return tuple(segments)


def _profile(
    specs: tuple[ComponentSpec, ...],
) -> tuple[ComponentSpec, ComponentSpec, Path]:
    engines = [item for item in specs if item.kind == "ENGINE"]
    models = [item for item in specs if item.kind == "MODEL"]
    if (
        len(engines) != 5
        or len(models) != 1
        or len(specs) != 6
        or {item.path.name for item in engines} != RUNTIME_FILES
        or any(item.version != "1.8.7" for item in engines)
        or models[0].version != "5359861c739e955e79d9a303bcbc70fb988958b1"
    ):
        raise ValueError("Select the approved v1.8.7 CPU/base multilingual profile")
    executable = next(item for item in engines if item.path.name == "whisper-cli.exe")
    runtime = executable.path.parent
    if any(item.path.parent != runtime for item in engines):
        raise ValueError(
            "Whisper runtime libraries must share the selected EXE directory"
        )
    return executable, models[0], runtime


def transcribe_whisper_wav(
    specs: tuple[ComponentSpec, ...],
    wav_path: Path,
    root: Path,
    coordination: AppDataLock,
    *,
    settings: WhisperSettings,
    expected_sha256: str,
    expected_size: int,
    cancelled: Callable[[], bool] = lambda: False,
) -> WhisperTranscript:
    """One sequential owned native task. RAW/failure workspaces are retained.

    This adapter does not create processing_runs, Evidence or review events.
    Pipeline registration/recovery and Qt video-to-PCM integration are separate.
    """
    if sys.platform != "win32":
        raise OSError("Whisper worker ownership requires Windows")
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError("Whisper requires this root's held app lock")
    if (
        type(settings.memory_limit_bytes) is not int
        or not 64 * 1024**2 <= settings.memory_limit_bytes <= 4 * 1024**3
        or type(settings.threads) is not int
        or not 1 <= settings.threads <= 4
        or not isinstance(settings.language, str)
        or not re.fullmatch(r"[a-z]{2}", settings.language)
        or type(settings.timeout_seconds) not in (int, float)
        or not math.isfinite(settings.timeout_seconds)
        or not 0 < settings.timeout_seconds <= 600
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or type(expected_size) is not int
        or not 44 <= expected_size <= MAX_PCM_BYTES
    ):
        raise ValueError("Invalid Whisper task limits/input identity")
    executable, model, runtime = _profile(specs)
    if cancelled():
        raise InterruptedError("Whisper cancelled before preflight")
    if not _sequential.acquire(blocking=False):
        raise RuntimeError("Another Whisper task is already running")
    try:
        with ExitStack() as held:
            held.enter_context(
                hold_verified_components(
                    specs,
                    worker_memory_limit=settings.memory_limit_bytes,
                    cancelled=cancelled,
                )
            )
            held.enter_context(RuntimeHandle(runtime, directory=True))
            if {entry.name for entry in runtime.iterdir()} != RUNTIME_FILES:
                raise ValueError("Unverified file in the selected Whisper runtime")
            for folder in (canonical / "runtime", canonical / "logs"):
                held.enter_context(
                    RuntimeHandle(folder, directory=True, allow_child_writes=True)
                )
            if wav_path.resolve(strict=True) != wav_path or wav_path.is_symlink():
                raise ValueError("A canonical regular PCM input is required")
            held.enter_context(RuntimeHandle(wav_path.parent, directory=True))
            audio = held.enter_context(_open_read_lock(wav_path))
            before = os.fstat(audio.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != expected_size
            ):
                raise ValueError("PCM input size/type/identity mismatch")
            digest = hashlib.sha256()
            while chunk := audio.read(1024 * 1024):
                if cancelled():
                    raise InterruptedError("Whisper input verification cancelled")
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("PCM input digest mismatch")
            audio.seek(0)
            with wave.open(audio, "rb") as pcm:
                if pcm.getcomptype() != "NONE" or (
                    pcm.getnchannels(),
                    pcm.getsampwidth(),
                    pcm.getframerate(),
                ) != (1, 2, 16000):
                    raise ValueError("Whisper input requires PCM16/16000Hz/mono WAV")
                frames = pcm.getnframes()
                if frames <= 0:
                    raise ValueError("Truncated or empty PCM input")
                remaining = frames
                while remaining:
                    count = min(16000, remaining)
                    if cancelled():
                        raise InterruptedError("Whisper PCM verification cancelled")
                    if len(pcm.readframes(count)) != count * 2:
                        raise ValueError("Truncated PCM input")
                    remaining -= count
            duration_ms = (frames * 1000 + 15999) // 16000
            if cancelled():
                raise InterruptedError("Whisper cancelled before native launch")
            workspace = Path(
                tempfile.mkdtemp(prefix="whisper-", dir=canonical / "runtime")
            )
            held.enter_context(
                RuntimeHandle(workspace, directory=True, allow_child_writes=True)
            )
            component = "whisper.cpp/1.8.7/base-multilingual"
            marker = create_workspace_marker(
                canonical, workspace, component, kind="WHISPER_TRANSCRIPTION_WORKSPACE"
            )
            environment = os.environ.copy()
            environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            environment["OMP_NUM_THREADS"] = str(settings.threads)
            for key in tuple(environment):
                if key.upper().startswith(("PYTHON", "QT_", "QML_")):
                    environment.pop(key)
            started = time.perf_counter()
            logs = canonical / "logs" / ("whisper-" + uuid4().hex)
            with OwnedWindowsProcess(
                executable.path,
                [
                    "-ng",
                    "-m",
                    str(model.path),
                    "-f",
                    str(wav_path),
                    "-l",
                    settings.language,
                    "-t",
                    str(settings.threads),
                    "-bs",
                    "1",
                    "-bo",
                    "1",
                    "-oj",
                    "-of",
                    str(workspace / "raw"),
                ],
                logs,
                component_version=component,
                environment=environment,
                cwd=workspace,
                capture_output=False,
                memory_limit_bytes=settings.memory_limit_bytes,
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
            ) as worker:
                while not worker.tree_finished():
                    if cancelled():
                        worker.stop(130)
                        _record_outcome(
                            worker, logs, worker.wait(1), "CANCELLED", settings, started
                        )
                        raise InterruptedError("Whisper native task cancelled")
                    if time.perf_counter() - started >= settings.timeout_seconds:
                        worker.stop(124)
                        _record_outcome(
                            worker, logs, worker.wait(1), "TIMED_OUT", settings, started
                        )
                        raise TimeoutError(
                            "Whisper exceeded its owned process deadline"
                        )
                    time.sleep(0.01)
                ownership = worker.record.copy()
                outcome = worker.wait(1)
                peak_job_commit_bytes = _record_outcome(
                    worker,
                    logs,
                    outcome,
                    "EXITED" if outcome.exit_code == 0 else "FAILED",
                    settings,
                    started,
                )
                if outcome.exit_code != 0:
                    raise WhisperRunError(
                        "Whisper native task failed; retain RAW/logs",
                        workspace,
                        outcome.exit_code,
                    )
            if cancelled():
                raise InterruptedError("Whisper cancelled before returning RAW")
            if {entry.name for entry in runtime.iterdir()} != RUNTIME_FILES:
                raise ValueError("Whisper runtime inventory changed")
            expected_entries = {"ownership.json", "child-ownership.json", "raw.json"}
            if {entry.name for entry in workspace.iterdir()} != expected_entries:
                raise ValueError("Unknown entry in the retained Whisper workspace")
            raw_path = workspace / "raw.json"
            if raw_path.resolve(strict=True) != raw_path or raw_path.is_symlink():
                raise ValueError("RAW output must be a regular owned workspace file")
            with _open_read_lock(raw_path) as output:
                info = os.fstat(output.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= MAX_RAW_BYTES
                ):
                    raise ValueError("Invalid RAW output file/size")
                raw = output.read(MAX_RAW_BYTES + 1)
            segments = parse_whisper_raw(
                raw, duration_ms=duration_ms, language=settings.language
            )
            return WhisperTranscript(
                raw,
                hashlib.sha256(raw).hexdigest(),
                segments,
                expected_sha256,
                expected_size,
                workspace,
                ownership,
                tuple((item.component_id, item.sha256) for item in specs),
                peak_job_commit_bytes,
                time.perf_counter() - started,
            )
    finally:
        _sequential.release()
