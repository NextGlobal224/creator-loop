"""Resolve safe media keys in user data or an identified registered volume."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Literal

from creator_loop.storage_roots import (
    StorageRootError,
    default_storage_root_id,
    resolve_registered_root,
)
from creator_loop.windows_paths import resolve_file_path

PathFailure = Literal["unsafe_path", "unavailable_file"]


class StoragePathError(ValueError):
    def __init__(self, reason: PathFailure) -> None:
        self.reason = reason
        super().__init__(reason.replace("_", " "))


def resolve_storage_path(data_root: Path, role: str, storage_key: str) -> Path:
    """Resolve only a safe relative key inside the recorded role's media folder.

    This path alone does not verify a file's bytes, grant ownership or certify
    availability. Callers retain their digest/size checks and error types.
    """
    if not isinstance(storage_key, str):
        raise StoragePathError("unsafe_path")
    parts = storage_key.split("/")
    expected_folder = "originals" if role == "ORIGINAL" else "derived"
    if (
        len(parts) < 3
        or any(part in ("", ".", "..") for part in parts)
        or any(
            char in storage_key
            for char in ("\\", "\x00", ":", "<", ">", '"', "|", "?", "*")
        )
        or PureWindowsPath(storage_key).drive
        or PureWindowsPath(storage_key).root
    ):
        raise StoragePathError("unsafe_path")
    if parts[:2] == ["storage", expected_folder]:
        try:
            canonical_root = resolve_file_path(Path(data_root))
        except OSError as exc:
            raise StoragePathError("unavailable_file") from exc
        relative_parts = parts
        folder_parts = ["storage", expected_folder]
    elif len(parts) >= 4 and parts[0] == "registered" and parts[2] == expected_folder:
        try:
            canonical_root = resolve_registered_root(data_root, parts[1])
        except (StorageRootError, OSError) as exc:
            raise StoragePathError("unavailable_file") from exc
        relative_parts = parts[2:]
        folder_parts = [expected_folder]
    else:
        raise StoragePathError("unsafe_path")
    try:
        folder = resolve_file_path(canonical_root.joinpath(*folder_parts))
        candidate = resolve_file_path(canonical_root.joinpath(*relative_parts))
    except OSError as exc:
        raise StoragePathError("unavailable_file") from exc
    if not folder.is_relative_to(canonical_root) or not candidate.is_relative_to(
        folder
    ):
        raise StoragePathError("unsafe_path")
    return candidate


def new_storage_destination(data_root: Path, role: str, name: str) -> tuple[str, Path]:
    """Prepare a safe destination for future media, never fallback on volume failure."""
    if "/" in name or not name:
        raise StoragePathError("unsafe_path")
    default = default_storage_root_id(data_root)
    folder = "originals" if role == "ORIGINAL" else "derived"
    key = (
        f"storage/{folder}/{name}"
        if default is None
        else f"registered/{default}/{folder}/{name}"
    )
    destination = resolve_storage_path(data_root, role, key)
    if default is None:
        # Do not recreate a missing data root or registered media root.
        (Path(data_root).resolve(strict=True) / "storage").mkdir(exist_ok=True)
    destination.parent.mkdir(exist_ok=True)
    # Validate again after directory creation, including volume identity.
    if resolve_storage_path(data_root, role, key) != destination:
        raise StoragePathError("unsafe_path")
    return key, destination
