"""Worker process ownership primitives.

A PID alone is never sufficient proof that a process is owned by Creator Loop.
Ownership records are persisted explicitly and invalid records fail closed.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def _canonical_executable(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    executable: str
    creation_identity: str


@dataclass(frozen=True)
class WorkerOwnership:
    run_id: str
    pid: int
    executable: str
    creation_identity: str


def owns_process(
    record: WorkerOwnership,
    actual: ProcessIdentity,
) -> bool:
    """Return True only when the recorded worker matches the live identity."""
    return (
        record.pid == actual.pid
        and _canonical_executable(record.executable)
        == _canonical_executable(actual.executable)
        and record.creation_identity == actual.creation_identity
    )


def save_ownership(path: Path, record: WorkerOwnership) -> None:
    """Persist one ownership record atomically enough for local runtime use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(record), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_ownership(path: Path) -> WorkerOwnership | None:
    """Load a valid ownership record; malformed data fails closed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if set(raw) != {"run_id", "pid", "executable", "creation_identity"}:
            return None

        run_id = raw["run_id"]
        pid = raw["pid"]
        executable = raw["executable"]
        creation_identity = raw["creation_identity"]

        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(executable, str)
            or not executable
            or not isinstance(creation_identity, str)
            or not creation_identity
        ):
            return None

        return WorkerOwnership(
            run_id=run_id,
            pid=pid,
            executable=executable,
            creation_identity=creation_identity,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
