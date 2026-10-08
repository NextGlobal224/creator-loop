"""User-data coordination metadata; preserve registry identities and domain DB."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.installation_stage import _installation_root, load_release_manifest
from creator_loop.storage_roots import _load, _manifest_path, _write_manifest
from creator_loop.update_activation import (
    _read_record,
    active_candidate,
    verify_candidate,
    verify_prepared_backup,
)
from creator_loop.update_backup import _inventory
from creator_loop.update_health import run_health_check
from creator_loop.update_preparation import _journal
from creator_loop.windows_paths import file_io_path, resolve_file_path


def record_successful_update(
    root: Path,
    db: sqlite3.Connection,
    record: dict[str, Any],
    candidate: Path,
    manifest: dict[str, Any],
) -> Path:
    """Caller holds app lock and DB writer reservation after completed health."""
    if not db.in_transaction:
        raise RuntimeError("Update metadata requires the source writer reservation")
    if (
        record.get("phase") != "COMPLETED"
        or record.get("activation_pending") is not False
        or record.get("migration_committed") is not True
        or record.get("schema_to") != SCHEMA_VERSION
    ):
        raise ValueError("Only a health-completed update may publish success metadata")
    for field in ("update_id", "backup_id"):
        if (
            not isinstance(record.get(field), str)
            or re.fullmatch(r"[0-9a-f]{32}", record[field]) is None
        ):
            raise ValueError("Invalid completed update identity")
    if record.get("candidate_commit") != manifest["git_commit"]:
        raise ValueError("Completed update manifest identity mismatch")
    activated_at = record.get("activated_at")
    if (
        not isinstance(activated_at, str)
        or datetime.fromisoformat(activated_at).tzinfo is None
    ):
        raise ValueError("Completed update timestamp missing")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RuntimeError("Source schema changed before metadata publication")
    path = _manifest_path(root)
    if path.is_junction():
        raise ValueError("Linked user manifest")
    payload = (
        _load(path)
        if path.exists()
        else {
            "manifest_version": 1,
            "data_root_id": uuid4().hex,
            "schema_version": version,
            "storage_roots": [],
        }
    )
    if not isinstance(payload.get("component_installations", []), list):
        raise ValueError("Invalid component ownership registry")
    payload.setdefault("component_installations", [])  # no owned components claimed
    payload["schema_version"] = version
    payload["last_backup_id"] = record["backup_id"]
    payload["last_successful_update"] = {
        "update_id": record["update_id"],
        "app_version": manifest["app_version"],
        "git_commit": manifest["git_commit"],
        "candidate_name": candidate.name,
        "completed_at": activated_at,
    }
    _write_manifest(path, payload)
    return path


def repair_update_metadata(
    root: Path, journal_path: Path, installation_root: Path
) -> Path:
    """Recheck a completed active update with fresh health, then repair metadata."""
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    for folder in (canonical / "manifests", canonical / "logs", canonical / "runtime"):
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Real coordination directories required")
    if (
        file_io_path(journal_path).is_symlink()
        or file_io_path(journal_path).is_junction()
        or resolve_file_path(journal_path, strict=True).parent
        != canonical / "manifests"
    ):
        raise ValueError("Update journal must belong to this data root")
    source = canonical / "creator_loop.sqlite3"
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("A real source database is required")
    with (
        AppDataLock(canonical),
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            validate(db)
            if any(
                path.name != "app-data.lock"
                for path in (canonical / "runtime").iterdir()
            ):
                raise RuntimeError(
                    "Worker records require recovery before metadata repair"
                )
            record = _read_record(journal_path)
            update_id = record.get("update_id")
            if (
                record.get("phase") not in ("COMPLETED", "COMPLETED_METADATA_PENDING")
                or not isinstance(update_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", update_id) is None
                or journal_path.name != f"update-{update_id}.json"
            ):
                raise ValueError("A completed active update journal is required")
            candidate, pointer = active_candidate(installation, SCHEMA_VERSION)
            if pointer.get("update_id") != update_id:
                raise ValueError("Journal does not identify the current active update")
            manifest = load_release_manifest(candidate / "release-manifest.json")
            verify_prepared_backup(canonical, record)
            before = _inventory(db, canonical)
            health_log = (
                canonical / "logs" / f"metadata-health-{update_id}-{uuid4().hex}"
            )
            record["metadata_health_log_directory"] = str(health_log)
            _journal(journal_path, record)
            try:
                run_health_check(
                    candidate / "CreatorLoop/CreatorLoop.exe",
                    canonical,
                    health_log,
                    component_version=manifest["app_version"],
                )
            except BaseException as exc:
                record["metadata_repair_error_type"] = type(exc).__name__
                _journal(journal_path, record)
                raise
            verify_candidate(candidate, installation)
            if _inventory(db, canonical) != before:
                raise RuntimeError("Metadata health changed storage inventory")
            record["phase"] = "COMPLETED"
            try:
                record_successful_update(canonical, db, record, candidate, manifest)
                record["metadata_repaired_at"] = datetime.now(timezone.utc).isoformat()
                record.pop("error_type", None)
                record.pop("metadata_repair_error_type", None)
                _journal(journal_path, record)
            except BaseException as exc:
                record["phase"] = "COMPLETED_METADATA_PENDING"
                record["error_type"] = type(exc).__name__
                _journal(journal_path, record)
                raise
            return candidate
        finally:
            db.rollback()
