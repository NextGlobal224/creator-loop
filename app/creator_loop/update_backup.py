"""Validated DB-only update snapshots; never migrate or modify user media here."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop import __version__
from creator_loop.app_lock import AppDataLock
from creator_loop.backup_runtime import hold_terminal_audio_runtime
from creator_loop.database import SCHEMA_VERSION, _sqlite_uri, open_readonly, validate
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.storage_roots import list_storage_roots
from creator_loop.windows_paths import file_io_path, resolve_file_path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with file_io_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(db: sqlite3.Connection, root: Path) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for file_id, role, key, size, digest in db.execute(
        "SELECT file_id,role,storage_key,byte_size,sha256 FROM asset_files ORDER BY file_id"
    ):
        path = resolve_storage_path(root, role, key)
        # Availability and recorded sizes, not a full media-byte backup/audit.
        if (
            not file_io_path(path).is_file()
            or file_io_path(path).stat().st_size != size
        ):
            raise RuntimeError("Storage reference unavailable or size mismatch")
        inventory.append(
            {
                "file_id": file_id,
                "role": role,
                "storage_key": key,
                "byte_size": size,
                "sha256": digest,
            }
        )
    return inventory


def create_update_backup(root: Path, *, timeout_seconds: float = 30) -> Path:
    """Acquire app and DB writer locks, then publish one validated backup directory.

    Run with the app closed. Unresolved runtime records and active processing
    runs require worker recovery first; this function never kills a PID.
    The returned metadata explicitly describes a DB-only snapshot. Originals
    and derived files remain at their existing storage keys.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Backup timeout must be positive")
    canonical = resolve_file_path(root, strict=True)
    with AppDataLock(canonical):
        return _create_locked(canonical, timeout_seconds)


def _create_locked(root: Path, timeout_seconds: float) -> Path:
    source = root / "creator_loop.sqlite3"
    folder = root / "backups"
    if (
        not file_io_path(source).is_file()
        or file_io_path(source).is_symlink()
        or file_io_path(source).is_junction()
        or not file_io_path(folder).is_dir()
        or file_io_path(folder).is_symlink()
        or file_io_path(folder).is_junction()
    ):
        raise OSError("A real database and backup directory are required")

    # mode=rw prevents accidentally creating a missing database. Do not change
    # journal mode, schema, migration history or any domain rows on this path.
    with closing(
        sqlite3.connect(
            _sqlite_uri(source) + "?mode=rw", uri=True, timeout=timeout_seconds
        )
    ) as guard:
        guard.execute("PRAGMA foreign_keys=ON")
        guard.execute("BEGIN IMMEDIATE")
        try:
            return _backup_from_guard(root, guard, timeout_seconds)
        finally:
            guard.rollback()


def _backup_from_guard(
    root: Path,
    guard: sqlite3.Connection,
    timeout_seconds: float,
    *,
    validate_storage: bool = True,
) -> Path:
    """Caller owns app lock and the source DB's BEGIN IMMEDIATE transaction."""
    if not guard.in_transaction:
        raise RuntimeError("Backup requires the source writer transaction")
    source = root / "creator_loop.sqlite3"
    folder = root / "backups"
    if (
        not file_io_path(source).is_file()
        or file_io_path(source).is_symlink()
        or file_io_path(source).is_junction()
        or not file_io_path(folder).is_dir()
        or file_io_path(folder).is_symlink()
        or file_io_path(folder).is_junction()
    ):
        raise OSError("A real database and backup directory are required")

    version = guard.execute("PRAGMA user_version").fetchone()[0]
    if not 1 <= version <= SCHEMA_VERSION:
        raise RuntimeError("Unsupported source schema")
    validate(guard, expected_version=version)
    if guard.execute(
        "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
    ).fetchone():
        raise RuntimeError("Active processing runs require recovery before backup")
    required = (
        guard.execute("PRAGMA page_count").fetchone()[0]
        * guard.execute("PRAGMA page_size").fetchone()[0]
    )
    if shutil.disk_usage(file_io_path(folder)).free < required * 2 + 1024 * 1024:
        raise OSError("Insufficient space for validated DB backup")
    with hold_terminal_audio_runtime(root, guard) as recheck_runtime:
        return _snapshot(
            root,
            source,
            folder,
            version,
            timeout_seconds,
            validate_storage=validate_storage,
            recheck_runtime=recheck_runtime,
        )


def _snapshot(
    root: Path,
    source: Path,
    folder: Path,
    version: int,
    timeout_seconds: float,
    *,
    validate_storage: bool = True,
    recheck_runtime: Callable[[], None] | None = None,
) -> Path:
    backup_id = uuid4().hex
    stage = folder / f".{backup_id}.staging"
    completed = folder / backup_id
    # mkdir and file writes are the write-permission preflight. All cleanup
    # targets are exclusively created by this invocation under backups/.
    file_io_path(stage).mkdir()
    try:
        probe = stage / "write-probe"
        with file_io_path(probe).open("xb") as stream:
            stream.write(b"Creator Loop backup preflight\n")
            stream.flush()
            os.fsync(stream.fileno())
        file_io_path(probe).unlink()
        snapshot = stage / "creator_loop.sqlite3"
        deadline = time.monotonic() + timeout_seconds

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError("SQLite backup timed out")

        # Separate reader sees committed WAL pages while guard blocks all DB
        # writers. Calling backup() on guard itself would hang in its write txn.
        with (
            closing(open_readonly(source)) as reader,
            closing(sqlite3.connect(_sqlite_uri(snapshot), uri=True)) as target,
        ):
            reader.backup(target, pages=256, progress=progress, sleep=0.01)
        with closing(open_readonly(snapshot)) as copy:
            validate(copy, expected_version=version)
            inventory = (
                _inventory(copy, root)
                if validate_storage
                else [
                    dict(
                        zip(
                            ("file_id", "role", "storage_key", "byte_size", "sha256"),
                            row,
                            strict=True,
                        )
                    )
                    for row in copy.execute(
                        "SELECT file_id,role,storage_key,byte_size,sha256 FROM asset_files ORDER BY file_id"
                    )
                ]
            )
        with file_io_path(snapshot).open("r+b") as snapshot_stream:
            os.fsync(snapshot_stream.fileno())
        roots = [asdict(entry) for entry in list_storage_roots(root)]
        registration = root / "manifests" / "storage-roots.json"
        storage_manifest = (
            json.loads(file_io_path(registration).read_text(encoding="utf-8"))
            if file_io_path(registration).exists()
            else None
        )
        metadata = {
            "backup_format": 1,
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": __version__,
            "schema_version": version,
            "database_file": snapshot.name,
            "database_size": file_io_path(snapshot).stat().st_size,
            "database_sha256": _digest(snapshot),
            "media_included": False,
            "storage_reference_check": "availability_and_size"
            if validate_storage
            else "not_checked",
            "storage_references": inventory,
            "storage_roots": roots,
            "storage_manifest": storage_manifest,
        }
        with file_io_path(stage / "backup-manifest.json").open(
            "x", encoding="utf-8"
        ) as manifest_stream:
            json.dump(
                metadata, manifest_stream, ensure_ascii=False, sort_keys=True, indent=2
            )
            manifest_stream.flush()
            os.fsync(manifest_stream.fileno())
        if recheck_runtime is not None:
            recheck_runtime()
        file_io_path(stage).rename(file_io_path(completed))
        return completed
    except BaseException:
        if (
            file_io_path(stage).is_symlink()
            or file_io_path(stage).is_junction()
            or resolve_file_path(stage, strict=True).parent != folder
        ):
            raise OSError("Backup staging path changed; cleanup refused")
        shutil.rmtree(file_io_path(stage))
        raise
