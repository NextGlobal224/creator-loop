"""Confirmed DB restore via SQLite transactions, with crash recovery and no media writes."""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import time
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import (
    SCHEMA_VERSION,
    _migrate_locked,
    open_readonly,
    validate,
)
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import (
    _assess_locked,
    _check_deadline,
    _database_identity,
    _media_assessment,
)
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.update_activation import (
    _read_record,
    activate_prepared_update,
    active_candidate,
    verify_candidate,
    verify_prepared_backup,
)
from creator_loop.update_backup import _backup_from_guard, _digest
from creator_loop.update_preparation import _journal, manifest_identity


def _copy_database(
    reader: sqlite3.Connection, target: sqlite3.Connection, deadline: float
) -> None:
    def progress(status: int, remaining: int, total: int) -> None:
        _check_deadline(deadline)

    reader.backup(target, pages=256, progress=progress, sleep=0.01)


def _data_paths(root: Path, installation_root: Path) -> tuple[Path, Path, Path]:
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    for name in ("runtime", "manifests", "backups", "logs"):
        path = canonical / name
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            raise ValueError("Real restore coordination directories required")
    source = canonical / "creator_loop.sqlite3"
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("This restore operation requires a real existing DB")
    return canonical, installation, source


def _refs(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT file_id,asset_id,role,storage_key,sha256,byte_size,parent_file_id FROM asset_files ORDER BY file_id"
    ).fetchall()


def _verify_restored_state(
    canonical: Path, db: sqlite3.Connection, record: dict[str, Any], handles: ExitStack
) -> None:
    """Recheck actual restored DB/media under reserved writer and app locks."""
    deadline = time.monotonic() + 60
    actual, _ = _database_identity(db, deadline)
    if actual != record.get("restored_database_identity"):
        raise RuntimeError("Restored DB changed before activation completed")
    if any(
        entry["status"] != "valid"
        for entry in _media_assessment(canonical, db, deadline, handles)
    ):
        raise RuntimeError("Storage reference bytes require recovery before activation")


def apply_restore(
    root: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    reviewed_identity: str,
    confirm_lost_changes: bool = False,
    confirm_media_issues: bool = False,
    timeout_seconds: float = 120,
) -> Path:
    """Require explicit consent to an unchanged review; retain DB snapshots/media.

    Never pass a default consent. The caller must display assessment and obtain
    confirmation. A durable runtime marker blocks normal app/managed launch
    across SQLite copy/health/crashes until explicit recovery resolves it.
    """
    if (
        confirm_lost_changes is not True
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_identity) is None
    ):
        raise ValueError(
            "Explicit lost-change confirmation and review identity required"
        )
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Restore work budget must be positive and at most 600s")
    if re.fullmatch(r"[0-9a-f]{32}", backup_id) is None:
        raise ValueError("Explicit backup ID required")
    canonical, installation, source = _data_paths(root, installation_root)
    deadline = time.monotonic() + timeout_seconds
    with (
        AppDataLock(canonical),
        ExitStack() as handles,
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        require_no_pending_restore(canonical)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        journal: Path | None = None
        marker_created = False
        copy_started = False
        try:
            assessment = _assess_locked(
                canonical,
                backup_id,
                installation,
                candidate_directory,
                db,
                deadline,
                handles,
            )
            if assessment["assessment_identity"] != reviewed_identity:
                raise ValueError(
                    "Restore review is stale; inspect and confirm the current state again"
                )
            if assessment["media_issue_count"] and confirm_media_issues is not True:
                raise ValueError(
                    "Explicit acknowledgement of assessed media issues required"
                )
            backup_version = assessment["backup_schema"]
            snapshot = verify_prepared_backup(
                canonical, {"backup_id": backup_id, "schema_from": backup_version}
            )
            handles.enter_context(_open_read_lock(snapshot))
            if _digest(snapshot) != assessment["backup_sha256"]:
                raise ValueError("Backup changed before its read lock was acquired")
            candidate = candidate_directory.resolve(strict=True)
            manifest = verify_candidate(candidate, installation)
            # Reserve writers while a separate reader captures current WAL data.
            # Missing current media must not prevent preserving current DB bytes.
            current_backup = _backup_from_guard(
                canonical, db, min(30, timeout_seconds), validate_storage=False
            )
            restore_id = uuid4().hex
            stage = canonical / "backups" / f"restore-{restore_id}"
            stage.mkdir()
            staged = stage / "restored.sqlite3"
            if shutil.disk_usage(stage).free < snapshot.stat().st_size * 3 + 1024**2:
                raise OSError("Insufficient space for a staged validated restore")
            with (
                closing(open_readonly(snapshot.resolve(strict=True))) as reader,
                closing(sqlite3.connect(staged)) as target,
            ):
                target.execute("PRAGMA foreign_keys=ON")
                _copy_database(reader, target, deadline)
                validate(target, expected_version=backup_version)
                if target.execute(
                    "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                ).fetchone():
                    raise RuntimeError(
                        "Backup contains processing that needs explicit recovery"
                    )
                refs = _refs(target)
                target.execute("BEGIN IMMEDIATE")
                try:
                    _migrate_locked(target, backup_version)
                    if _refs(target) != refs:
                        raise RuntimeError("Restore migration changed media references")
                    target.commit()
                except BaseException:
                    target.rollback()
                    raise
                restored_identity, _ = _database_identity(target, deadline)
            with staged.open("r+b") as stream:
                os.fsync(stream.fileno())
            handles.enter_context(_open_read_lock(staged))
            journal = canonical / "manifests" / f"update-{restore_id}.json"
            record: dict[str, Any] = {
                "restore_format": 1,
                "update_format": 1,
                "update_id": restore_id,
                "restore_id": restore_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "phase": "RESTORE_STAGED",
                "backup_id": backup_id,
                "schema_from": backup_version,
                "schema_to": SCHEMA_VERSION,
                "candidate_commit": manifest["git_commit"],
                "candidate_manifest_identity": manifest_identity(manifest),
                "candidate_directory": str(candidate),
                "activation_pending": True,
                "migration_committed": False,
                "pre_restore_backup_id": current_backup.name,
                "pre_restore_schema": assessment["current_schema"],
                "pre_restore_database_identity": assessment[
                    "current_database_identity"
                ],
                "restored_database_identity": restored_identity,
                "staged_database_directory": stage.name,
                "staged_database_sha256": _digest(staged),
                "reviewed_identity": reviewed_identity,
                "confirmed_lost_changes": True,
                "confirmed_media_issues": confirm_media_issues is True,
                "media_issue_count": assessment["media_issue_count"],
                "registry_sha256_at_review": assessment["registry_sha256"],
            }
            _journal(journal, record)
            marker = canonical / "runtime/restore-in-progress.json"
            _journal(
                marker,
                {
                    "restore_format": 1,
                    "restore_id": restore_id,
                    "journal_name": journal.name,
                },
            )
            marker_created = True
            # Rollback of the reservation is not a rollback of user data.
            # Acquire an exclusive SQLite lock and retain it across Backup API's
            # own transaction; it requires no active destination transaction.
            db.rollback()
            if db.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] != "exclusive":
                raise RuntimeError("Exclusive SQLite mode required for restore")
            db.execute("BEGIN EXCLUSIVE")
            actual_identity, _ = _database_identity(db, deadline)
            if actual_identity != assessment["current_database_identity"]:
                raise ValueError("DB changed while acquiring exclusive restore lock")
            db.commit()
            if (
                manifest_identity(verify_candidate(candidate, installation))
                != record["candidate_manifest_identity"]
            ):
                raise ValueError("Restore candidate changed after confirmation")
            registry_path = canonical / "manifests/storage-roots.json"
            registry_digest = _digest(registry_path) if registry_path.exists() else None
            if registry_digest != assessment["registry_sha256"]:
                raise ValueError("Storage registry changed after restore confirmation")
            record["phase"] = "RESTORE_COPY_STARTED"
            _journal(journal, record)
            copy_started = True
            with closing(open_readonly(staged.resolve(strict=True))) as reader:
                _copy_database(reader, db, deadline)
            validate(db)
            actual_identity, _ = _database_identity(db, deadline)
            if actual_identity != restored_identity:
                raise RuntimeError("Restored DB does not match validated staged state")
            record["phase"] = "RESTORE_DB_COMMITTED"
            record["migration_committed"] = True
            _journal(journal, record)
        except BaseException as exc:
            db.rollback()
            if journal is not None:
                record["phase"] = (
                    "RESTORE_OUTCOME_UNCONFIRMED"
                    if copy_started
                    else "RESTORE_NOT_APPLIED"
                )
                record["error_type"] = type(exc).__name__
                _journal(journal, record)
                if marker_created and not copy_started:
                    marker.unlink()
            raise
        finally:
            db.rollback()
    return recover_restore(canonical, journal, installation)


def recover_restore(root: Path, journal_path: Path, installation_root: Path) -> Path:
    """Resolve a confirmed restore by actual DB identity, then fresh health.

    Unknown/different state remains blocked. Never re-copy the backup, silently
    undo post-backup changes or infer the copy's outcome from journal phase.
    """
    canonical, installation, source = _data_paths(root, installation_root)
    if (
        journal_path.is_symlink()
        or journal_path.is_junction()
        or journal_path.resolve(strict=True).parent != canonical / "manifests"
    ):
        raise ValueError("Restore journal must belong to this data root")
    with (
        AppDataLock(canonical),
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            record = _read_record(journal_path)
            restore_id = record.get("restore_id")
            if (
                record.get("restore_format") != 1
                or record.get("update_format") != 1
                or type(record.get("schema_to")) is not int
                or record.get("schema_to") != SCHEMA_VERSION
                or not isinstance(record.get("candidate_directory"), str)
                or not isinstance(restore_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", restore_id) is None
                or record.get("update_id") != restore_id
                or journal_path.name != f"update-{restore_id}.json"
                or record.get("confirmed_lost_changes") is not True
                or record.get("phase")
                not in (
                    "RESTORE_STAGED",
                    "RESTORE_COPY_STARTED",
                    "RESTORE_DB_COMMITTED",
                    "RESTORE_OUTCOME_UNCONFIRMED",
                    "PREPARED",
                    "ACTIVATION_STARTED",
                    "ACTIVATED_PENDING_HEALTH",
                    "HEALTH_FAILED",
                    "COMPLETED",
                    "COMPLETED_METADATA_PENDING",
                )
            ):
                raise ValueError("A confirmed restore journal is required")
            marker = canonical / "runtime/restore-in-progress.json"
            marker_record = _read_record(marker)
            if (
                marker_record.get("restore_id") != restore_id
                or marker_record.get("journal_name") != journal_path.name
            ):
                raise ValueError("Restore marker/journal identity mismatch")
            if any(
                path.name not in ("app-data.lock", marker.name)
                for path in (canonical / "runtime").iterdir()
            ):
                raise RuntimeError("Other worker records require recovery")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= SCHEMA_VERSION:
                raise RuntimeError("Unsupported actual schema in restore recovery")
            validate(db, expected_version=version)
            pre_backup = verify_prepared_backup(
                canonical,
                {
                    "backup_id": record.get("pre_restore_backup_id"),
                    "schema_from": record.get("pre_restore_schema"),
                },
            )
            with closing(open_readonly(pre_backup.resolve(strict=True))) as prior:
                prior_identity, _ = _database_identity(prior, time.monotonic() + 60)
            if prior_identity != record.get("pre_restore_database_identity"):
                raise ValueError("Pre-restore backup does not match recorded state")
            stage_directory = canonical / "backups" / f"restore-{restore_id}"
            staged = stage_directory / "restored.sqlite3"
            if (
                stage_directory.is_symlink()
                or stage_directory.is_junction()
                or staged.is_symlink()
                or staged.is_junction()
                or not staged.is_file()
                or _digest(staged) != record.get("staged_database_sha256")
            ):
                raise ValueError("Validated restored-state snapshot changed")
            with closing(open_readonly(staged.resolve(strict=True))) as prepared:
                validate(prepared)
                staged_identity, _ = _database_identity(prepared, time.monotonic() + 60)
            if staged_identity != record.get("restored_database_identity"):
                raise ValueError("Restored-state identity does not match staged DB")
            actual, _ = _database_identity(db, time.monotonic() + 60)
            if actual != record.get("restored_database_identity"):
                if actual == record.get("pre_restore_database_identity"):
                    record["phase"] = "RESTORE_NOT_APPLIED"
                    record["activation_pending"] = False
                    _journal(journal_path, record)
                    marker.unlink()
                    return journal_path
                raise RuntimeError(
                    "Unknown actual restore state; preserve DB/backups and marker"
                )
            validate(db)
            candidate = Path(record["candidate_directory"])
            manifest = verify_candidate(candidate, installation)
            if manifest_identity(manifest) != record.get("candidate_manifest_identity"):
                raise ValueError("Restore candidate changed")
            verify_prepared_backup(canonical, record)
            record["phase"] = "PREPARED"
            record["migration_committed"] = True
            record["activation_pending"] = True
            record["restore_state_verified_at"] = datetime.now(timezone.utc).isoformat()
            _journal(journal_path, record)
        finally:
            db.rollback()
    activated = activate_prepared_update(
        canonical, journal_path, installation, restore_id=restore_id
    )
    with (
        AppDataLock(canonical),
        ExitStack() as handles,
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        active, pointer = active_candidate(installation, SCHEMA_VERSION)
        if active != activated or pointer.get("update_id") != restore_id:
            raise RuntimeError("Restore activation changed before clearing guard")
        if _read_record(marker).get("restore_id") != restore_id:
            raise ValueError("Restore marker changed before clearing guard")
        completed = _read_record(journal_path)
        if completed.get("phase") != "COMPLETED":
            raise RuntimeError("Restore metadata/health incomplete; guard retained")
        _verify_restored_state(canonical, db, completed, handles)
        completed["restore_completed_at"] = datetime.now(timezone.utc).isoformat()
        _journal(journal_path, completed)
        marker.unlink()
    return activated
