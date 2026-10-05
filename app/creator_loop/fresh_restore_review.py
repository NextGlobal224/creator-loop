"""Explicit bound backup/candidate choice for a changed completed guarded copy."""

from __future__ import annotations

import math
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_copy_recovery import _record
from creator_loop.corrupt_copy_review import _review_copy_locked
from creator_loop.corrupt_database import _regular
from creator_loop.corrupt_restore_inspection import _hold_inspected_copy
from creator_loop.installation_stage import _installation_root
from creator_loop.restore_assessment import assessment_identity


@dataclass(frozen=True)
class _FreshRestoreRequest:
    backup_id: str
    candidate_directory: Path
    reviewed_identity: str


def _fresh_choice(
    canonical: Path,
    copy_journal: Path,
    review: dict[str, Any],
    backup_id: str,
    candidate_directory: Path,
) -> dict[str, Any]:
    """Called while actual inspection/candidate/media leases and app lock are held."""
    copy = _record(copy_journal)
    completed_digest = copy.get("copied_database_sha256")
    if (
        not isinstance(completed_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", completed_digest) is None
        or type(copy.get("copied_database_byte_size")) is not int
        or copy["copied_database_byte_size"] <= 0
        or review["requires_fresh_restore_decision"] is not True
        or review["originals_retained_complete"] is not True
    ):
        raise ValueError(
            "Completed physical receipt and complete retained originals required"
        )
    candidate = candidate_directory.resolve(strict=True)
    if (
        backup_id != review["backup_id"]
        or str(candidate) != copy["candidate_directory"]
    ):
        raise ValueError(
            "Explicit selected bound backup/candidate required; no automatic choice"
        )
    current = []
    for entry in review["inspection"]["current_files"]:
        observed = _regular(canonical / entry["name"])
        if (observed is not None) != entry["present"]:
            raise RuntimeError("Current inventory changed during fresh choice")
        if observed is not None and (
            observed.st_size != entry["byte_size"] or observed.st_ino <= 0
        ):
            raise RuntimeError("Stable native current-file identity required")
        current.append(
            {
                **entry,
                "device": observed.st_dev if observed is not None else None,
                "inode": observed.st_ino if observed is not None else None,
            }
        )
    result: dict[str, Any] = {
        "fresh_completed_restore_review_format": 1,
        "data_root_identity": review["data_root_identity"],
        "copy_journal_name": copy_journal.name,
        "installation_root": review["installation_root"],
        "backup_id": backup_id,
        "backup_created_at": review["backup_created_at"],
        "candidate_directory": str(candidate),
        "completed_database_sha256": completed_digest,
        "copy_review": review,
        "current_source_files": current,
        "current_changes_assessable": False,
        "current_counts": None,
        "consistent_current_backup": False,
        "requires_lost_changes_confirmation": True,
        "requires_media_issues_confirmation": review["media_issue_count"] > 0,
        "media_issue_count": review["media_issue_count"],
        "loss_warning": review["loss_warning"]
        + " Current guarded DB/sidecars cannot establish all changes lost; retain the whole current bundle and every previous original/archive.",
        "guard_retained": True,
        "activated": False,
        "restored": False,
        "apply_supported": True,
        "apply_authorized": False,
    }
    result["assessment_identity"] = assessment_identity(result)
    return result


def review_fresh_restore(
    root: Path,
    copy_journal: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Review only; selection never authorizes interrupted or fresh copy by itself.

    First fresh path chooses the original copy's verified bound backup/candidate
    explicitly. It neither infers a newer backup nor interprets/replays current
    WAL/journal as a consistent snapshot. No source initialization or mutation.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Bounded fresh restore review required")
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    deadline = time.monotonic() + timeout_seconds
    with AppDataLock(canonical) as lock, ExitStack() as leases:
        held = leases.enter_context(
            _hold_inspected_copy(
                canonical,
                copy_journal,
                app_lock=lock,
                timeout_seconds=timeout_seconds,
            )
        )
        review = _review_copy_locked(
            canonical, copy_journal, installation, deadline, held, leases
        )
        choice = _fresh_choice(
            canonical, copy_journal, review, backup_id, candidate_directory
        )
        held.recheck()
        if time.monotonic() >= deadline:
            raise TimeoutError("Fresh restore review exceeded budget")
        return choice
