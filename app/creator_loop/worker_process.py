"""Worker process ownership primitives.

A PID alone is never sufficient proof that a process is owned by Creator Loop.
"""

import os
from dataclasses import dataclass


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
