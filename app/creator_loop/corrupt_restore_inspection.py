"""Read-only actual-state inspection of an interrupted guarded corrupt restore."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, BinaryIO

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_database import _NAMES, _identity, _regular
from creator_loop.corrupt_restore_copy import _hash
from creator_loop.corrupt_stage_validation import hold_corrupt_preparation
from creator_loop.database import validate
from creator_loop.preserved_source_validation import _unique_object
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import _database_identity
from creator_loop.runtime_files import RuntimeHandle


def inspect_corrupt_copy(
    root: Path, journal_path: Path, *, timeout_seconds: float = 60
) -> dict[str, Any]:
    """Pin input bytes and classify actual state, never trust a journal phase.

    No live DB initialization/replay, source recopy, original/guard deletion,
    installation activation or health is performed. A matching closed restored
    DB is still guarded until explicit recovery/fresh health. Unknown states
    keep all bytes and must not be automatically guessed or overwritten.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Bounded corrupt-copy inspection budget required")
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Corrupt-copy inspection exceeded its work budget")

    canonical = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(canonical)):
        raise ValueError("Canonical data root required")
    selected = journal_path.absolute()
    match = re.fullmatch(r"corrupt-restore-([0-9a-f]{32})\.json", selected.name)
    if (
        match is None
        or selected.parent != canonical / "manifests"
        or selected.resolve(strict=True) != selected
    ):
        raise ValueError("Explicit canonical corrupt-copy journal required")
    copy_id = match[1]
    root_id = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    with AppDataLock(canonical), ExitStack() as handles:
        for folder in (
            canonical,
            canonical / "manifests",
            canonical / "runtime",
            canonical / "backups",
        ):
            if (
                folder.is_symlink()
                or folder.is_junction()
                or not folder.is_dir()
                or folder.resolve(strict=True) != folder
            ):
                raise ValueError("Real corrupt-copy inspection directories required")
            if sys.platform == "win32":
                handles.enter_context(
                    RuntimeHandle(folder, directory=True, allow_child_writes=True)
                )
        if {path.name for path in (canonical / "runtime").iterdir()} != {
            "app-data.lock",
            "restore-in-progress.json",
        }:
            raise ValueError(
                "An explicit guarded copy with no other runtime work is required"
            )
        observed: dict[Path, tuple[BinaryIO, os.stat_result]] = {}
        absent: set[Path] = set()

        def acquire(path: Path) -> BinaryIO | None:
            check()
            before = _regular(path)
            if before is None:
                absent.add(path)
                return None
            stream = handles.enter_context(_open_read_lock(path))
            actual = os.fstat(stream.fileno())
            if _identity(actual) != _identity(before):
                raise RuntimeError("Copy evidence changed before read lease")
            observed[path] = stream, actual
            return stream

        def read_record(path: Path) -> dict[str, Any]:
            stream = acquire(path)
            if stream is None or not 0 < os.fstat(stream.fileno()).st_size <= 1024**2:
                raise ValueError("Bounded existing guarded-copy metadata required")
            record = json.loads(
                stream.read(1024**2 + 1).decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
            if not isinstance(record, dict):
                raise ValueError("Guarded-copy metadata object required")
            return record

        record = read_record(selected)
        guard = read_record(canonical / "runtime/restore-in-progress.json")
        if (
            type(record.get("corrupt_restore_copy_format")) is not int
            or record["corrupt_restore_copy_format"] != 1
            or record.get("copy_id") != copy_id
            or record.get("data_root_identity") != root_id
            or record.get("confirmed_lost_changes") is not True
            or type(record.get("confirmed_media_issues")) is not bool
            or record.get("activation_pending") is not True
            or record.get("restored") is not False
            or record.get("retained_directory") != f"corrupt-original-{copy_id}"
            or guard
            != {
                "corrupt_restore_copy_format": 1,
                "copy_id": copy_id,
                "journal_name": selected.name,
                "data_root_identity": root_id,
                "preparation_sha256": record.get("preparation_sha256"),
            }
            or type(guard["corrupt_restore_copy_format"]) is not int
        ):
            raise ValueError("Guard/journal/root/consent identity mismatch")
        relative = record.get("preparation_manifest")
        if (
            not isinstance(relative, str)
            or re.fullmatch(
                r"backups[\\/]corrupt-restore-[0-9a-f]{32}[\\/]preparation\.json",
                relative,
            )
            is None
        ):
            raise ValueError("Canonical preparation binding required")
        preparation_sha = record.get("preparation_sha256")
        if not isinstance(preparation_sha, str):
            raise ValueError("Explicit preparation digest required")
        preparation = handles.enter_context(
            hold_corrupt_preparation(
                canonical,
                canonical / relative,
                preparation_sha,
                timeout_seconds=timeout_seconds,
            )
        )
        if (
            record.get("reviewed_identity")
            != preparation["reviewed_assessment_identity"]
            or record.get("backup_id") != preparation["backup_id"]
            or type(record.get("schema_from")) is not int
            or record["schema_from"] != preparation["schema_from"]
            or type(record.get("schema_to")) is not int
            or record["schema_to"] != preparation["schema_to"]
            or record.get("restored_database_identity")
            != preparation["staged_database_identity"]
            or record.get("candidate_manifest_identity")
            != preparation["candidate_manifest_identity"]
        ):
            raise ValueError("Copy journal differs from bound preparation")
        # Stage validation already retains/revalidates the raw directory and
        # every raw file through this context. Reuse that native ownership;
        # a second directory DELETE handle conflicts with its denied sharing.
        raw_path = canonical / preparation["raw_manifest"]
        archive = read_record(raw_path)
        raw_stream, _ = observed[raw_path]
        if _hash(raw_stream, check)[0] != preparation["raw_manifest_sha256"]:
            raise ValueError("Bound raw manifest changed during inspection")
        if record.get("damage") != archive["damage"]:
            raise ValueError(
                "Copy journal differs from validated original raw evidence"
            )
        retained = canonical / "backups" / f"corrupt-original-{copy_id}"
        if (
            retained.is_symlink()
            or retained.is_junction()
            or not retained.is_dir()
            or retained.resolve(strict=True) != retained
        ):
            raise ValueError("Real original-source retention directory required")
        if sys.platform == "win32":
            handles.enter_context(RuntimeHandle(retained, directory=True))
        inventory = {path.name for path in retained.iterdir()}
        entries = archive["damage"]["raw_files"]
        expected = {entry["name"] for entry in entries if entry["present"]}
        locations: list[dict[str, Any]] = []
        current: dict[str, tuple[str, int] | None] = {}
        missing_or_duplicate = False
        unexpected = bool(inventory - expected)
        for entry in entries:
            live = acquire(canonical / entry["name"])
            old = acquire(retained / entry["name"])
            current[entry["name"]] = _hash(live, check) if live is not None else None
            old_digest = _hash(old, check) if old is not None else None
            if not entry["present"]:
                if live is not None or old is not None:
                    unexpected = True
                continue
            original = (entry["sha256"], entry["byte_size"])
            live_match = current[entry["name"]] == original
            old_match = old_digest == original
            missing_or_duplicate |= live_match == old_match
            if old is not None and not old_match:
                unexpected = True
            if (
                entry["name"] != "creator_loop.sqlite3"
                and live is not None
                and not live_match
            ):
                unexpected = True
            locations.append(
                {
                    "name": entry["name"],
                    "in_live_source": live_match,
                    "in_retention": old_match,
                }
            )
        disposition = "UNKNOWN_SOURCE_EVIDENCE"
        actual_identity = None
        main = current["creator_loop.sqlite3"]
        if not unexpected and not missing_or_duplicate:
            all_live = all(entry["in_live_source"] for entry in locations)
            all_retained = all(entry["in_retention"] for entry in locations)
            if all_live:
                disposition = "SOURCE_NOT_MOVED_GUARDED"
            elif not all_retained:
                disposition = "SOURCE_PARTIALLY_RETAINED_GUARDED"
            elif any(
                current[name] is not None
                for name in _NAMES
                if name != "creator_loop.sqlite3"
            ):
                disposition = "UNKNOWN_LIVE_SIDECARS"
            elif main is None:
                disposition = "SOURCE_RETAINED_LIVE_MISSING_GUARDED"
            elif main[1] == 0:
                disposition = "EMPTY_CURRENT_DATABASE_GUARDED"
            else:
                disposition = "UNKNOWN_CURRENT_DATABASE"
                check()
                # Immutable view never replays sidecars or creates a missing DB.
                try:
                    with closing(
                        sqlite3.connect(
                            (canonical / "creator_loop.sqlite3").as_uri()
                            + "?mode=ro&immutable=1",
                            uri=True,
                        )
                    ) as db:
                        db.execute("PRAGMA foreign_keys=ON")
                        db.set_progress_handler(
                            lambda: int(time.monotonic() >= deadline), 1000
                        )
                        validate(db)
                        actual_identity, counts = _database_identity(db, deadline)
                    if (
                        actual_identity == preparation["staged_database_identity"]
                        and counts == preparation["staged_counts"]
                    ):
                        disposition = "VALIDATED_COPY_GUARDED"
                        recorded_sha = record.get("copied_database_sha256")
                        if (
                            recorded_sha is not None
                            and (recorded_sha, record.get("copied_database_byte_size"))
                            != main
                        ):
                            disposition = "UNKNOWN_CURRENT_PHYSICAL_BYTES"
                except (sqlite3.Error, RuntimeError):
                    check()
        check()
        if any(_regular(path) is not None for path in absent):
            raise RuntimeError(
                "Previously absent copy evidence appeared during inspection"
            )
        for path, (stream, before) in observed.items():
            actual = _regular(path)
            if (
                actual is None
                or _identity(actual) != _identity(before)
                or _identity(os.fstat(stream.fileno())) != _identity(before)
            ):
                raise RuntimeError("Copy evidence changed during inspection")
        if {path.name for path in retained.iterdir()} != inventory:
            raise RuntimeError("Retention inventory changed during inspection")
        phases = {
            "CORRUPT_COPY_GUARD_PENDING",
            "CORRUPT_COPY_GUARDED",
            "CORRUPT_SOURCE_RETENTION_STARTED",
            "CORRUPT_SOURCE_RETAINED",
            "CORRUPT_COPY_STARTED",
            "CORRUPT_DB_COMMITTED_GUARDED",
        }
        phase = record.get("phase")
        result: dict[str, Any] = {
            "corrupt_restore_inspection_format": 1,
            "data_root_identity": root_id,
            "journal_sha256": _hash(observed[selected][0], check)[0],
            "preparation_sha256": preparation_sha,
            "copy_id": copy_id,
            "recorded_phase": phase
            if isinstance(phase, str) and phase in phases
            else "UNRECOGNIZED",
            "actual_state": disposition,
            "original_locations": locations,
            "current_database_identity": actual_identity,
            "current_database_sha256": main[0] if main is not None else None,
            "guard_retained": True,
            "requires_recovery_health": True,
            "activated": False,
            "restored": False,
        }
        result["inspection_identity"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result
