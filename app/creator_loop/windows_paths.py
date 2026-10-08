"""Extended Win32 file-I/O spelling, without changing canonical path identities.

This is not a containment, ownership or link check. Callers must retain those
checks on the original paths before using the alias for file operations.
https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def file_io_path(path: Path) -> Path:
    """Use Unicode extended paths without an administrator registry change."""
    if sys.platform != "win32":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\.\\"):
        raise ValueError("Device namespaces are not installation file paths")
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def resolve_file_path(path: Path, *, strict: bool = False) -> Path:
    """Resolve through extended I/O, returning the normal canonical identity.

    Resolution still follows links; callers retain their containment and
    ownership checks. The extended prefix is never persisted in metadata.
    """
    resolved = file_io_path(path).resolve(strict=strict)
    value = str(resolved)
    if sys.platform == "win32":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)
