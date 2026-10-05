"""Explicit recovery of an actually validated corrupt copy, without recopying."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_database import _regular
from creator_loop.corrupt_restore_copy import _hash
from creator_loop.corrupt_restore_inspection import _hold_inspected_copy
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.installation_stage import _installation_root
from creator_loop.preserved_source_validation import _unique_object
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import (
    _database_identity,
    _media_assessment,
    _restore_inputs,
)
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.sqlite_recovery_file import hold_recovery_database
from creator_loop.update_activation import _candidate_from_pointer, verify_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_health import run_health_check
from creator_loop.update_metadata import record_successful_update
from creator_loop.update_preparation import _journal, manifest_identity


def _record(path: Path) -> dict[str, Any]:
    if _regular(path) is None:
        raise ValueError("Existing recovery metadata required")
    with _open_read_lock(path) as stream:
        raw = stream.read(1024**2 + 1)
    if not 0 < len(raw) <= 1024**2:
        raise ValueError("Bounded recovery metadata required")
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise ValueError("Recovery metadata must be an object")
    return result


def recover_corrupt_copy(
    root: Path,
    copy_journal: Path,
    installation_root: Path,
    *,
    reviewed_inspection: str,
    confirm_recovery: bool = False,
    timeout_seconds: float = 180,
) -> Path:
    """Fresh actual-state consent, compatible activation/health, then guard clear.

    Only VALIDATED_COPY_GUARDED is supported. Unknown/partial/missing/empty
    states stay guarded; no DB copy/migration/repair/source move occurs here.
    Original files, raw archive, consistent backup and old installations remain.
    The original copy journal is immutable history; a separate format2 recovery
    journal records health/activation, with no invented pre-corruption backup.
    Native I/O/child health still require an outer process timeout.
    """
    if sys.platform != "win32":
        raise OSError("Corrupt-copy recovery requires native Windows ownership")
    if (
        confirm_recovery is not True
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_inspection) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError(
            "Explicit current inspection/consent and bounded budget required"
        )
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Corrupt recovery exceeded its work budget; retain guard"
            )

    def remaining() -> float:
        check()
        return min(60, deadline - time.monotonic())

    with AppDataLock(canonical) as lock, ExitStack() as leases:
        # Read-only first: never open SQLite RW on an unknown or missing state.
        with _hold_inspected_copy(
            root, copy_journal, app_lock=lock, timeout_seconds=remaining()
        ) as initial_held:
            initial = initial_held.proof
            if initial["inspection_identity"] != reviewed_inspection:
                raise ValueError("Inspection is stale; review the actual state again")
            if initial["actual_state"] != "VALIDATED_COPY_GUARDED":
                raise RuntimeError("Unconfirmed copy state requires separate recovery")
        for folder in (
            canonical,
            installation,
            canonical / "manifests",
            canonical / "runtime",
            canonical / "logs",
        ):
            if (
                folder.resolve(strict=True) != folder
                or folder.is_symlink()
                or folder.is_junction()
                or not folder.is_dir()
            ):
                raise ValueError("Real recovery coordination directories required")
            leases.enter_context(
                RuntimeHandle(folder, directory=True, allow_child_writes=True)
            )
        source = canonical / "creator_loop.sqlite3"
        main = leases.enter_context(hold_recovery_database(source))
        with closing(
            sqlite3.connect(
                source.as_uri() + "?mode=rw", uri=True, timeout=min(10, remaining())
            )
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                with (
                    _hold_inspected_copy(
                        canonical,
                        copy_journal,
                        app_lock=lock,
                        sqlite_shared=True,
                        timeout_seconds=remaining(),
                    ) as proof_held,
                    ExitStack() as media_leases,
                ):
                    proof = proof_held.proof
                    if proof != initial:
                        raise ValueError(
                            "Copy evidence changed in the SQLite lock transition"
                        )
                    copy = _record(copy_journal)
                    preparation = _record(canonical / copy["preparation_manifest"])
                    candidate_value = copy.get("candidate_directory")
                    if not isinstance(candidate_value, str):
                        raise ValueError("Explicit bound candidate required")
                    candidate = Path(candidate_value)
                    candidate_manifest = verify_candidate(candidate, installation)
                    leases.enter_context(RuntimeHandle(candidate, directory=True))
                    for name in ("release-manifest.json", *candidate_manifest["files"]):
                        if _regular(candidate / name) is None:
                            raise ValueError("Unaliased candidate inventory required")
                        leases.enter_context(_open_read_lock(candidate / name))
                    backup_dir = canonical / "backups" / copy["backup_id"]
                    media_leases.enter_context(
                        _open_read_lock(backup_dir / "backup-manifest.json")
                    )
                    media_leases.enter_context(
                        _open_read_lock(backup_dir / "creator_loop.sqlite3")
                    )
                    snapshot, metadata, manifest = _restore_inputs(
                        canonical, copy["backup_id"], installation, candidate
                    )
                    if (
                        manifest_identity(manifest)
                        != copy["candidate_manifest_identity"]
                        or manifest_identity(candidate_manifest)
                        != manifest_identity(manifest)
                        or metadata["schema_version"] != copy["schema_from"]
                        or _digest(snapshot) != preparation["backup_sha256"]
                    ):
                        raise ValueError(
                            "Bound backup/candidate changed before recovery health"
                        )
                    registry = canonical / "manifests/storage-roots.json"
                    registry_sha = None
                    if _regular(registry) is not None:
                        media_leases.enter_context(_open_read_lock(registry))
                        registry_sha = _digest(registry)

                    def verify_current(handles: ExitStack) -> None:
                        check()
                        validate(db)
                        actual, counts = _database_identity(db, deadline)
                        if (
                            actual != proof["current_database_identity"]
                            or counts != preparation["staged_counts"]
                            or _hash(main, check)[0] != proof["current_database_sha256"]
                            or any(
                                entry["status"] != "valid"
                                for entry in _media_assessment(
                                    canonical, db, deadline, handles
                                )
                            )
                        ):
                            raise RuntimeError(
                                "Actual database/media require recovery; retain guard"
                            )

                    verify_current(media_leases)
                    recovery = (
                        canonical
                        / "manifests"
                        / f"corrupt-recovery-{proof['copy_id']}.json"
                    )
                    binding = {
                        "restore_format": 2,
                        "corrupt_recovery_format": 1,
                        "source_kind": "DAMAGED_SQLITE",
                        "update_id": proof["copy_id"],
                        "copy_id": proof["copy_id"],
                        "copy_journal_name": copy_journal.name,
                        "copy_journal_sha256": proof["journal_sha256"],
                        "data_root_identity": proof["data_root_identity"],
                        "preparation_sha256": proof["preparation_sha256"],
                        "candidate_directory": str(candidate),
                        "candidate_commit": manifest["git_commit"],
                        "candidate_manifest_identity": copy[
                            "candidate_manifest_identity"
                        ],
                        "backup_id": copy["backup_id"],
                        "schema_from": copy["schema_from"],
                        "schema_to": SCHEMA_VERSION,
                        "restored_database_identity": proof[
                            "current_database_identity"
                        ],
                    }
                    record: dict[str, Any] = dict(binding)
                    if (
                        recovery.exists()
                        or recovery.is_symlink()
                        or recovery.is_junction()
                    ):
                        record = _record(recovery)
                        if any(
                            record.get(key) != value
                            or type(record.get(key)) is not type(value)
                            for key, value in binding.items()
                        ):
                            raise ValueError(
                                "Existing recovery journal binding differs"
                            )
                    pointer_path = installation / "active-installation.json"
                    previous = record.get("previous_active_pointer")
                    if (
                        pointer_path.exists()
                        or pointer_path.is_symlink()
                        or pointer_path.is_junction()
                    ):
                        observed_pointer = _record(pointer_path)
                        if (
                            observed_pointer.get("status") == "ACTIVE"
                            and observed_pointer.get("update_id") != proof["copy_id"]
                        ):
                            previous = observed_pointer
                        elif observed_pointer.get("update_id") != proof["copy_id"]:
                            raise RuntimeError(
                                "Foreign installation pointer requires recovery"
                            )
                    previous_candidate = None
                    if previous is not None:
                        if not isinstance(previous, dict):
                            raise ValueError("Invalid saved previous pointer")
                        try:
                            previous_candidate = _candidate_from_pointer(
                                installation, previous, SCHEMA_VERSION
                            )
                        except RuntimeError:
                            previous = None  # incompatible version kept, never selected
                    pointer = {
                        "pointer_format": 1,
                        "status": "PENDING_HEALTH",
                        "candidate_name": candidate.name,
                        "manifest_sha256": _digest(candidate / "release-manifest.json"),
                        "update_id": proof["copy_id"],
                    }
                    record.update(
                        previous_active_pointer=previous,
                        phase="ACTIVATION_STARTED",
                        activation_pending=True,
                        restored=False,
                        reviewed_inspection=reviewed_inspection,
                        confirmed_recovery=True,
                        migration_committed=True,
                    )
                    _journal(recovery, record)
                    try:
                        _journal(pointer_path, pointer)
                        record["phase"] = "ACTIVATED_PENDING_HEALTH"
                        health_log = (
                            canonical
                            / "logs"
                            / f"corrupt-health-{proof['copy_id']}-{uuid4().hex}"
                        )
                        record["health_log_directory"] = str(health_log)
                        _journal(recovery, record)
                        run_health_check(
                            candidate / "CreatorLoop/CreatorLoop.exe",
                            canonical,
                            health_log,
                            component_version=manifest["app_version"],
                            timeout_seconds=remaining(),
                        )
                        if _record(pointer_path) != pointer:
                            raise RuntimeError(
                                "Installation pointer changed during health"
                            )
                        verify_current(media_leases)
                        if (
                            _digest(registry)
                            if _regular(registry) is not None
                            else None
                        ) != registry_sha or manifest_identity(
                            verify_candidate(candidate, installation)
                        ) != binding["candidate_manifest_identity"]:
                            raise RuntimeError("Recovery inputs changed during health")
                        pointer["status"] = "ACTIVE"
                        _journal(pointer_path, pointer)
                        record.update(
                            phase="COMPLETED",
                            activation_pending=False,
                            activated_at=datetime.now(timezone.utc).isoformat(),
                        )
                        record.pop("error_type", None)
                        _journal(recovery, record)
                    except BaseException as exc:
                        # Never overwrite a foreign pointer observed during health.
                        try:
                            ours = _record(pointer_path) == pointer
                        except (OSError, ValueError):
                            ours = False
                        if ours:
                            if previous is not None and previous_candidate is not None:
                                try:
                                    _candidate_from_pointer(
                                        installation, previous, SCHEMA_VERSION
                                    )
                                except (OSError, ValueError, RuntimeError):
                                    previous = None
                            if previous is not None:
                                _journal(pointer_path, previous)
                            else:
                                pointer["status"] = "HEALTH_FAILED"
                                _journal(pointer_path, pointer)
                        record.update(
                            phase="HEALTH_FAILED",
                            activation_pending=True,
                            error_type=type(exc).__name__,
                            restored=False,
                        )
                        _journal(recovery, record)
                        raise
                # Media/registry read pins end before atomic success metadata.
                # App lock, writer reservation and native main/candidate leases
                # stay held. Fresh full evidence/media checks follow this gap.
                try:
                    record_successful_update(canonical, db, record, candidate, manifest)
                except BaseException as exc:
                    record.update(
                        phase="COMPLETED_METADATA_PENDING",
                        error_type=type(exc).__name__,
                    )
                    _journal(recovery, record)
                    raise
                marker = canonical / "runtime/restore-in-progress.json"
                with (
                    RuntimeHandle(marker) as guard,
                    _hold_inspected_copy(
                        canonical,
                        copy_journal,
                        app_lock=lock,
                        sqlite_shared=True,
                        timeout_seconds=remaining(),
                        guard_handle=guard,
                    ) as final_held,
                    ExitStack() as final_media,
                ):
                    final = final_held.proof
                    if final != initial or _record(pointer_path) != pointer:
                        raise RuntimeError(
                            "Actual recovery state changed before guard clear"
                        )
                    if _regular(registry) is not None:
                        final_media.enter_context(_open_read_lock(registry))
                    verify_current(final_media)
                    if (
                        manifest_identity(verify_candidate(candidate, installation))
                        != binding["candidate_manifest_identity"]
                    ):
                        raise RuntimeError("Candidate changed before guard clear")
                    record["phase"] = "HEALTH_COMPLETED_GUARDED"
                    _journal(recovery, record)
                    check()
                    # Evidence, media, guard, main inode and writer reservation
                    # remain held together through exact native marker deletion.
                    final_held.recheck()
                    verify_current(final_media)
                    guard.discard()
                record.update(
                    phase="RESTORE_COMPLETED",
                    restored=True,
                    restore_completed_at=datetime.now(timezone.utc).isoformat(),
                )
                _journal(recovery, record)
                return candidate
            finally:
                db.set_progress_handler(None, 0)
                db.rollback()
