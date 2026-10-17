"""Launch source workers with the exact native interpreter, not a venv PID proxy."""

import os
import sys
from pathlib import Path


def python_worker(
    marker: dict[str, object], environment: dict[str, str] | None = None
) -> tuple[Path, list[str], dict[str, str]]:
    parent = marker.get("parent")
    if not isinstance(parent, dict) or not isinstance(parent.get("executable"), str):
        raise ValueError("Worker requires exact parent interpreter identity")
    env = (environment or os.environ).copy()
    if getattr(sys, "frozen", False):
        return Path(sys.executable), [], env
    # Preserve the already-running source environment's verified package paths.
    # Executing a Windows venv .exe would insert a different native child PID.
    env["PYTHONPATH"] = os.pathsep.join(
        str(Path(path).resolve()) for path in sys.path if path
    )
    return Path(parent["executable"]), ["-m", "creator_loop"], env
