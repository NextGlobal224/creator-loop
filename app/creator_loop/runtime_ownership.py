"""Bind decoder runtime files to parent/child creation identities before resume."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes as w
from pathlib import Path
from typing import Any

from creator_loop.owned_process import _api


def process_identity(kernel: Any, handle: int) -> dict[str, object]:
    if sys.platform != "win32":
        raise OSError("Runtime process identity requires Windows")
    times = [w.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(32768)
    size = w.DWORD(len(buffer))
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "executable": str(Path(buffer.value).resolve()),
        "creation_identity": str(
            (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        ),
        "creation_identity_format": "WIN32_FILETIME",
    }


def create_workspace_marker(
    root: Path,
    workspace: Path,
    component_version: str,
    *,
    kind: str = "QT_DECODER_WORKSPACE",
) -> dict[str, object]:
    canonical = root.resolve(strict=True)
    directory = workspace.resolve(strict=True)
    if (
        directory.parent != canonical / "runtime"
        or workspace.is_symlink()
        or workspace.is_junction()
    ):
        raise OSError("Workspace marker requires a real direct runtime child")
    kernel = _api()
    parent = process_identity(kernel, kernel.GetCurrentProcess())
    parent["pid"] = os.getpid()
    marker: dict[str, object] = {
        "format": 1,
        "kind": kind,
        "data_root": str(canonical),
        "workspace": directory.name,
        "component_version": component_version,
        "parent": parent,
    }
    with (directory / "ownership.json").open("x", encoding="utf-8") as stream:
        json.dump(marker, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return marker


def bind_workspace_child(
    workspace: Path, marker: dict[str, object], child: dict[str, object]
) -> None:
    """A separate fsynced file prevents partial rewrite of the parent marker."""
    if workspace.resolve(strict=True).name != marker.get("workspace"):
        raise ValueError("Workspace binding changed before child resume")
    parent = marker.get("parent")
    if not isinstance(parent, dict) or child.get("parent_pid") != parent.get("pid"):
        raise ValueError("Workspace child has a different parent identity")
    if child.get("component_version") != marker.get("component_version"):
        raise ValueError("Workspace child component does not match marker")
    with (workspace / "child-ownership.json").open("x", encoding="utf-8") as stream:
        json.dump(child, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
