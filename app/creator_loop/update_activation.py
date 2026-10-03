"""Explicit activation of a prepared installation; never restore user DB bytes."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
from creator_loop.installation_stage import (
    MAX_FILES,
    _installation_root,
    load_release_manifest,
)
from creator_loop.update_backup import _digest, _inventory
from creator_loop.update_health import run_health_check
from creator_loop.update_preparation import _journal, manifest_identity


def _read_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("A real coordination record is required")
    with path.open("rb") as stream:
        raw = stream.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError("Coordination record exceeds metadata budget")
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Invalid coordination record")
    return record


def verify_candidate(candidate: Path, installation: Path) -> dict[str, Any]:
    """Recheck a complete staged inventory, rejecting changed or linked files."""
    if (
        candidate.is_symlink()
        or candidate.is_junction()
        or candidate.resolve(strict=True).parent != installation
        or re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}-[0-9a-f]{32}", candidate.name
        )
        is None
    ):
        raise ValueError("Candidate must be a real staged version directory")
    manifest_path = candidate / "release-manifest.json"
    if manifest_path.is_symlink() or manifest_path.is_junction():
        raise ValueError("Linked candidate manifest")
    manifest = load_release_manifest(manifest_path)
    actual: set[str] = set()
    for index, path in enumerate(candidate.rglob("*")):
        if index >= MAX_FILES * 2:
            raise ValueError("Candidate inventory exceeds entry budget")
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked candidate member")
        if path.is_file():
            name = path.relative_to(candidate).as_posix()
            if name == "release-manifest.json":
                continue
            entry = manifest["files"].get(name)
            if (
                entry is None
                or path.stat().st_size != entry["size"]
                or _digest(path) != entry["sha256"]
            ):
                raise ValueError("Candidate file inventory or digest changed")
            actual.add(name)
    if actual != set(manifest["files"]):
        raise ValueError("Candidate lacks required inventory files")
    if not candidate.name.startswith(
        f"{manifest['app_version']}-{manifest['git_commit'][:12]}-"
    ):
        raise ValueError("Candidate identity does not match release manifest")
    return manifest


def active_candidate(
    installation: Path, schema_version: int
) -> tuple[Path, dict[str, Any]]:
    if installation.is_symlink() or installation.is_junction():
        raise ValueError("Active installation root must be a real directory")
    installation = installation.resolve(strict=True)
    pointer = _read_record(installation / "active-installation.json")
    return _candidate_from_pointer(installation, pointer, schema_version), pointer


def _candidate_from_pointer(
    installation: Path, pointer: dict[str, Any], schema_version: int
) -> Path:
    if pointer.get("pointer_format") != 1 or pointer.get("status") != "ACTIVE":
        raise RuntimeError("Installation has no health-validated active candidate")
    name = pointer.get("candidate_name")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("Invalid active candidate name")
    candidate = installation / name
    manifest = verify_candidate(candidate, installation)
    if pointer.get("manifest_sha256") != _digest(candidate / "release-manifest.json"):
        raise ValueError("Active candidate manifest changed")
    if not manifest["schema_read_min"] <= schema_version <= manifest["schema_read_max"]:
        raise RuntimeError("Active installation cannot read the current schema")
    return candidate


def verify_prepared_backup(canonical: Path, record: dict[str, Any]) -> Path:
    backup_id = record.get("backup_id")
    if (
        not isinstance(backup_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", backup_id) is None
    ):
        raise ValueError("Prepared backup identity missing")
    backup = canonical / "backups" / backup_id
    if (
        backup.is_symlink()
        or backup.is_junction()
        or backup.resolve(strict=True).parent != canonical / "backups"
    ):
        raise ValueError("Unsafe prepared backup directory")
    metadata = _read_record(backup / "backup-manifest.json")
    backup_version = metadata.get("schema_version")
    if (
        type(backup_version) is not int
        or not 1 <= backup_version <= SCHEMA_VERSION
        or type(record.get("schema_from")) is not int
        or record.get("schema_from") != backup_version
    ):
        raise ValueError("Prepared backup schema identity changed")
    snapshot = backup / "creator_loop.sqlite3"
    if (
        snapshot.is_symlink()
        or snapshot.is_junction()
        or _digest(snapshot) != metadata.get("database_sha256")
        or metadata.get("backup_id") != backup_id
        or snapshot.stat().st_size != metadata.get("database_size")
    ):
        raise ValueError("Prepared backup digest or identity changed")
    with closing(open_readonly(snapshot.resolve(strict=True))) as copy:
        validate(copy, expected_version=backup_version)
    return snapshot


def activate_prepared_update(
    root: Path,
    journal_path: Path,
    installation_root: Path,
    *,
    restore_id: str | None = None,
) -> Path:
    """Switch the managed pointer then health-check while app/DB locks remain held.

    A health failure can return to a verified previous executable only if its
    manifest supports the actual current schema. Otherwise the pointer remains
    explicitly blocked. Backup/DB/media/old installations are never restored,
    deleted or rewritten by this operation.
    """
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    manifests = canonical / "manifests"
    logs = canonical / "logs"
    for folder in (manifests, logs, canonical / "runtime"):
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Real user-data coordination directories required")
    if (
        journal_path.is_symlink()
        or journal_path.is_junction()
        or journal_path.resolve(strict=True).parent != manifests
    ):
        raise ValueError("Update journal must belong to this data root")
    source = canonical / "creator_loop.sqlite3"
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("A real source database is required")
    with (
        AppDataLock(canonical),
        ExitStack() as media_handles,
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            validate(db)
            allowed_runtime = {"app-data.lock"}
            if restore_id is not None:
                marker = _read_record(canonical / "runtime/restore-in-progress.json")
                if (
                    marker.get("restore_id") != restore_id
                    or journal_path.name != f"update-{restore_id}.json"
                ):
                    raise ValueError("Restore marker does not identify this activation")
                allowed_runtime.add("restore-in-progress.json")
            if (
                any(
                    path.name not in allowed_runtime
                    for path in (canonical / "runtime").iterdir()
                )
                or db.execute(
                    "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                ).fetchone()
            ):
                raise RuntimeError("Owned workers require recovery before activation")
            record = _read_record(journal_path)
            update_id = record.get("update_id")
            if (
                record.get("update_format") != 1
                or not isinstance(update_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", update_id) is None
                or journal_path.name != f"update-{update_id}.json"
                or record.get("phase") not in ("PREPARED", "HEALTH_FAILED")
                or record.get("migration_committed") is not True
                or record.get("schema_to") != SCHEMA_VERSION
            ):
                raise ValueError("A prepared committed update journal is required")
            candidate_value = record.get("candidate_directory")
            if not isinstance(candidate_value, str):
                raise ValueError("Prepared candidate missing")
            candidate = Path(candidate_value)
            manifest = verify_candidate(candidate, installation)
            if (
                manifest["git_commit"] != record.get("candidate_commit")
                or (
                    record.get("candidate_manifest_identity") is not None
                    and manifest_identity(manifest)
                    != record["candidate_manifest_identity"]
                )
                or manifest["schema_to"] != record["schema_to"]
                or not manifest["schema_read_min"]
                <= SCHEMA_VERSION
                <= manifest["schema_read_max"]
            ):
                raise ValueError("Prepared candidate compatibility mismatch")
            verify_prepared_backup(canonical, record)
            if restore_id is not None:
                from creator_loop.restore_apply import _verify_restored_state

                if (
                    record.get("restore_format") != 1
                    or record.get("restore_id") != restore_id
                    or record.get("confirmed_lost_changes") is not True
                ):
                    raise ValueError("Confirmed restore activation required")
                _verify_restored_state(canonical, db, record, media_handles)
            before = _inventory(db, canonical)
            pointer_path = installation / "active-installation.json"
            previous = None
            previous_candidate = None
            if pointer_path.exists() or pointer_path.is_symlink():
                previous = _read_record(pointer_path)
                # Keep incompatible previous versions, but never launch them.
                if previous.get("status") == "ACTIVE":
                    try:
                        previous_candidate, _ = active_candidate(
                            installation, SCHEMA_VERSION
                        )
                    except RuntimeError:
                        previous = None
                else:
                    current_pointer = previous
                    previous = None
                    # An interrupted pointer switch may have persisted the
                    # original ACTIVE pointer only in its update journal.
                    saved = record.get("previous_active_pointer")
                    if (
                        record.get("recovery_commit_completed") is True
                        and isinstance(saved, dict)
                        and current_pointer.get("update_id") == update_id
                    ):
                        try:
                            previous_candidate = _candidate_from_pointer(
                                installation, saved, SCHEMA_VERSION
                            )
                            previous = saved
                        except (OSError, ValueError, RuntimeError):
                            pass
            pointer = {
                "pointer_format": 1,
                "status": "PENDING_HEALTH",
                "candidate_name": candidate.name,
                "manifest_sha256": _digest(candidate / "release-manifest.json"),
                "update_id": update_id,
            }
            record["previous_active_pointer"] = previous
            record["phase"] = "ACTIVATION_STARTED"
            _journal(journal_path, record)
            try:
                _journal(pointer_path, pointer)
                record["phase"] = "ACTIVATED_PENDING_HEALTH"
                _journal(journal_path, record)
                health_log = logs / f"update-health-{update_id}-{uuid4().hex}"
                record["health_log_directory"] = str(health_log)
                _journal(journal_path, record)
                run_health_check(
                    candidate / "CreatorLoop/CreatorLoop.exe",
                    canonical,
                    health_log,
                    component_version=manifest["app_version"],
                )
                verify_candidate(candidate, installation)
                if _inventory(db, canonical) != before:
                    raise RuntimeError("Health changed storage reference inventory")
                pointer["status"] = "ACTIVE"
                _journal(pointer_path, pointer)
                record["phase"] = "COMPLETED"
                record["activation_pending"] = False
                record.pop("error_type", None)
                record["activated_at"] = datetime.now(timezone.utc).isoformat()
                _journal(journal_path, record)
            except BaseException as exc:
                if previous is not None and previous_candidate is not None:
                    try:
                        previous_manifest = verify_candidate(
                            previous_candidate, installation
                        )
                        if (
                            _digest(previous_candidate / "release-manifest.json")
                            != previous["manifest_sha256"]
                            or not previous_manifest["schema_read_min"]
                            <= SCHEMA_VERSION
                            <= previous_manifest["schema_read_max"]
                        ):
                            previous = None
                    except (OSError, ValueError, RuntimeError):
                        previous = None
                if previous is not None:
                    _journal(pointer_path, previous)
                else:
                    pointer["status"] = "HEALTH_FAILED"
                    _journal(pointer_path, pointer)
                record["phase"] = "HEALTH_FAILED"
                record["activation_pending"] = True
                record.pop("activated_at", None)
                record["error_type"] = type(exc).__name__
                _journal(journal_path, record)
                raise
            # Core activation/health is already durable. A metadata failure
            # must not pretend health failed or undo a compatible active app.
            from creator_loop.update_metadata import record_successful_update

            try:
                record_successful_update(canonical, db, record, candidate, manifest)
            except BaseException as exc:
                record["phase"] = "COMPLETED_METADATA_PENDING"
                record["error_type"] = type(exc).__name__
                _journal(journal_path, record)
                raise
            return candidate
        finally:
            db.rollback()
