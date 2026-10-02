"""Resolve safe media keys in user data or an identified registered volume."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Literal

from creator_loop.storage_roots import StorageRootError, resolve_registered_root

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
            canonical_root = Path(data_root).resolve()
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
        folder = canonical_root.joinpath(*folder_parts).resolve()
        candidate = canonical_root.joinpath(*relative_parts).resolve()
    except OSError as exc:
        raise StoragePathError("unavailable_file") from exc
    if not folder.is_relative_to(canonical_root) or not candidate.is_relative_to(
        folder
    ):
        raise StoragePathError("unsafe_path")
    return candidate
