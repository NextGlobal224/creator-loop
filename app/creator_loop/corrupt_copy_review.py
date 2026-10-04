"""Read-only published backup/candidate/media review for a selected guarded copy."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_copy_recovery import _record
from creator_loop.corrupt_database import _regular
from creator_loop.corrupt_restore_inspection import _hold_inspected_copy
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import (
    LOSS_WARNING,
    _media_assessment,
    _restore_inputs,
    assessment_identity,
)
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.update_activation import verify_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_preparation import manifest_identity


def review_corrupt_copy(
    root: Path,
    copy_journal: Path,
    installation_root: Path,
    *,
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    """Show actual copy proof, selected backup time and fresh media; no apply.

    This is an advisory review. Resume/recovery recheck actual inspection under
    their own locks; neither accepts this review identity as a substitute for
    physical proof or explicit loss/partial/media/recovery consent.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Bounded copy review required")
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    deadline = time.monotonic() + timeout_seconds
    with AppDataLock(canonical) as lock, ExitStack() as leases:
        inspected = leases.enter_context(
            _hold_inspected_copy(
                root,
                copy_journal,
                app_lock=lock,
                timeout_seconds=timeout_seconds,
            )
        )
        proof = inspected.proof
        copy = _record(copy_journal)
        candidate_value = copy.get("candidate_directory")
        if not isinstance(candidate_value, str):
            raise ValueError("Bound explicit candidate path required")
        candidate = Path(candidate_value)
        leases.enter_context(
            RuntimeHandle(installation, directory=True, allow_child_writes=True)
        )
        snapshot, metadata, manifest = _restore_inputs(
            canonical, copy["backup_id"], installation, candidate
        )
        leases.enter_context(RuntimeHandle(candidate, directory=True))
        for name in ("release-manifest.json", *manifest["files"]):
            if _regular(candidate / name) is None:
                raise ValueError("Unaliased candidate files required")
            leases.enter_context(_open_read_lock(candidate / name))
        backup = snapshot.parent
        leases.enter_context(RuntimeHandle(backup, directory=True))
        for path in (snapshot, backup / "backup-manifest.json"):
            leases.enter_context(_open_read_lock(path))
        snapshot, metadata, fresh = _restore_inputs(
            canonical, copy["backup_id"], installation, candidate
        )
        if (
            metadata["schema_version"] != copy["schema_from"]
            or manifest_identity(manifest) != copy["candidate_manifest_identity"]
            or manifest_identity(fresh) != manifest_identity(manifest)
        ):
            raise ValueError(
                "Selected backup/candidate differs from guarded preparation"
            )
        preparation_path = canonical / copy["preparation_manifest"]
        preparation = _record(preparation_path)
        if _digest(snapshot) != preparation["backup_sha256"]:
            raise ValueError("Published backup changed since preparation")
        registry = canonical / "manifests/storage-roots.json"
        if _regular(registry) is not None:
            leases.enter_context(_open_read_lock(registry))
        registry_sha = _digest(registry) if registry.exists() else None
        staged = preparation_path.parent / "restored.sqlite3"
        with closing(
            sqlite3.connect(staged.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as reader:
            reader.execute("PRAGMA foreign_keys=ON")
            reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            media = _media_assessment(canonical, reader, deadline, leases)
        retained = canonical / "backups" / copy["retained_directory"]
        originals_complete = (
            bool(proof["original_locations"])
            and all(
                entry["in_retention"] and not entry["in_live_source"]
                for entry in proof["original_locations"]
            )
            and {path.name for path in retained.iterdir()}
            == {
                entry["name"]
                for entry in copy["damage"]["raw_files"]
                if entry["present"]
            }
        )
        review: dict[str, Any] = {
            "corrupt_copy_review_format": 1,
            "data_root_identity": proof["data_root_identity"],
            "copy_journal_name": copy_journal.name,
            "installation_root": str(installation),
            "inspection": proof,
            "backup_id": copy["backup_id"],
            "backup_created_at": metadata["created_at"],
            "backup_schema": metadata["schema_version"],
            "target_schema": copy["schema_to"],
            "candidate_name": candidate.name,
            "media_included": False,
            "media_assessment": media,
            "media_issue_count": sum(entry["status"] != "valid" for entry in media),
            "loss_warning": LOSS_WARNING,
            "unknown_continuation_allowed": (
                proof["actual_state"].startswith("UNKNOWN_")
                and originals_complete
                and copy.get("copied_database_sha256") is None
            ),
            "activated": False,
            "restored": False,
        }
        inspected.recheck()
        if (
            manifest_identity(verify_candidate(candidate, installation))
            != copy["candidate_manifest_identity"]
        ):
            raise RuntimeError("Candidate inventory changed during review")
        if (_digest(registry) if registry.exists() else None) != registry_sha:
            raise RuntimeError("Media registry changed during review")
        if time.monotonic() >= deadline:
            raise TimeoutError("Copy review exceeded budget")
        review["assessment_identity"] = assessment_identity(review)
        return review
