"""Revalidate held raw damage evidence without opening/replacing the live DB."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from creator_loop.corrupt_database import _NAMES, _identity, _regular
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_files import RuntimeHandle

_MANIFEST = "raw-source-manifest.json"
_RECORD_KEYS = {
    "raw_source_format",
    "retention_id",
    "created_at",
    "app_version",
    "data_root_identity",
    "damage_identity",
    "damage",
    "raw_bytes_verified",
    "consistent_backup",
    "wal_recoverability_assessed",
    "restore_authorized",
    "media_included",
}
_DAMAGE_KEYS = {
    "damage_inspection_format",
    "sqlite_view",
    "sqlite_primary_error",
    "raw_files",
    "consistent_backup",
    "wal_recoverability_assessed",
    "restore_authorized",
    "current_changes_assessable",
    "data_root_identity",
    "damage_identity",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Raw-source manifest has duplicate fields")
        result[key] = value
    return result


def _metadata(
    body: bytes, root_identity: str, retention_id: str, expected_damage: str
) -> dict[str, Any]:
    record = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise ValueError("Unsupported raw-source manifest shape")
    if type(record["raw_source_format"]) is not int or record["raw_source_format"] != 1:
        raise ValueError("Unsupported raw-source format")
    if (
        record["retention_id"] != retention_id
        or record["data_root_identity"] != root_identity
        or record["damage_identity"] != expected_damage
    ):
        raise ValueError("Raw-source manifest root/retention/review binding differs")
    if record["raw_bytes_verified"] is not True or any(
        record[key] is not False
        for key in (
            "consistent_backup",
            "wal_recoverability_assessed",
            "restore_authorized",
            "media_included",
        )
    ):
        raise ValueError("Raw-source evidence cannot grant a backup/restore claim")
    if not isinstance(record["created_at"], str):
        raise ValueError("UTC retention timestamp required")
    created = datetime.fromisoformat(record["created_at"])
    if created.utcoffset() != timedelta(0):
        raise ValueError("UTC retention timestamp required")
    if (
        not isinstance(record["app_version"], str)
        or re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", record["app_version"]
        )
        is None
        or len(record["app_version"]) > 64
    ):
        raise ValueError("Bounded app version metadata required")
    damage = record["damage"]
    if not isinstance(damage, dict) or set(damage) != _DAMAGE_KEYS:
        raise ValueError("Unsupported damaged-source inventory")
    if (
        type(damage["damage_inspection_format"]) is not int
        or damage["damage_inspection_format"] != 1
        or damage["sqlite_view"] != "immutable_readonly_inspection"
    ):
        raise ValueError("Unsupported damaged-source inspection")
    if type(damage["sqlite_primary_error"]) is not int or damage[
        "sqlite_primary_error"
    ] not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        raise ValueError("Damaged view evidence required")
    if any(
        damage[key] is not False
        for key in (
            "consistent_backup",
            "wal_recoverability_assessed",
            "restore_authorized",
            "current_changes_assessable",
        )
    ):
        raise ValueError("Damaged-source evidence has unsupported authorization")
    if (
        damage["data_root_identity"] != root_identity
        or damage["damage_identity"] != expected_damage
    ):
        raise ValueError("Damaged-source root/review binding differs")
    entries = damage["raw_files"]
    if not isinstance(entries, list) or len(entries) != len(_NAMES):
        raise ValueError("Complete raw-source sidecar inventory required")
    for entry, name in zip(entries, sorted(_NAMES), strict=True):
        if (
            not isinstance(entry, dict)
            or entry.get("name") != name
            or type(entry.get("present")) is not bool
        ):
            raise ValueError("Canonical raw-source file inventory required")
        if not entry["present"]:
            if name == _NAMES[0] or set(entry) != {"name", "present"}:
                raise ValueError(
                    "Raw-source main DB and explicit absent-sidecar entry required"
                )
        elif (
            set(entry) != {"name", "present", "byte_size", "sha256"}
            or type(entry["byte_size"]) is not int
            or entry["byte_size"] < 0
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise ValueError("Raw-source file size/digest metadata required")
    payload = {key: value for key, value in damage.items() if key != "damage_identity"}
    computed = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if computed != expected_damage:
        raise ValueError("Raw-source damaged inventory identity mismatch")
    return record


@contextmanager
def hold_preserved_source(
    root: Path,
    manifest: Path,
    expected_damage: str,
    *,
    timeout_seconds: float = 60,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold freshly verified archive bytes, including when live DB is missing.

    The explicit identity comes from fresh damage review or a guarded recovery
    record. This read-only lease does not compare current live data, take the app
    writer lock, initialize SQLite, authorize/apply a restore or salvage WAL.
    A caller applying recovery must own the app lock and revalidate all current
    inputs/consents separately. Outer process timeout is required for OS I/O.
    """
    if (
        not isinstance(expected_damage, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_damage) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Explicit damaged-source identity and bounded budget required")
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Raw-source verification cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Raw-source verification exceeded its work budget")

    check()
    canonical = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(canonical)):
        raise ValueError("Canonical data root required")
    selected = manifest.absolute()
    parent, folder = canonical / "backups", selected.parent
    if (
        selected.name != _MANIFEST
        or folder.parent != parent
        or re.fullmatch(r"raw-source-[0-9a-f]{32}", folder.name) is None
        or selected.resolve(strict=True) != selected
    ):
        raise ValueError("Explicit canonical raw-source manifest required")
    for directory in (canonical, parent, folder):
        if (
            directory.is_symlink()
            or directory.is_junction()
            or not directory.is_dir()
            or directory.resolve(strict=True) != directory
        ):
            raise ValueError("Real raw-source archive directories required")
    with ExitStack() as handles:
        if sys.platform == "win32":
            for directory in (canonical, parent):
                handles.enter_context(
                    RuntimeHandle(directory, directory=True, allow_child_writes=True)
                )
            handles.enter_context(RuntimeHandle(folder, directory=True))
        observed: dict[str, tuple[BinaryIO, os.stat_result]] = {}

        def acquire(path: Path) -> BinaryIO:
            check()
            before = _regular(path)
            if before is None:
                raise ValueError("Required raw-source entry is missing")
            stream = handles.enter_context(_open_read_lock(path))
            actual = os.fstat(stream.fileno())
            if _identity(before) != _identity(actual):
                raise RuntimeError("Raw-source entry changed before acquiring lease")
            observed[path.name] = stream, actual
            return stream

        stream = acquire(selected)
        if not 0 < os.fstat(stream.fileno()).st_size <= 16384:
            raise ValueError("Raw-source manifest exceeds its metadata budget")
        body = stream.read(16385)
        check()
        record = _metadata(
            body,
            hashlib.sha256(str(canonical).encode("utf-8")).hexdigest(),
            folder.name[len("raw-source-") :],
            expected_damage,
        )
        entries = record["damage"]["raw_files"]
        expected = {_MANIFEST} | {
            entry["name"] for entry in entries if entry["present"]
        }

        def inventory() -> None:
            check()
            if {path.name for path in folder.iterdir()} != expected:
                raise ValueError("Partial or unknown raw-source archive inventory")
            for name, (held, before) in observed.items():
                check()
                after = _regular(folder / name)
                if (
                    after is None
                    or _identity(before) != _identity(after)
                    or _identity(before) != _identity(os.fstat(held.fileno()))
                ):
                    raise RuntimeError("Raw-source archive changed during verification")

        inventory()
        # Hold all present raw handles before streaming verification.
        for entry in entries:
            if entry["present"]:
                acquire(folder / entry["name"])
        for entry in entries:
            if not entry["present"]:
                continue
            held, before = observed[entry["name"]]
            if before.st_size != entry["byte_size"]:
                raise ValueError("Raw-source retained file size mismatch")
            digest, size = hashlib.sha256(), 0
            while chunk := held.read(1024**2):
                check()
                digest.update(chunk)
                size += len(chunk)
            if size != entry["byte_size"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError("Raw-source retained bytes/digest mismatch")
        inventory()
        verified = {
            **record,
            "archive_revalidated": True,
            "archive_manifest_sha256": hashlib.sha256(body).hexdigest(),
        }
        yield verified
        inventory()
