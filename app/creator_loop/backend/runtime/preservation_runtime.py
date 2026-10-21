"""Private preservation of historical A/component workspaces, never recovery."""

from __future__ import annotations

import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_recovery import _identity_dead
from creator_loop.backend.runtime.windows_paths import file_io_path

_WORKSPACE = re.compile(
    r"(facebook|semantic|piper|vieneu-nano|voice-request)-[0-9a-f]{32}"
)
_ID = re.compile(r"[0-9a-f]{32}")
_COMMON = {
    "ownership.json",
    "child-ownership.json",
    "request.json",
    "result.json",
    "metrics.json",
    "terminal.json",
    "error.json",
    "process",
    "narration.wav",
}


def preservation_family(name: str) -> str | None:
    match = _WORKSPACE.fullmatch(name)
    return match[1] if match else None


def hold_historical_preservation(
    root: Path,
    directories: list[Path],
    held: ExitStack,
    opaque: list[dict[str, str]],
) -> Callable[[], None]:
    """No runtime execution, row changes, deletion, or terminal provenance claims.

    Ordinary backup never calls this. Bound executor markers require the same
    native dead-parent/dead-child proof as B. The voice-request-only text folder
    is an input descriptor, not an executor: freeze its exact bytes/namespace,
    retain it as unowned evidence, and never infer execution or cleanup rights.
    """
    namespaces: dict[Path, set[str]] = {}

    def retain(directory: Path) -> dict[str, Any]:
        held.enter_context(RuntimeHandle(directory, directory=True))
        names = {p.name for p in file_io_path(directory).iterdir()}
        namespaces[directory] = names
        return {
            name: held.enter_context(RuntimeHandle(directory / name, read_only=True))
            for name in names
            if name != "process"
        }

    def child_matches(child: dict, marker: dict) -> None:
        if (
            type(child.get("record_format")) is not int
            or child["record_format"] != 1
            or type(child.get("parent_pid")) is not int
            or child["parent_pid"] != marker["parent"].get("pid")
            or child.get("component_version") != marker["component_version"]
            or any(
                not isinstance(child.get(k), str) or not _ID.fullmatch(child[k])
                for k in ("run_id", "job_id")
            )
            or not _identity_dead(child)
        ):
            raise RuntimeError("Historical child is unbound or still live")

    for directory in directories:
        family = preservation_family(directory.name)
        if directory.parent != root / "runtime" or family is None:
            raise RuntimeError("Unsupported historical preservation namespace")
        files = retain(directory)
        names = namespaces[directory]
        if family == "voice-request":
            if names != {"voice-request.txt"}:
                raise RuntimeError("Unknown voice input descriptor entries")
            opaque.append(
                {
                    "workspace": directory.name,
                    "provenance": "UNOWNED_INPUT_DESCRIPTOR_NOT_EXECUTOR",
                    "component_version": "unknown",
                }
            )
            continue
        allowed = (
            {
                "ownership.json",
                "child-ownership.json",
                "request.json",
                "result.json",
                "stop",
            }
            if family == "facebook"
            else _COMMON
        )
        if "ownership.json" not in names or any(
            name not in allowed
            and not (
                family in {"piper", "vieneu-nano"}
                and re.fullmatch(r"speech-scene-[1-9][0-9]{0,2}\.wav", name)
            )
            for name in names
        ):
            raise RuntimeError("Unknown historical runtime evidence")
        marker = files["ownership.json"].read_json(reject_duplicates=True)
        if (
            type(marker.get("format")) is not int
            or marker["format"] != 1
            or marker.get("kind")
            != (
                "FACEBOOK_BROWSER_WORKSPACE"
                if family == "facebook"
                else "AC_COMPONENT_WORKSPACE"
            )
            or marker.get("data_root") != str(root)
            or marker.get("workspace") != directory.name
            or not isinstance(marker.get("component_version"), str)
            or not 0 < len(marker["component_version"]) <= 512
            or not isinstance(marker.get("parent"), dict)
            or not _identity_dead(marker["parent"])
        ):
            raise RuntimeError("Historical owner must be bound and verified dead")
        child = None
        if "child-ownership.json" in files:
            child = files["child-ownership.json"].read_json(reject_duplicates=True)
            child_matches(child, marker)
        if "process" in names:
            nested = retain(directory / "process")
            if "ownership.json" not in nested or not namespaces[
                directory / "process"
            ] <= {"ownership.json", "stdout.log", "stderr.log"}:
                raise RuntimeError("Unknown historical process evidence")
            nested_child = nested["ownership.json"].read_json(reject_duplicates=True)
            if nested_child != child:
                raise RuntimeError("Historical child/process identities differ")
        opaque.append(
            {
                "workspace": directory.name,
                "provenance": "PRESERVED_OWNER_BOUND_HISTORY_NOT_EXECUTION_PROOF",
                "component_version": marker["component_version"],
            }
        )

    def recheck() -> None:
        for directory, expected in namespaces.items():
            if {p.name for p in file_io_path(directory).iterdir()} != expected:
                raise RuntimeError("Historical runtime namespace changed during backup")

    recheck()
    return recheck
