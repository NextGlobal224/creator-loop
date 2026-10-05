"""Persist external selections as coordination metadata, not installations."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.local_components import (
    DEFAULT_WORKER_MEMORY,
    ComponentPreflightError,
    ComponentSpec,
    component_review_fingerprint,
    hold_verified_components,
    load_component_manifest,
    parse_component_manifest,
)
from creator_loop.storage_roots import (
    StorageRootError,
    _load,
    _manifest_path,
    _write_manifest,
)
from creator_loop.windows_paths import file_io_path


@dataclass(frozen=True)
class ComponentSelection:
    specs: tuple[ComponentSpec, ...]
    checked_at: str
    worker_memory_limit: int


def _selection(payload: dict[str, object]) -> ComponentSelection | None:
    entry = payload.get("component_selection")
    if entry is None:
        return None
    if (
        not isinstance(entry, dict)
        or set(entry)
        != {
            "manifest",
            "checked_at",
            "worker_memory_limit",
            "check",
            "runtime_compatibility_verified",
        }
        or entry["check"] != "LOCAL_ARTIFACTS_VERIFIED"
        or entry["runtime_compatibility_verified"] is not False
        or not isinstance(entry["checked_at"], str)
        or type(entry["worker_memory_limit"]) is not int
        or not 64 * 1024**2 <= entry["worker_memory_limit"] <= 4 * 1024**3
    ):
        raise ComponentPreflightError("Invalid saved component selection")
    try:
        timestamp = datetime.fromisoformat(entry["checked_at"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timezone required")
        specs = parse_component_manifest(json.dumps(entry["manifest"]).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ComponentPreflightError("Invalid saved component selection") from exc
    if any(spec.worker_memory_bytes > entry["worker_memory_limit"] for spec in specs):
        raise ComponentPreflightError("Saved component selection exceeds Job budget")
    return ComponentSelection(specs, entry["checked_at"], entry["worker_memory_limit"])


def _selection_path(root: Path) -> Path:
    path = _manifest_path(root)
    if path.parent.is_symlink() or path.parent.is_junction():
        raise ComponentPreflightError("Real coordination folder required")
    io = file_io_path(path)
    if io.is_symlink() or io.is_junction() or (io.exists() and io.stat().st_nlink != 1):
        raise ComponentPreflightError("Unaliased coordination file required")
    return path


def load_component_selection(root: Path) -> ComponentSelection | None:
    """Historical declarations only; never treat this read as fresh preflight."""
    path = _selection_path(root)
    return _selection(_load(path)) if file_io_path(path).exists() else None


def save_component_selection(
    root: Path,
    specs: tuple[ComponentSpec, ...],
    *,
    lock: AppDataLock,
    worker_memory_limit: int = DEFAULT_WORKER_MEMORY,
    cancelled: Callable[[], bool] = lambda: False,
) -> ComponentSelection:
    """Caller holds app lock; hash off-GUI, then serialize all manifest writers.

    SQLite writer reservation coordinates with registry/update APIs. No domain
    changes or migrations. Atomic metadata is independent of DB rollback.
    Each actual processing operation must reverify and retain its own lease.
    """
    canonical = root.resolve(strict=True)
    if not lock.held or lock.root != canonical:
        raise RuntimeError("Selection requires this data root's held app lock")
    source = canonical / "creator_loop.sqlite3"
    if (
        source.is_symlink()
        or source.is_junction()
        or not source.is_file()
        or source.stat().st_nlink != 1
    ):
        raise ComponentPreflightError("Existing unaliased user database required")
    with hold_verified_components(
        specs, worker_memory_limit=worker_memory_limit, cancelled=cancelled
    ) as verified:
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=5)
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                validate(db)
                path = _selection_path(canonical)
                payload = (
                    _load(path)
                    if file_io_path(path).exists()
                    else {
                        "manifest_version": 1,
                        "data_root_id": uuid4().hex,
                        "schema_version": SCHEMA_VERSION,
                        "storage_roots": [],
                    }
                )
                if payload["schema_version"] != SCHEMA_VERSION:
                    raise ComponentPreflightError(
                        "Coordination schema metadata needs update repair"
                    )
                if not isinstance(payload.get("component_installations", []), list):
                    raise ComponentPreflightError(
                        "Invalid existing component ownership registry"
                    )
                _selection(
                    payload
                )  # preserve malformed history for diagnosis, do not silently replace it
                entries = [
                    {**asdict(item.spec), "path": str(item.spec.path)}
                    for item in verified
                ]
                payload["component_selection"] = {
                    "manifest": {"manifest_version": 1, "components": entries},
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "worker_memory_limit": worker_memory_limit,
                    "check": "LOCAL_ARTIFACTS_VERIFIED",
                    "runtime_compatibility_verified": False,
                }
                if (
                    len(
                        json.dumps(payload, ensure_ascii=False, indent=2).encode(
                            "utf-8"
                        )
                    )
                    > 1024**2
                ):
                    raise ComponentPreflightError(
                        "Selection would exceed coordination metadata budget"
                    )
                if cancelled():
                    raise InterruptedError(
                        "Component selection cancelled before publication"
                    )
                _write_manifest(path, payload)
                result = _selection(payload)
                assert result is not None
                return result
            finally:
                db.rollback()


def select_components_cli(
    root: Path,
    manifest: Path,
    reviewed_fingerprint: str | None = None,
    *,
    worker_memory_limit: int = DEFAULT_WORKER_MEMORY,
) -> int:
    """Explicit selection with the app closed; existing compatible DB only."""
    try:
        specs = load_component_manifest(manifest)
        if reviewed_fingerprint is not None and (
            re.fullmatch(r"[0-9a-f]{64}", reviewed_fingerprint) is None
            or component_review_fingerprint(specs) != reviewed_fingerprint
        ):
            raise ComponentPreflightError(
                "Selection changed after review; check it again before saving"
            )
        with AppDataLock(root) as lock:
            selection = save_component_selection(
                root, specs, lock=lock, worker_memory_limit=worker_memory_limit
            )
        print(
            json.dumps(
                {
                    "selection": "SAVED",
                    "check": "LOCAL_ARTIFACTS_VERIFIED",
                    "checked_at": selection.checked_at,
                    "runtime_compatibility_verified": False,
                    "worker_memory_limit": selection.worker_memory_limit,
                    "component_ids": [spec.component_id for spec in selection.specs],
                }
            )
        )
        return 0
    except DataRootBusy:
        print(
            json.dumps(
                {
                    "selection": "FAILED",
                    "reason": "Close the app using this data root before saving selections",
                }
            )
        )
        return 3
    except (
        ComponentPreflightError,
        StorageRootError,
        RuntimeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        reason = (
            str(exc)
            if isinstance(
                exc, (ComponentPreflightError, StorageRootError, RuntimeError)
            )
            else type(exc).__name__
        )
        print(json.dumps({"selection": "FAILED", "reason": reason}))
        return 1


def inspect_components_cli(root: Path) -> int:
    """Read saved declarations, including offline files; not a fresh check."""
    try:
        from creator_loop.local_components import component_manifest_bytes

        selection = load_component_selection(root)
        print(
            json.dumps(
                {
                    "selection": "HISTORY"
                    if selection is not None
                    else "NOT_CONFIGURED",
                    "freshly_verified": False,
                    "runtime_compatibility_verified": False,
                    "checked_at": selection.checked_at
                    if selection is not None
                    else None,
                    "manifest": json.loads(component_manifest_bytes(selection.specs))
                    if selection is not None
                    else None,
                }
            )
        )
        return 0
    except (ComponentPreflightError, StorageRootError, OSError) as exc:
        print(json.dumps({"selection": "FAILED", "reason": type(exc).__name__}))
        return 1
