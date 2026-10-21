"""Read leases for retained B evidence after its original owner has exited."""

from __future__ import annotations

import re
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from creator_loop.backend.runtime.preservation_identity import (
    preservation_identity_dead,
)
from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_recovery import _identity_dead
from creator_loop.backend.runtime.windows_paths import file_io_path

PREFIXES = {
    "CLOUDFLARE_WORKERS_AI": "creator-workers/",
    "CODEX_CLI_TEXT": "creator-codex/",
    "LOCAL_LLAMA_CPP": "creator-llm/",
}


def hold_creator_evidence(
    root: Path, db: sqlite3.Connection, directories: list[Path], held: ExitStack
) -> Callable[[], None]:
    """No deletion, killing, payload reads or permission for live owners.

    Require a terminal DB owner witness and dead native identities, then deny
    writes/replacement to every known file and directory through the backup.
    Unknown entries retain the ordinary refusal. Restart after B before backup.
    """
    return _hold_creator(root, db, directories, held, None)


def hold_creator_preservation(
    root: Path,
    db: sqlite3.Connection,
    directories: list[Path],
    held: ExitStack,
    opaque: list[dict[str, str]],
) -> Callable[[], None]:
    """Preservation only: archive every leased byte, never qualify orphan outputs.

    Ordinary update backup still requires the matching terminal DB owner.
    """
    return _hold_creator(root, db, directories, held, opaque)


def _hold_creator(
    root: Path,
    db: sqlite3.Connection,
    directories: list[Path],
    held: ExitStack,
    opaque: list[dict[str, str]] | None,
) -> Callable[[], None]:
    check_dead = _identity_dead if opaque is None else preservation_identity_dead
    namespaces: dict[Path, set[str]] = {}
    markers = {}
    children = {}
    for directory in directories:
        if not re.fullmatch(
            r"creator-(?:task|workers(?:-stage)?|codex|llm)-[0-9a-f]{32}",
            directory.name,
        ):
            raise RuntimeError("Unknown B runtime directory")
        held.enter_context(RuntimeHandle(directory, directory=True))
        names = {p.name for p in file_io_path(directory).iterdir()}
        task = directory.name.startswith("creator-task-")
        allowed = (
            {"ownership.json"}
            if task
            else {
                "ownership.json",
                "child-ownership.json",
                "request.json",
                "response.json",
                "stage-progress.json",
                "metrics.json",
                "quality-rejection.json",
                "prompt.txt",
                "response-schema.json",
                "answer.json",
                "response.txt",
                "process",
            }
        )
        if "ownership.json" not in names or not names <= allowed:
            raise RuntimeError("Unknown or incomplete B runtime evidence")
        namespaces[directory] = names
        files = {}
        for name in names:
            if name == "process":
                process = directory / name
                held.enter_context(RuntimeHandle(process, directory=True))
                nested = {p.name for p in file_io_path(process).iterdir()}
                if "ownership.json" not in nested or not nested <= {
                    "ownership.json",
                    "stdout.log",
                    "stderr.log",
                }:
                    raise RuntimeError("Unknown B process evidence")
                namespaces[process] = nested
                for filename in nested:
                    handle = held.enter_context(
                        RuntimeHandle(process / filename, read_only=True)
                    )
                    if filename == "ownership.json":
                        children[directory, "process"] = handle.read_json(
                            reject_duplicates=True
                        )
            else:
                files[name] = held.enter_context(
                    RuntimeHandle(directory / name, read_only=True)
                )
        marker = files["ownership.json"].read_json(reject_duplicates=True)
        if (
            type(marker.get("format")) is not int
            or marker["format"] != 1
            or marker.get("data_root") != str(root)
            or marker.get("workspace") != directory.name
            or marker.get("kind")
            != ("CREATOR_WORKFLOW_OWNER" if task else "CREATOR_LLM_WORKSPACE")
            or not isinstance(marker.get("parent"), dict)
            or not isinstance(marker.get("component_version"), str)
            or not 0 < len(marker["component_version"]) <= 512
            or not check_dead(marker["parent"])
        ):
            raise RuntimeError(
                "B owner must be verified dead; close/reopen before backup"
            )
        markers[directory] = marker
        if "child-ownership.json" in files:
            children[directory, "child"] = files["child-ownership.json"].read_json(
                reject_duplicates=True
            )
    owners = []
    for directory, marker in markers.items():
        if not directory.name.startswith("creator-task-"):
            continue
        row = db.execute(
            "SELECT status,finished_at,tool_name,tool_version FROM processing_runs WHERE run_id=? AND task_type='CREATOR_GENERATION'",
            (directory.name.removeprefix("creator-task-"),),
        ).fetchone()
        if (
            row is None
            or row[0] not in {"SUCCEEDED", "FAILED", "CANCELLED"}
            or not row[1]
            or row[2] not in PREFIXES
            or marker.get("component_version") != row[2] + "/" + row[3]
        ):
            raise RuntimeError("B runtime lacks terminal generation witness")
        owners.append((marker["parent"], PREFIXES[row[2]] + row[3]))
    for directory, marker in markers.items():
        if directory.name.startswith("creator-task-"):
            continue
        component = marker.get("component_version")
        if not any(
            marker["parent"] == parent
            and (
                component == prefix
                or component
                in {
                    prefix + "/" + stage
                    for stage in ("analysis", "writing", "editing", "source_audit")
                }
            )
            for parent, prefix in owners
        ):
            if opaque is None:
                raise RuntimeError("B workspace lacks matching terminal owner")
            opaque.append(
                {
                    "workspace": directory.name,
                    "provenance": "UNMATCHED_TERMINAL_OWNER",
                    "component_version": str(component),
                }
            )
    for (directory, _), child in children.items():
        marker = markers[directory]
        if (
            type(child.get("record_format")) is not int
            or child["record_format"] != 1
            or child.get("parent_pid") != marker["parent"].get("pid")
            or child.get("component_version") != marker.get("component_version")
            or any(
                not isinstance(child.get(key), str)
                or not re.fullmatch(r"[0-9a-f]{32}", child[key])
                for key in ("run_id", "job_id")
            )
            or not check_dead(child)
        ):
            raise RuntimeError("B child is live or unbound")
    for directory in markers:
        if (directory, "process") in children and children.get(
            (directory, "child")
        ) != children[directory, "process"]:
            raise RuntimeError("B child/process identities differ")

    def recheck() -> None:
        for directory, expected in namespaces.items():
            if {p.name for p in file_io_path(directory).iterdir()} != expected:
                raise RuntimeError("B runtime namespace changed during backup")

    recheck()
    return recheck
