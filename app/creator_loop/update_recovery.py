"""Inspect interrupted updates from current SQLite/candidate/backup state."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION, _migrate_locked, validate
from creator_loop.installation_stage import _installation_root
from creator_loop.update_activation import (
    _read_record,
    activate_prepared_update,
    active_candidate,
    verify_candidate,
    verify_prepared_backup,
)
from creator_loop.update_backup import _backup_from_guard, _inventory
from creator_loop.update_preparation import (
    _journal,
    manifest_identity,
    validate_candidate_migrations,
)

RESUMABLE_PHASES = (
    "INSTALLATION_STAGED",
    "MIGRATION_VALIDATED",
    "PREPARED",
    "FAILED_ROLLED_BACK",
    "FAILED_POST_COMMIT",
    "ACTIVATION_STARTED",
    "ACTIVATED_PENDING_HEALTH",
    "HEALTH_FAILED",
    "RECOVERY_STARTED",
    "RECOVERY_MIGRATION_VALIDATED",
    "RECOVERY_FAILED_ROLLED_BACK",
    "RECOVERY_FAILED_POST_COMMIT",
)


def _candidate(
    record: dict[str, Any], installation: Path
) -> tuple[Path, dict[str, Any]]:
    value = record.get("candidate_directory")
    if not isinstance(value, str):
        raise ValueError(
            "No recorded candidate; prepare again from an explicit package"
        )
    candidate = Path(value)
    manifest = verify_candidate(candidate, installation)
    if (
        manifest["git_commit"] != record.get("candidate_commit")
        or manifest["schema_to"] != record.get("schema_to")
        or record.get("candidate_manifest_identity") != manifest_identity(manifest)
    ):
        raise ValueError(
            "Candidate binding missing or changed; prepare from explicit package"
        )
    validate_candidate_migrations(manifest, candidate)
    return candidate.resolve(strict=True), manifest


def inspect_update(
    root: Path, journal_path: Path, installation_root: Path
) -> dict[str, Any]:
    """Read-only evidence; never infer commit outcome or restore/kill from a phase.

    App/DB locks freeze this inspection. Return only coordination IDs, schemas,
    status and error types, without asset text/media or database contents.
    """
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    for folder in (canonical / "manifests", canonical / "runtime"):
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Real recovery coordination directories required")
    if (
        journal_path.is_symlink()
        or journal_path.is_junction()
        or journal_path.resolve(strict=True).parent != canonical / "manifests"
    ):
        raise ValueError("Recovery journal must belong to this data root")
    source = canonical / "creator_loop.sqlite3"
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("A real source database is required for update inspection")
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
            update_id = record.get("update_id")
            target = record.get("schema_to")
            if (
                record.get("update_format") != 1
                or not isinstance(update_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", update_id) is None
                or journal_path.name != f"update-{update_id}.json"
                or type(target) is not int
                or target != SCHEMA_VERSION
            ):
                raise ValueError(
                    "Recovery requires a journal for this updater's schema"
                )
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= SCHEMA_VERSION:
                raise RuntimeError(
                    "Use an updater compatible with the actual source schema"
                )
            # Actual user_version plus checksum history/integrity/FK, rather
            # than migration_committed or journal phase, establishes this.
            validate(db, expected_version=version)
            evidence: dict[str, Any] = {
                "recovery_format": 1,
                "update_id": update_id,
                "journal_phase": record.get("phase"),
                "actual_schema": version,
                "target_schema": target,
                "database_valid": True,
                "schema_state": "at_target" if version == target else "before_target",
                "commit_outcome": "not_inferred_from_journal",
                "runtime_records_pending": sum(
                    path.name != "app-data.lock"
                    for path in (canonical / "runtime").iterdir()
                ),
                "active_processing_runs": db.execute(
                    "SELECT count(*) FROM processing_runs WHERE status IN ('QUEUED','RUNNING')"
                ).fetchone()[0],
                "candidate_valid": False,
                "backup_valid": False,
                "storage_valid": False,
                "active_pointer_valid": False,
            }
            try:
                _inventory(db, canonical)
                evidence["storage_valid"] = True
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                evidence["storage_error_type"] = type(exc).__name__
            candidate_value = record.get("candidate_directory")
            if isinstance(candidate_value, str):
                try:
                    _, manifest = _candidate(record, installation)
                    evidence["candidate_valid"] = True
                    evidence["candidate_reads_actual_schema"] = (
                        manifest["schema_read_min"]
                        <= version
                        <= manifest["schema_read_max"]
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    evidence["candidate_error_type"] = type(exc).__name__
            if record.get("backup_id") is not None:
                try:
                    verify_prepared_backup(canonical, record)
                    evidence["backup_valid"] = True
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    evidence["backup_error_type"] = type(exc).__name__
            pointer_path = installation / "active-installation.json"
            if pointer_path.exists() or pointer_path.is_symlink():
                try:
                    pointer = _read_record(pointer_path)
                    evidence["pointer_status"] = pointer.get("status")
                    active, _ = active_candidate(installation, version)
                    evidence["active_pointer_valid"] = True
                    evidence["active_candidate_is_update"] = (
                        active == Path(candidate_value).resolve(strict=True)
                        if isinstance(candidate_value, str)
                        else False
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    evidence["pointer_error_type"] = type(exc).__name__
            ready = (
                evidence["candidate_valid"]
                and evidence["backup_valid"]
                and evidence["storage_valid"]
                and not evidence["runtime_records_pending"]
                and not evidence["active_processing_runs"]
                and record.get("phase") in RESUMABLE_PHASES
            )
            evidence["recovery_ready"] = bool(ready)
            evidence["next_action"] = (
                "recheck_activation_at_target"
                if ready and version == target
                else "backup_current_state_then_resume_migration"
                if ready
                else "resolve_reported_dependencies_before_recovery"
            )
            return evidence
        finally:
            db.rollback()


def resume_update(root: Path, journal_path: Path, installation_root: Path) -> Path:
    """Revalidate actual state, back up current data, migrate and freshly activate.

    The last journal phase cannot establish whether an old COMMIT happened.
    This invocation performs its own known commit after actual schema/history
    checks; backups and attempts retain the former coordination state. It never
    restores DB/media, selects an unrecorded candidate, or kills from a PID.
    """
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    for name in ("manifests", "runtime", "logs"):
        folder = canonical / name
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Real recovery coordination directories required")
    if (
        journal_path.is_symlink()
        or journal_path.is_junction()
        or journal_path.resolve(strict=True).parent != canonical / "manifests"
    ):
        raise ValueError("Recovery journal must belong to this data root")
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
        committed = False
        started = False
        record = _read_record(journal_path)
        try:
            update_id = record.get("update_id")
            if (
                record.get("update_format") != 1
                or not isinstance(update_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", update_id) is None
                or journal_path.name != f"update-{update_id}.json"
                or type(record.get("schema_to")) is not int
                or record["schema_to"] != SCHEMA_VERSION
                or record.get("phase") not in RESUMABLE_PHASES
            ):
                raise ValueError(
                    "An interrupted update journal for this updater is required"
                )
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= SCHEMA_VERSION:
                raise RuntimeError("Unsupported actual source schema")
            validate(db, expected_version=version)
            _, manifest = _candidate(record, installation)
            if not manifest["schema_from"] <= version <= manifest["schema_to"]:
                raise ValueError("Actual schema outside candidate migration range")
            verify_prepared_backup(canonical, record)
            if (
                any(
                    path.name != "app-data.lock"
                    for path in (canonical / "runtime").iterdir()
                )
                or db.execute(
                    "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                ).fetchone()
            ):
                raise RuntimeError(
                    "Owned workers require recovery before update resume"
                )
            before = _inventory(db, canonical)
            attempts = record.get("recovery_attempts", [])
            if not isinstance(attempts, list) or len(attempts) >= 100:
                raise ValueError("Invalid or exhausted recovery attempt budget")
            attempts.append(
                {
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "previous_phase": record.get("phase"),
                    "previous_backup_id": record.get("backup_id"),
                    "previous_schema_from": record.get("schema_from"),
                    "previous_migration_committed": record.get("migration_committed"),
                    "observed_schema": version,
                }
            )
            record["recovery_attempts"] = attempts
            record["phase"] = "RECOVERY_STARTED"
            record["recovery_observed_schema"] = version
            record["recovery_method"] = (
                "validated_current_target"
                if version == SCHEMA_VERSION
                else "sequential_migration"
            )
            record["migration_committed"] = False
            record["recovery_commit_completed"] = False
            record["activation_pending"] = True
            record.pop("activated_at", None)
            _journal(journal_path, record)
            started = True
            snapshot = _backup_from_guard(canonical, db, 30)
            record["backup_id"] = snapshot.name
            record["schema_from"] = version
            _journal(journal_path, record)
            _migrate_locked(db, version)
            _candidate(record, installation)
            if _inventory(db, canonical) != before:
                raise RuntimeError(
                    "Recovery migration changed storage reference inventory"
                )
            record["phase"] = "RECOVERY_MIGRATION_VALIDATED"
            _journal(journal_path, record)
            db.commit()
            committed = True
            record["migration_committed"] = True
            record["recovery_commit_completed"] = True
            record["phase"] = "PREPARED"
            record["prepared_at"] = datetime.now(timezone.utc).isoformat()
            record.pop("error_type", None)
            _journal(journal_path, record)
        except BaseException as exc:
            db.rollback()
            if started:
                record["phase"] = (
                    "RECOVERY_FAILED_POST_COMMIT"
                    if committed
                    else "RECOVERY_FAILED_ROLLED_BACK"
                )
                record["migration_committed"] = committed
                record["recovery_commit_completed"] = committed
                record["error_type"] = type(exc).__name__
                _journal(journal_path, record)
            raise
        finally:
            db.rollback()
    # Activation takes fresh locks and validates DB/media/candidate/backup again.
    # A writer/app winning this gap causes contention or a fresh refusal, rather
    # than running the candidate against a stale pre-migration connection.
    return activate_prepared_update(canonical, journal_path, installation)
