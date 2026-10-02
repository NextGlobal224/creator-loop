"""Resolve media keys consistently before adding registered storage volumes."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Literal

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
        or parts[:2] != ["storage", expected_folder]
        or any(part in ("", ".", "..") for part in parts)
        or any(
            char in storage_key
            for char in ("\\", "\x00", ":", "<", ">", '"', "|", "?", "*")
        )
        or PureWindowsPath(storage_key).drive
        or PureWindowsPath(storage_key).root
    ):
        raise StoragePathError("unsafe_path")
    try:
        canonical_root = Path(data_root).resolve()
        folder = (canonical_root / "storage" / expected_folder).resolve()
        candidate = canonical_root.joinpath(*parts).resolve()
    except OSError as exc:
        raise StoragePathError("unavailable_file") from exc
    if not folder.is_relative_to(canonical_root) or not candidate.is_relative_to(
        folder
    ):
        raise StoragePathError("unsafe_path")
    return candidate
