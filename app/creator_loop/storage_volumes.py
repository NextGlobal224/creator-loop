"""Identify the volume behind a path rather than trusting a drive letter."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_mount = _kernel32.GetVolumePathNameW
    _get_guid = _kernel32.GetVolumeNameForVolumeMountPointW
    for _function in (_get_mount, _get_guid):
        _function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _function.restype = wintypes.BOOL

    def _windows_volume_id(path: str) -> str:
        mount = ctypes.create_unicode_buffer(32768)
        if not _get_mount(path, mount, len(mount)):
            raise ctypes.WinError(ctypes.get_last_error())
        guid = ctypes.create_unicode_buffer(128)
        if not _get_guid(mount.value, guid, len(guid)):
            raise ctypes.WinError(ctypes.get_last_error())
        return guid.value.casefold()


def volume_identity(path: Path) -> str:
    """Return a Windows volume GUID, or a device identity for portable CI.

    The path must exist. Windows network/unsupported volumes fail explicitly;
    a registered root may not fall back to a drive letter or another volume.
    """
    canonical = Path(path).resolve(strict=True)
    if sys.platform == "win32":
        return _windows_volume_id(str(canonical))
    return f"posix-device:{canonical.stat().st_dev}"
