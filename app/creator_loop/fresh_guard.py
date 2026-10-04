"""Anchor fresh history in the live guard before moving any current bytes."""

from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.corrupt_source_preservation import _create_file, _publish_new_manifest
from creator_loop.publication_media import _open_read_lock
from creator_loop.update_preparation import _journal


def fresh_guard_names(guard: dict[str, Any], copy_id: str) -> list[str]:
    names = guard.get("fresh_restore_journals", [])
    if (
        not isinstance(names, list)
        or ("fresh_restore_journals" in guard and not names)
        or len(names) > 64
        or any(
            not isinstance(name, str)
            or re.fullmatch(
                rf"corrupt-fresh-restore-{copy_id}-[0-9a-f]{{32}}\.json", name
            )
            is None
            for name in names
        )
        or names != sorted(set(names))
    ):
        raise ValueError("Typed canonical bounded fresh guard history required")
    return names


def anchor_fresh_guard(
    root: Path,
    previous: dict[str, Any],
    names: list[str],
    leases: ExitStack,
) -> dict[str, Any]:
    """Caller holds app lock/exclusive current files and verified old history.

    Preserve the original guard as an immutable archive. Atomically extend the
    live guard and re-pin it before source mutation. A pending journal may be
    published before this anchor; without an anchor it must still have all its
    current native originals live. No data restoration or guard clearing here.
    """
    from creator_loop.corrupt_copy_recovery import _record

    marker = root / "runtime/restore-in-progress.json"
    if _record(marker) != previous:
        raise ValueError("Fresh guard changed before authorized history extension")
    extended = {**previous, "fresh_restore_journals": names}
    fresh_guard_names(extended, previous["copy_id"])
    if not set(fresh_guard_names(previous, previous["copy_id"])) <= set(names):
        raise ValueError("Fresh guard history may only grow")
    archived = root / "backups" / f"corrupt-guard-{previous['copy_id']}.json"
    base = {
        key: value for key, value in previous.items() if key != "fresh_restore_journals"
    }
    if archived.exists():
        leases.enter_context(_open_read_lock(archived))
        if _record(archived) != base:
            raise ValueError("Existing original guard archive differs")
    elif "fresh_restore_journals" in previous:
        raise ValueError("Original fresh guard archive missing")
    else:
        # Preserve actual guarded evidence bytes, including valid whitespace;
        # parsing and serializing the same object is not byte preservation.
        with _open_read_lock(marker) as reader:
            original = reader.read(16385)
            if not 0 < len(original) <= 16384 or _record(marker) != previous:
                raise ValueError("Bounded unchanged original guard bytes required")
            temporary = archived.with_name(f".{archived.name}.{uuid4().hex}.pending")
            writer = _create_file(leases, temporary)
            if writer.write(original) != len(original):
                raise OSError("Incomplete original guard archive; retain evidence")
            writer.flush()
            os.fsync(writer.fileno())
            writer.seek(0)
            if writer.read(len(original) + 1) != original:
                raise RuntimeError("Original guard archive readback differs")
            _publish_new_manifest(temporary, archived, writer)
    # Same atomic metadata publication used by the updater; guard never has an
    # absent interval. Pin the replacement and verify before moving any byte.
    _journal(marker, extended)
    leases.enter_context(_open_read_lock(marker))
    if _record(marker) != extended:
        raise ValueError("Fresh history guard changed before native retention")
    return extended
