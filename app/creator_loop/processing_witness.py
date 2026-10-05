"""Runtime executor proof for startup reconciliation; SQLite owns domain state."""

from __future__ import annotations

import json
import os
from pathlib import Path

from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import create_workspace_marker


def record_processing_executor(
    root: Path,
    run_id: str,
    asset_id: str,
    input_file_id: str,
    task_type: str,
    component_version: str,
) -> Path:
    if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
        raise ValueError("Invalid processing run identity")
    directory = root / "runtime" / ("processing-" + run_id)
    with RuntimeHandle(root / "runtime", directory=True, allow_child_writes=True):
        directory.mkdir()
        with RuntimeHandle(directory, directory=True, allow_child_writes=True):
            create_workspace_marker(
                root, directory, component_version, kind="PROCESSING_EXECUTOR_WITNESS"
            )
            payload = {
                "format": 1,
                "run_id": run_id,
                "asset_id": asset_id,
                "input_file_id": input_file_id,
                "task_type": task_type,
                "component_version": component_version,
            }
            with (directory / "task.json").open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
    return directory
