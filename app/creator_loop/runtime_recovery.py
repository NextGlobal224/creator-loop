"""Clean only bound, dead decoder workspaces; preserve unknown/live entries."""

from __future__ import annotations

import ctypes
import re
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from creator_loop.app_lock import AppDataLock
from creator_loop.owned_process import _api
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import process_identity

_FILES = {
    "ownership.json",
    "child-ownership.json",
    "request.json",
    "response.json",
    "frame.png",
}
_PLAYBACK_FILES = {
    "ownership.json",
    "child-ownership.json",
    "request.json",
    "control.json",
    "control.pending",
}


@dataclass(frozen=True)
class RuntimeRecovery:
    cleaned: tuple[str, ...]
    preserved: tuple[tuple[str, str], ...]


def _identity_dead(identity: dict[str, Any]) -> bool:
    if sys.platform != "win32":
        raise OSError("Runtime process identity requires Windows")
    pid = identity.get("pid")
    if (
        type(pid) is not int
        or not 0 < pid < 2**32
        or identity.get("creation_identity_format") != "WIN32_FILETIME"
        or not isinstance(identity.get("creation_identity"), str)
        or not re.fullmatch(r"[0-9]{1,20}", identity["creation_identity"])
        or not isinstance(identity.get("executable"), str)
        or not Path(identity["executable"]).is_absolute()
    ):
        raise ValueError("Invalid runtime process identity")
    kernel = _api()
    handle = kernel.OpenProcess(0x00101000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # PID no longer exists; not access denied
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state not in (0, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        if state == 0:
            # A retained terminal process cannot own a live worker. Windows may
            # refuse its image-name query even though this handle proves exit.
            return True
        observed = process_identity(kernel, handle)
        if observed["creation_identity"] != identity["creation_identity"]:
            return True  # original process is gone; never touch reused PID
        if Path(str(observed["executable"])) != Path(identity["executable"]):
            raise ValueError("Runtime executable binding mismatch")
        return False
    finally:
        kernel.CloseHandle(handle)


def _clean_one(root: Path, workspace: Path) -> None:
    if workspace.parent != root / "runtime" or not re.fullmatch(
        r"(?:decode|playback)-[a-z0-9_]{8}", workspace.name
    ):
        raise ValueError("Unsupported workspace name/location")
    with RuntimeHandle(workspace, directory=True) as directory, ExitStack() as stack:
        entries = {entry.name for entry in workspace.iterdir()}
        playback = workspace.name.startswith("playback-")
        allowed = _PLAYBACK_FILES if playback else _FILES
        if "ownership.json" not in entries or not entries <= allowed:
            raise ValueError("Workspace lacks binding or contains unknown files")
        held = {
            name: stack.enter_context(RuntimeHandle(workspace / name))
            for name in sorted(entries)
        }
        marker = held["ownership.json"].read_json()
        if (
            type(marker.get("format")) is not int
            or marker["format"] != 1
            or marker.get("kind")
            != ("QT_PLAYBACK_WORKSPACE" if playback else "QT_DECODER_WORKSPACE")
            or marker.get("data_root") != str(root)
            or marker.get("workspace") != workspace.name
            or not isinstance(marker.get("component_version"), str)
            or not marker["component_version"].startswith(
                "qt-playback/" if playback else "qt-decoder/"
            )
            or not isinstance(marker.get("parent"), dict)
        ):
            raise ValueError("Workspace parent binding is invalid")
        if not _identity_dead(marker["parent"]):
            raise RuntimeError("Workspace parent is still live")
        if "child-ownership.json" in held:
            child = held["child-ownership.json"].read_json()
            if (
                type(child.get("record_format")) is not int
                or child["record_format"] != 1
                or child.get("parent_pid") != marker["parent"].get("pid")
                or child.get("component_version") != marker["component_version"]
            ):
                raise ValueError("Workspace child binding is invalid")
            for key in ("run_id", "job_id"):
                if (
                    not isinstance(child.get(key), str)
                    or UUID(child[key]).hex != child[key]
                ):
                    raise ValueError("Workspace child ownership ID is invalid")
            if not _identity_dead(child):
                raise RuntimeError("Workspace child is still live")
        elif not entries <= {"ownership.json", "request.json"}:
            raise ValueError("Workspace has results without child ownership binding")
        if {entry.name for entry in workspace.iterdir()} != entries:
            raise RuntimeError("Workspace inventory changed during inspection")
        # Retained file handles deny replacement and target deletion by handle.
        # Keep binding markers until all payload files have been discarded.
        ordered = sorted(entries - {"ownership.json", "child-ownership.json"})
        ordered += sorted(entries & {"child-ownership.json", "ownership.json"})
        for name in ordered:
            held[name].discard()
            held[name].close()
        directory.discard()  # only an empty, still-retained verified directory


def recover_runtime_startup(root: Path, coordination: AppDataLock) -> RuntimeRecovery:
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError("Runtime recovery requires this root's held app lock")
    if sys.platform != "win32":
        return RuntimeRecovery((), ())  # hosted portable CI cannot certify Win handles
    runtime = canonical / "runtime"
    if runtime.is_symlink() or runtime.is_junction():
        raise OSError("Runtime recovery requires a real runtime directory")
    cleaned: list[str] = []
    preserved: list[tuple[str, str]] = []
    for workspace in sorted(runtime.iterdir()):
        if not workspace.name.startswith(("decode-", "playback-")):
            continue
        try:
            _clean_one(canonical, workspace)
        except (OSError, ValueError, RuntimeError) as error:
            preserved.append((workspace.name, type(error).__name__))
        else:
            cleaned.append(workspace.name)
    return RuntimeRecovery(tuple(cleaned), tuple(preserved))


def discard_known_playback_workspace(
    workspace: Path, marker: dict[str, object], child: dict[str, object] | None
) -> bool:
    """Normal cleanup preserves an unknown/replaced entry; never recursively delete.

    The caller has already proved its private Job empty. Validate its original
    marker against this live parent and independently prove the bound child dead.
    """
    try:
        root = workspace.parent.parent.resolve(strict=True)
        kernel = _api()
        parent = process_identity(kernel, kernel.GetCurrentProcess())
        import os

        parent["pid"] = os.getpid()
        if (
            workspace.parent != root / "runtime"
            or not re.fullmatch(r"playback-[a-z0-9_]{8}", workspace.name)
            or marker.get("kind") != "QT_PLAYBACK_WORKSPACE"
            or marker.get("data_root") != str(root)
            or marker.get("workspace") != workspace.name
            or marker.get("parent") != parent
        ):
            return False
        with (
            RuntimeHandle(workspace, directory=True) as directory,
            ExitStack() as stack,
        ):
            entries = {entry.name for entry in workspace.iterdir()}
            if "ownership.json" not in entries or not entries <= _PLAYBACK_FILES:
                return False
            held = {
                name: stack.enter_context(RuntimeHandle(workspace / name))
                for name in sorted(entries)
            }
            if held["ownership.json"].read_json() != marker:
                return False
            if "child-ownership.json" in held:
                if (
                    child is None
                    or held["child-ownership.json"].read_json() != child
                    or not _identity_dead(child)
                ):
                    return False
            elif child is not None or not entries <= {"ownership.json", "request.json"}:
                return False
            if {entry.name for entry in workspace.iterdir()} != entries:
                return False
            ordered = sorted(entries - {"ownership.json", "child-ownership.json"})
            ordered += sorted(entries & {"child-ownership.json", "ownership.json"})
            for name in ordered:
                held[name].discard()
                held[name].close()
            directory.discard()
        return True
    except (OSError, ValueError, RuntimeError):
        return False
