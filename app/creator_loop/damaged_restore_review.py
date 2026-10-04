"""Bound read-only damaged-source assessment and optional raw/stage proofs."""

from __future__ import annotations

import math
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from creator_loop.corrupt_database import _regular, hold_corrupt_database
from creator_loop.corrupt_restore_assessment import _assess_corrupt_locked
from creator_loop.corrupt_stage_validation import hold_corrupt_preparation
from creator_loop.installation_stage import _installation_root
from creator_loop.preserved_source_validation import hold_preserved_source
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import _restore_inputs, assessment_identity
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.update_activation import verify_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_preparation import manifest_identity


def review_damaged_restore(
    root: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    raw_manifest: Path | None = None,
    preparation_manifest: Path | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Review only; optional stage proof never replaces fresh loss/media consent.

    Holds the same app/source leases throughout backup/candidate/media and raw/
    stage validation. Damaged main is never initialized, repaired or replayed.
    A valid historical stage whose assessment no longer matches current inputs
    is shown as stale; copy stays unauthorized until a fresh preparation.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Bounded damaged restore review required")
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    deadline = time.monotonic() + timeout_seconds
    with (
        hold_corrupt_database(root, timeout_seconds=timeout_seconds) as damage,
        ExitStack() as leases,
    ):
        snapshot, metadata, manifest = _restore_inputs(
            canonical, backup_id, installation, candidate_directory
        )
        candidate = candidate_directory.resolve(strict=True)
        leases.enter_context(
            RuntimeHandle(installation, directory=True, allow_child_writes=True)
        )
        leases.enter_context(RuntimeHandle(candidate, directory=True))
        for name in ("release-manifest.json", *manifest["files"]):
            if _regular(candidate / name) is None:
                raise ValueError("Unaliased candidate files required")
            leases.enter_context(_open_read_lock(candidate / name))
        for path in (snapshot, snapshot.parent / "backup-manifest.json"):
            leases.enter_context(_open_read_lock(path))
        assessment = _assess_corrupt_locked(
            canonical, backup_id, installation, candidate, damage, deadline, leases
        )
        if assessment["backup_sha256"] != metadata["database_sha256"] or assessment[
            "candidate_manifest_identity"
        ] != manifest_identity(manifest):
            raise RuntimeError("Selected backup/candidate changed before review")
        raw_proof = None
        stage_proof = None
        if preparation_manifest is not None:
            # Hashing/validation is done in this bounded child, never in Qt UI.
            selected = preparation_manifest.absolute()
            if (
                selected.parent.parent != canonical / "backups"
                or selected.name != "preparation.json"
                or re.fullmatch(r"corrupt-restore-[0-9a-f]{32}", selected.parent.name)
                is None
                or selected.resolve(strict=True) != selected
                or (observed := _regular(selected)) is None
                or not 0 < observed.st_size <= 16 * 1024
            ):
                raise ValueError("Canonical bounded preparation metadata required")
            prepared = leases.enter_context(
                hold_corrupt_preparation(
                    canonical,
                    selected,
                    _digest(selected),
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
            )
            if (
                prepared["backup_id"] != backup_id
                or prepared["candidate_manifest_identity"]
                != assessment["candidate_manifest_identity"]
            ):
                raise ValueError("Preparation belongs to another backup/candidate")
            if (
                raw_manifest is not None
                and raw_manifest.absolute() != canonical / prepared["raw_manifest"]
            ):
                raise ValueError("Selected raw manifest differs from prepared archive")
            # Stage context already owns/revalidates raw directory and files;
            # reuse it rather than reacquiring a conflicting directory DELETE lease.
            raw_proof = {
                "manifest": prepared["raw_manifest"],
                "manifest_sha256": prepared["raw_manifest_sha256"],
                "damage_identity": prepared["damage_identity"],
                "archive_revalidated": True,
                "consistent_backup": False,
            }
            stage_proof = {
                "manifest": str(selected.relative_to(canonical)),
                "manifest_sha256": prepared["preparation_manifest_sha256"],
                "stage_revalidated": True,
                "current_binding_matches": prepared["reviewed_assessment_identity"]
                == assessment["assessment_identity"],
                "apply_authorized": False,
                "activated": False,
                "restored": False,
            }
        elif raw_manifest is not None:
            verified = leases.enter_context(
                hold_preserved_source(
                    canonical,
                    raw_manifest,
                    damage["damage_identity"],
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
            )
            raw_proof = {
                "manifest": str(raw_manifest.absolute().relative_to(canonical)),
                "manifest_sha256": verified["archive_manifest_sha256"],
                "damage_identity": verified["damage_identity"],
                "archive_revalidated": True,
                "consistent_backup": False,
            }
        if (
            manifest_identity(verify_candidate(candidate, installation))
            != assessment["candidate_manifest_identity"]
        ):
            raise RuntimeError("Candidate inventory changed during combined review")
        if time.monotonic() >= deadline:
            raise TimeoutError("Damaged restore review exceeded budget")
        result: dict[str, Any] = {
            "damaged_restore_review_format": 1,
            "data_root_identity": assessment["data_root_identity"],
            "installation_root": str(installation),
            "candidate_directory": str(candidate),
            "assessment": assessment,
            "raw_source": raw_proof,
            "preparation": stage_proof,
            "copy_supported": bool(
                stage_proof and stage_proof["current_binding_matches"]
            ),
            "activated": False,
            "restored": False,
        }
        result["assessment_identity"] = assessment_identity(result)
        return result
