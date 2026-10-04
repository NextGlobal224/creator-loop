"""Keep reviewed damaged DB/sidecar bytes separately without replacing live data."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from ctypes import wintypes as w
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from creator_loop import __version__
from creator_loop.corrupt_database import _identity, _regular, hold_corrupt_database
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.windows_owned_file import OwnedWindowsFile, _kernel32


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("flags", w.DWORD),
        ("root", w.HANDLE),
        ("length", w.DWORD),
        ("name", w.WCHAR * 1),
    ]


def _create_file(handles: ExitStack, path: Path) -> BinaryIO:
    if sys.platform == "win32":
        owned = OwnedWindowsFile.create_new(path)
        handles.callback(owned.close)
        return owned.stream
    return handles.enter_context(path.open("x+b"))


def _publish_new_manifest(temporary: Path, destination: Path, stream: BinaryIO) -> None:
    """Atomic non-overwriting publication inside the retained owned directory."""
    if temporary.parent != destination.parent:
        raise ValueError("Manifest publication must stay in the owned directory")
    if sys.platform == "win32":
        import msvcrt

        kernel = _kernel32()
        name = str(destination).encode("utf-16-le")
        size = max(ctypes.sizeof(_RenameInfo), _RenameInfo.name.offset + len(name) + 2)
        buffer = ctypes.create_string_buffer(size)
        info = _RenameInfo.from_buffer(buffer)
        info.flags = 0  # ReplaceIfExists=False, RootDirectory=NULL.
        info.length = len(name)
        ctypes.memmove(
            ctypes.addressof(buffer) + _RenameInfo.name.offset, name, len(name)
        )
        # FileRenameInfo=3, using the original exclusive CREATE_NEW handle.
        # https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info
        if not kernel.SetFileInformationByHandle(
            msvcrt.get_osfhandle(stream.fileno()), 3, buffer, size
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        os.fsync(stream.fileno())
    else:
        os.link(temporary, destination)  # exclusive publication, same filesystem
        temporary.unlink()
        fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def preserve_corrupt_source(
    root: Path,
    reviewed_damage: str,
    *,
    timeout_seconds: float = 180,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Publish exact raw files after fresh review matching, never a SQLite backup.

    An incomplete owned folder remains on failure/crash. No cleanup deletes
    source, sidecars, earlier backups or partial evidence. A returned manifest
    means bytes verified/fsynced; it does not authorize a restore or certify WAL
    recoverability. Outer process timeout is required for blocking filesystem I/O.
    """
    if (
        re.fullmatch(r"[0-9a-f]{64}", reviewed_damage) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Reviewed damaged-source identity and bounded budget required")
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Raw-source preservation cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Raw-source preservation exceeded its work budget")

    with hold_corrupt_database(
        root, timeout_seconds=timeout_seconds, cancelled=cancelled
    ) as damage:
        if damage["damage_identity"] != reviewed_damage:
            raise ValueError("Damaged source changed since review; inspect again")
        canonical = root.resolve(strict=True)
        parent = canonical / "backups"
        if parent.is_symlink() or parent.is_junction() or not parent.is_dir():
            raise ValueError("Real raw-source preservation parent required")
        entries = damage["raw_files"]
        check()
        required = sum(entry.get("byte_size", 0) for entry in entries) + 1024**2
        if shutil.disk_usage(parent).free < required:
            raise OSError("Insufficient space to retain reviewed raw source bytes")
        with ExitStack() as handles:
            if sys.platform == "win32":
                handles.enter_context(
                    RuntimeHandle(parent, directory=True, allow_child_writes=True)
                )
            retention_id = uuid4().hex
            folder = parent / f"raw-source-{retention_id}"
            # Fixed generated name, real parent verified before any file mutation.
            if folder.absolute().parent != parent or folder.resolve().parent != parent:
                raise ValueError("Raw-source destination escaped the verified parent")
            folder.mkdir()
            if sys.platform == "win32":
                handles.enter_context(
                    RuntimeHandle(folder, directory=True, allow_child_writes=True)
                )
            sources: dict[str, tuple[BinaryIO, os.stat_result]] = {}
            retained_files: dict[str, tuple[BinaryIO, os.stat_result]] = {}
            for entry in entries:
                check()
                if not entry["present"]:
                    continue
                source = canonical / entry["name"]
                target = folder / entry["name"]
                before = _regular(source)
                reader = handles.enter_context(_open_read_lock(source))
                if before is None or _identity(before) != _identity(
                    os.fstat(reader.fileno())
                ):
                    raise RuntimeError("Raw-source path changed before retention")
                sources[entry["name"]] = reader, before
                writer = _create_file(handles, target)
                copied, digest = 0, hashlib.sha256()
                while chunk := reader.read(1024**2):
                    check()
                    if writer.write(chunk) != len(chunk):
                        raise OSError(
                            "Incomplete raw-source write; keep partial evidence"
                        )
                    copied += len(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
                if (
                    copied != entry["byte_size"]
                    or digest.hexdigest() != entry["sha256"]
                ):
                    raise RuntimeError(
                        "Retained raw file does not match reviewed source"
                    )
                # Read back the retained exclusive destination handle, not a
                # reopened pathname that could refer to a replacement file.
                retained = writer
                retained.seek(0)
                actual, size = hashlib.sha256(), 0
                while chunk := retained.read(1024**2):
                    check()
                    actual.update(chunk)
                    size += len(chunk)
                if size != copied or actual.hexdigest() != entry["sha256"]:
                    raise RuntimeError("Retained physical bytes failed verification")
                retained_files[entry["name"]] = retained, os.fstat(retained.fileno())
            check()
            for entry in entries:
                source_stat = _regular(canonical / entry["name"])
                if (source_stat is not None) != entry["present"]:
                    raise RuntimeError(
                        "Raw-source sidecar inventory changed before publish"
                    )
                if not entry["present"]:
                    continue
                reader, before = sources[entry["name"]]
                if source_stat is None or _identity(source_stat) != _identity(before):
                    raise RuntimeError("Source changed before raw-source publication")
                reader.seek(0)
                fresh = hashlib.sha256()
                while chunk := reader.read(1024**2):
                    check()
                    fresh.update(chunk)
                if fresh.hexdigest() != entry["sha256"] or _identity(
                    os.fstat(reader.fileno())
                ) != _identity(before):
                    raise RuntimeError(
                        "Source bytes changed before raw-source publication"
                    )
                retained, target_stat = retained_files[entry["name"]]
                path_stat = _regular(folder / entry["name"])
                if path_stat is None or _identity(path_stat) != _identity(target_stat):
                    raise RuntimeError(
                        "Raw-source destination changed before publication"
                    )
            expected = {entry["name"] for entry in entries if entry["present"]}
            if {path.name for path in folder.iterdir()} != expected:
                raise RuntimeError(
                    "Raw-source folder contains unknown entries; preserve evidence"
                )
            record = {
                "raw_source_format": 1,
                "retention_id": retention_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "app_version": __version__,
                "data_root_identity": damage["data_root_identity"],
                "damage_identity": reviewed_damage,
                "damage": damage,
                "raw_bytes_verified": True,
                "consistent_backup": False,
                "wal_recoverability_assessed": False,
                "restore_authorized": False,
                "media_included": False,
            }
            temporary = folder / "raw-source-manifest.json.pending"
            manifest = folder / "raw-source-manifest.json"
            stream = _create_file(handles, temporary)
            body = json.dumps(
                record, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            if stream.write(body) != len(body):
                raise OSError("Incomplete raw-source manifest; keep partial evidence")
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read(len(body) + 1) != body:
                raise RuntimeError("Raw-source manifest readback failed")
            check()
            if {path.name for path in folder.iterdir()} != expected | {temporary.name}:
                raise RuntimeError("Raw-source folder changed before publication")
            _publish_new_manifest(temporary, manifest, stream)
            return manifest
