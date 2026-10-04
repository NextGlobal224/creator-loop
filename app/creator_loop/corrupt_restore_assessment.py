"""Explicit backup/candidate review while retaining a damaged source read lease."""

from __future__ import annotations

import hashlib
import time
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from creator_loop.corrupt_database import hold_corrupt_database
from creator_loop.database import SCHEMA_VERSION, open_readonly
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import (
    LOSS_WARNING,
    _check_deadline,
    _database_identity,
    _media_assessment,
    _restore_inputs,
    assessment_identity,
)
from creator_loop.storage_roots import _load
from creator_loop.update_backup import _digest
from creator_loop.update_preparation import manifest_identity


def assess_corrupt_restore(
    root: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Review damaged bytes and a validated backup without modifying either.

    No current row counts/change loss can be inferred from an unreadable source.
    This result is not accepted by ordinary restore apply; corrupt-source apply,
    raw-source preservation and fresh confirmation remain separate work.
    """
    deadline = time.monotonic() + timeout_seconds
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    with hold_corrupt_database(root, timeout_seconds=timeout_seconds) as damage:
        with ExitStack() as handles:
            return _assess_corrupt_locked(
                canonical,
                backup_id,
                installation,
                candidate_directory,
                damage,
                deadline,
                handles,
            )


def _assess_corrupt_locked(
    canonical: Path,
    backup_id: str,
    installation: Path,
    candidate_directory: Path,
    damage: dict[str, Any],
    deadline: float,
    handles: ExitStack,
) -> dict[str, Any]:
    """Caller retains app/source leases; input/media leases live in its stack."""
    registry_path = canonical / "manifests/storage-roots.json"
    registry = None
    if (
        registry_path.exists()
        or registry_path.is_symlink()
        or registry_path.is_junction()
    ):
        handles.enter_context(_open_read_lock(registry_path))
        registry = _load(registry_path)
    snapshot, metadata, manifest = _restore_inputs(
        canonical, backup_id, installation, candidate_directory
    )
    # Hold the validated backup during counts/media review, then verify
    # the same input again. This never makes source damage a valid backup.
    handles.enter_context(_open_read_lock(snapshot))
    with closing(open_readonly(snapshot.resolve(strict=True))) as db:
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            _, counts = _database_identity(db, deadline)
            media = _media_assessment(canonical, db, deadline, handles)
        finally:
            db.set_progress_handler(None, 0)
    fresh_snapshot, fresh_metadata, fresh_manifest = _restore_inputs(
        canonical, backup_id, installation, candidate_directory
    )
    if (
        fresh_snapshot != snapshot
        or fresh_metadata != metadata
        or manifest_identity(fresh_manifest) != manifest_identity(manifest)
    ):
        raise RuntimeError("Backup/candidate changed during damaged source review")
    _check_deadline(deadline)
    assessment: dict[str, Any] = {
        "restore_assessment_format": 2,
        "source_kind": "DAMAGED_SQLITE",
        "assessed_at": datetime.now(timezone.utc).isoformat(),
        "data_root_identity": hashlib.sha256(
            str(canonical).encode("utf-8")
        ).hexdigest(),
        "data_root_id": registry.get("data_root_id") if registry else None,
        "registry_sha256": _digest(registry_path) if registry else None,
        "damage": damage,
        "current_schema": None,
        "current_counts": None,
        "current_changes_assessable": False,
        "backup_id": backup_id,
        "backup_created_at": metadata["created_at"],
        "backup_schema": metadata["schema_version"],
        "backup_sha256": metadata["database_sha256"],
        "backup_counts": counts,
        "target_schema": SCHEMA_VERSION,
        "candidate_name": candidate_directory.name,
        "candidate_manifest_identity": manifest_identity(manifest),
        "media_included": False,
        "media_assessment": media,
        "media_issue_count": sum(entry["status"] != "valid" for entry in media),
        "loss_warning": LOSS_WARNING
        + " The damaged current DB cannot establish which changes would be lost or whether WAL/journal recovery is possible.",
        "requires_lost_changes_confirmation": True,
        "requires_media_issues_confirmation": any(
            entry["status"] != "valid" for entry in media
        ),
        "raw_source_preserved": False,
        "apply_supported": False,
        "restored": False,
    }
    assessment["assessment_identity"] = assessment_identity(assessment)
    return assessment
