"""Distinct explicitly consented fresh copy of a changed completed guarded DB."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from creator_loop.corrupt_copy_resume import _continue_corrupt_copy
from creator_loop.fresh_restore_review import _FreshRestoreRequest


def copy_fresh_restore(
    root: Path,
    copy_journal: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    reviewed_fresh_restore: str,
    confirm_fresh_restore: bool = False,
    confirm_lost_changes: bool = False,
    confirm_media_issues: bool = False,
    timeout_seconds: float = 180,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Revalidate the explicit choice, retain every bundle, keep launch guarded.

    Neither an interrupted-copy token nor its partial/unknown consent is
    accepted. No health, activation, guard removal or archive cleanup occurs.
    """
    if (
        confirm_fresh_restore is not True
        or confirm_lost_changes is not True
        or type(confirm_media_issues) is not bool
        or not isinstance(reviewed_fresh_restore, str)
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_fresh_restore) is None
        or not isinstance(backup_id, str)
    ):
        raise ValueError("Distinct fresh choice and fresh/loss consent required")
    return _continue_corrupt_copy(
        root,
        copy_journal,
        installation_root,
        reviewed_inspection=None,
        fresh_request=_FreshRestoreRequest(
            backup_id, candidate_directory, reviewed_fresh_restore
        ),
        confirm_lost_changes=True,
        confirm_media_issues=confirm_media_issues,
        timeout_seconds=timeout_seconds,
        cancelled=cancelled,
    )
