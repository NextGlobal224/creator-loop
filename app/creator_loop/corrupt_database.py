"""Retain and identify damaged SQLite bytes; never call them a valid backup.

This lease is a prerequisite for a future explicit corrupt-source assessment,
not permission to replace a database or execute a recovery candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from creator_loop.app_lock import AppDataLock
from creator_loop.publication_media import _open_read_lock

_NAMES = tuple(
    "creator_loop.sqlite3" + suffix for suffix in ("", "-wal", "-shm", "-journal")
)


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _regular(path: Path) -> os.stat_result | None:
    if path.is_symlink() or path.is_junction():
        raise ValueError("Linked database/sidecar cannot be assessed")
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError("Database/sidecar must be a regular unaliased file")
    return value


def _damage_code(path: Path, check: Callable[[], None], deadline: float) -> int:
    """Only integrity failure or SQLITE_CORRUPT/NOTADB in an immutable view.

    Busy, permissions, readonly WAL setup, missing files and unsupported app
    schemas are not corruption evidence. Never emit SQLite diagnostic text.
    Immutable mode prevents sidecar creation/recovery. This inspection does not
    assess WAL/hot-journal recoverability; no restore decision may infer it.
    https://www.sqlite.org/uri.html
    """
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            db.execute(
                f"PRAGMA busy_timeout={max(1, min(10000, int((deadline - time.monotonic()) * 1000)))}"
            )
            db.set_progress_handler(lambda: _progress(check), 1000)
            try:
                check()
                result = db.execute("PRAGMA integrity_check(1)").fetchone()
                check()
                if result != ("ok",):
                    return sqlite3.SQLITE_CORRUPT
            finally:
                db.set_progress_handler(None, 0)
    except sqlite3.DatabaseError as exc:
        check()  # cancellation/deadline is not evidence of database damage
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return code
        raise
    raise ValueError("Readable database requires ordinary restore assessment")


def _progress(check: Callable[[], None]) -> int:
    try:
        check()
    except (InterruptedError, TimeoutError):
        return 1
    return 0


@contextmanager
def hold_corrupt_database(
    root: Path,
    *,
    timeout_seconds: float = 60,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Own app lock and readonly DB/sidecar handles through the caller's review.

    Hashing is streamed. Windows sharing refuses concurrent write/delete;
    portable tests prove snapshot checks only. Blocking OS I/O still requires
    an outer process deadline. No files are copied, replaced, deleted or repaired.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Damage inspection needs a bounded work deadline")
    canonical = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(canonical)):
        raise ValueError("Canonical data root required")
    for name in ("runtime", "manifests"):
        path = canonical / name
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            raise ValueError("Real coordination folders required")
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Damage inspection cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Damage inspection exceeded its work budget")

    check()
    with AppDataLock(canonical), ExitStack() as handles:
        if any(
            path.name != "app-data.lock" for path in (canonical / "runtime").iterdir()
        ):
            raise RuntimeError("Resolve retained runtime work before damage inspection")
        observed: dict[str, tuple[BinaryIO, os.stat_result] | None] = {}
        fingerprints: list[dict[str, Any]] = []
        # Acquire every present source handle before SQLite reads or hashing.
        for name in _NAMES:
            check()
            path = canonical / name
            before = _regular(path)
            if before is None:
                if name == _NAMES[0]:
                    raise ValueError("Missing database requires separate recovery")
                observed[name] = None
                fingerprints.append({"name": name, "present": False})
                continue
            stream = handles.enter_context(_open_read_lock(path))
            actual = os.fstat(stream.fileno())
            if _identity(before) != _identity(actual):
                raise RuntimeError("Database/sidecar changed before acquiring lease")
            observed[name] = (stream, actual)
        code = _damage_code(canonical / _NAMES[0], check, deadline)
        for name, held in observed.items():
            if held is None:
                continue
            stream, before = held
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024**2):
                check()
                digest.update(chunk)
                size += len(chunk)
            if size != before.st_size or _identity(
                os.fstat(stream.fileno())
            ) != _identity(before):
                raise RuntimeError("Database/sidecar changed during inspection")
            fingerprints.append(
                {
                    "name": name,
                    "present": True,
                    "byte_size": size,
                    "sha256": digest.hexdigest(),
                }
            )
        # Reject added/missing/replaced sidecars and portable concurrent writes.
        for name, held in observed.items():
            check()
            path_stat = _regular(canonical / name)
            if (held is None) != (path_stat is None) or (
                held is not None
                and path_stat is not None
                and _identity(held[1]) != _identity(path_stat)
            ):
                raise RuntimeError(
                    "Database/sidecar inventory changed during inspection"
                )
        damage = {
            "damage_inspection_format": 1,
            "sqlite_view": "immutable_readonly_inspection",
            "wal_recoverability_assessed": False,
            "sqlite_primary_error": code,
            "raw_files": sorted(fingerprints, key=lambda entry: entry["name"]),
            "consistent_backup": False,
            "current_changes_assessable": False,
            "restore_authorized": False,
        }
        damage["data_root_identity"] = hashlib.sha256(
            str(canonical).encode("utf-8")
        ).hexdigest()
        damage["damage_identity"] = hashlib.sha256(
            json.dumps(damage, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        yield damage
