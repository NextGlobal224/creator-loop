"""Explicit engine CLI startup/version check, without model inference or acceptance.

Call from an owned I/O worker for filesystem deadlines. Saved selections remain
historical declarations; this operation rehashes/leases all selected bytes.
Untrusted stdout/stderr use one bounded private pipe, never default log files.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.component_selection import load_component_selection
from creator_loop.database import open_readonly, validate
from creator_loop.local_components import (
    ComponentPreflightError,
    VerifiedComponent,
    hold_verified_components,
)
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.private_pipe import PrivatePipe
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.whisper_adapter import RUNTIME_FILES, _profile
from creator_loop.windows_paths import file_io_path

MAX_VERSION_OUTPUT = 32 * 1024
_retained: list[tuple[ExitStack, PrivatePipe, OwnedWindowsProcess | None]] = []


def parse_engine_version(raw: bytes) -> str:
    """Require exactly a bounded version receipt, not arbitrary engine output."""
    if len(raw) > MAX_VERSION_OUTPUT:
        raise ValueError("Engine version output exceeds metadata budget")
    match = re.fullmatch(
        rb"whisper\.cpp version: ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\r?\n?", raw
    )
    if match is None:
        raise ValueError("Engine version response is not the expected metadata")
    return match[1].decode("ascii")


def inspect_amd64_image(item: VerifiedComponent) -> None:
    """Check bounded PE headers from the held file; does not load any DLL."""
    stream = item.stream
    try:
        stream.seek(0)
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Engine runtime file is not a PE image")
        offset = int.from_bytes(header[60:64], "little")
        if not 64 <= offset <= min(item.spec.byte_size - 26, 16 * 1024 * 1024):
            raise ValueError("Engine PE header offset is invalid")
        stream.seek(offset)
        header = stream.read(26)
        if (
            len(header) != 26
            or header[:4] != b"PE\x00\x00"
            or int.from_bytes(header[4:6], "little") != 0x8664
            or int.from_bytes(header[24:26], "little") != 0x20B
        ):
            raise ValueError("Engine runtime requires an AMD64 PE32+ image")
        optional_size = int.from_bytes(header[20:22], "little")
        flags = int.from_bytes(header[22:24], "little")
        expected_dll = item.spec.path.suffix.casefold() == ".dll"
        if (
            optional_size < 112  # PE32+ standard + Windows fields before directories
            or offset + 24 + optional_size > item.spec.byte_size
            or not flags & 2
            or bool(flags & 0x2000) != expected_dll
        ):
            raise ValueError("Engine runtime PE type/header is invalid")
    finally:
        stream.seek(0)


def _refuse_restore_guard(root: Path) -> None:
    # lstat refuses every existing guard entry, including a dangling link.
    try:
        file_io_path(root / "runtime/restore-in-progress.json").lstat()
    except FileNotFoundError:
        return
    raise RuntimeError("Restore guard blocks engine version checks")


def probe_selected_whisper_cli(
    root: Path,
    *,
    timeout_seconds: float = 15,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, object]:
    """Recheck selected CPU profile and run --version under its declared Job cap.

    No selection/DB/domain changes, model execution, download or activation.
    Version startup does not prove all delay-loaded dependencies, model format,
    quality, resource pressure, clean-target compatibility or V1 acceptance.
    """
    if sys.platform != "win32":
        raise OSError("Native engine version checks require Windows")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        raise ValueError("Engine version deadline must be within 30 seconds")
    canonical = root.resolve(strict=True)
    if canonical != root.absolute():
        raise ValueError("A canonical data root is required")
    resources = ExitStack()
    pipe: PrivatePipe | None = None
    process: OwnedWindowsProcess | None = None
    clean = True
    try:
        resources.enter_context(AppDataLock(canonical))
        resources.enter_context(
            RuntimeHandle(canonical, directory=True, allow_child_writes=True)
        )
        resources.enter_context(
            RuntimeHandle(
                canonical / "runtime", directory=True, allow_child_writes=True
            )
        )
        resources.enter_context(
            RuntimeHandle(canonical / "logs", directory=True, allow_child_writes=True)
        )
        _refuse_restore_guard(canonical)
        resources.enter_context(
            RuntimeHandle(canonical / "creator_loop.sqlite3", read_only=True)
        )
        with closing(open_readonly(canonical / "creator_loop.sqlite3")) as db:
            validate(db)
        selection = load_component_selection(canonical)
        if selection is None:
            raise ComponentPreflightError(
                "Select local components before checking the engine"
            )
        executable, _model, runtime = _profile(selection.specs)
        verified = resources.enter_context(
            hold_verified_components(
                selection.specs,
                worker_memory_limit=selection.worker_memory_limit,
                cancelled=cancelled,
            )
        )
        resources.enter_context(RuntimeHandle(runtime, directory=True))
        if {entry.name for entry in runtime.iterdir()} != RUNTIME_FILES:
            raise ValueError("Unexpected file in selected engine runtime")
        for item in verified:
            if item.spec.kind == "ENGINE":
                inspect_amd64_image(item)
        _refuse_restore_guard(canonical)
        if cancelled():
            raise InterruptedError("Engine version check cancelled before launch")
        pipe = PrivatePipe(eager_reads=True)
        log_directory = canonical / "logs" / ("engine-version-" + uuid4().hex)
        environment = {
            **os.environ,
            "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        }
        process = OwnedWindowsProcess(
            executable.path,
            ["--version"],
            log_directory,
            component_version="whisper.cpp/1.8.7/version-only",
            environment=environment,
            cwd=Path(environment["PATH"]),
            capture_output=False,
            stdout_sink=pipe.writer,
            stderr_sink=pipe.writer,
            memory_limit_bytes=selection.worker_memory_limit,
        )
        pipe.writer.close()
        # Both parent duplicates must close after resume; child handles retain
        # their endpoints. Otherwise native child exit cannot prove pipe EOF.
        process.streams[1].close()
        process.streams[2].close()
        output = bytearray()
        deadline = time.monotonic() + timeout_seconds
        while True:
            output.extend(pipe.read_available(8192))
            if len(output) > MAX_VERSION_OUTPUT:
                raise ValueError("Engine version output exceeds metadata budget")
            if cancelled():
                raise InterruptedError("Engine version check cancelled")
            if process.tree_finished() and pipe.eof:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Engine version check exceeded deadline")
            time.sleep(0.01)
        outcome = process.wait(1)
        if outcome.exit_code != 0 or outcome.timed_out:
            raise RuntimeError(
                "Engine CLI startup failed; runtime prerequisites remain unverified"
            )
        version = parse_engine_version(bytes(output))
        if version != executable.version:
            raise ValueError(
                "Engine reported a different version from selected metadata"
            )
        if {entry.name for entry in runtime.iterdir()} != RUNTIME_FILES:
            raise ValueError("Engine runtime inventory changed during version check")
        _refuse_restore_guard(canonical)
        return {
            "check": "ENGINE_CLI_VERSION_CONFIRMED",
            "engine_version": version,
            "engine_component_id": executable.component_id,
            "engine_sha256": executable.sha256,
            "pe_amd64_verified": True,
            "model_executed": False,
            "model_compatibility_verified": False,
            "runtime_compatibility_verified": False,
            "worker_memory_limit": selection.worker_memory_limit,
            "seconds": outcome.elapsed_seconds,
        }
    finally:
        try:
            if process is not None:
                process.close()
            if pipe is not None:
                deadline = time.monotonic() + 5
                while not pipe.try_close():
                    if time.monotonic() >= deadline:
                        clean = False
                        raise RuntimeError(
                            "Private engine pipe cancellation is incomplete; leases retained"
                        )
                    time.sleep(0.01)
        except BaseException:
            clean = False
            if pipe is not None:
                _retained.append((resources, pipe, process))
            raise
        finally:
            if clean:
                resources.close()


def check_whisper_runtime_cli(root: Path) -> int:
    """Explicit closed-app version probe; errors expose only bounded categories."""
    import json

    try:
        result = probe_selected_whisper_cli(root)
    except DataRootBusy:
        result = {
            "check": "REFUSED",
            "reason": "Close the app using this data root before checking the engine",
        }
        code = 3
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        result = {
            "check": "REFUSED",
            "reason": type(exc).__name__,
            "runtime_compatibility_verified": False,
        }
        code = 1
    else:
        code = 0
    print(json.dumps(result))
    return code
