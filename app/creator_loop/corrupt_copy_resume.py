"""Explicit continuation of known interrupted copies, retaining partial bytes."""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_copy_recovery import _record
from creator_loop.corrupt_database import _NAMES, _regular
from creator_loop.corrupt_restore_copy import _hash
from creator_loop.corrupt_restore_inspection import _hold_inspected_copy
from creator_loop.corrupt_source_retention import hold_existing_corrupt_source
from creator_loop.corrupt_stage_validation import hold_corrupt_preparation
from creator_loop.database import validate
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import (
    _database_identity,
    _media_assessment,
    _restore_inputs,
)
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.sqlite_stage_file import hold_new_sqlite_target
from creator_loop.update_activation import verify_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_preparation import _journal, manifest_identity

_KNOWN = {
    "SOURCE_NOT_MOVED_GUARDED",
    "SOURCE_PARTIALLY_RETAINED_GUARDED",
    "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
    "EMPTY_CURRENT_DATABASE_GUARDED",
}


def resume_corrupt_copy(
    root: Path,
    copy_journal: Path,
    installation_root: Path,
    *,
    reviewed_inspection: str,
    confirm_lost_changes: bool = False,
    confirm_keep_partial: bool = False,
    confirm_preserve_unknown: bool = False,
    confirm_media_issues: bool = False,
    timeout_seconds: float = 180,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Only known fresh states; no overwrite/deletion, activation or guard clear.

    Remaining original inodes go to the original copy's retention directory.
    Existing empty or explicitly reviewed unknown partial files are retained
    separately before CREATE_NEW/BackupAPI. Unknown consent is valid only for
    an interrupted copy whose complete original source is already retained.
    No silent repair, recopy or cleanup is authorized by a journal phase.
    """
    if sys.platform != "win32":
        raise OSError("Corrupt-copy continuation requires Windows ownership")
    if (
        confirm_lost_changes is not True
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_inspection) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Fresh inspection/loss consent and bounded budget required")
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled and cancelled():
            raise InterruptedError("Copy continuation cancelled; retain guard/evidence")
        if time.monotonic() >= deadline:
            raise TimeoutError("Copy continuation exceeded budget; retain guard")

    def remaining() -> float:
        check()
        return deadline - time.monotonic()

    with AppDataLock(canonical) as lock, ExitStack() as leases:
        with _hold_inspected_copy(
            root, copy_journal, app_lock=lock, timeout_seconds=remaining()
        ) as held:
            proof = held.proof
            if proof["inspection_identity"] != reviewed_inspection:
                raise ValueError("Inspection changed; review actual copy state again")
            copy = _record(copy_journal)
            if proof["actual_state"] != "VALIDATED_COPY_GUARDED" and (
                copy.get("copied_database_sha256") is not None
                or copy.get("phase") == "CORRUPT_DB_COMMITTED_GUARDED"
            ):
                raise RuntimeError(
                    "A changed completed copy requires a fresh restore decision"
                )
            unknown = proof["actual_state"].startswith("UNKNOWN_")
            if proof["actual_state"] not in _KNOWN and not (
                unknown and confirm_preserve_unknown is True
            ):
                raise RuntimeError("Unknown or already copied state cannot be recopied")
            if unknown and (
                not proof["original_locations"]
                or any(
                    not entry["in_retention"] or entry["in_live_source"]
                    for entry in proof["original_locations"]
                )
            ):
                raise RuntimeError(
                    "Unknown continuation requires every original retained exactly once"
                )
            if (
                proof["actual_state"] == "EMPTY_CURRENT_DATABASE_GUARDED"
                and confirm_keep_partial is not True
            ):
                raise ValueError(
                    "Explicit consent to preserve the empty partial target required"
                )
        if _digest(copy_journal) != proof["journal_sha256"]:
            raise ValueError("Copy journal changed in native lease transition")
        leases.enter_context(_open_read_lock(copy_journal))
        if _digest(copy_journal) != proof["journal_sha256"]:
            raise ValueError("Copy journal changed before retained read lease")
        for folder in (
            canonical,
            canonical / "backups",
            canonical / "manifests",
            canonical / "runtime",
            installation,
        ):
            if (
                folder.resolve(strict=True) != folder
                or folder.is_symlink()
                or folder.is_junction()
                or not folder.is_dir()
            ):
                raise ValueError("Real continuation directories required")
            leases.enter_context(
                RuntimeHandle(folder, directory=True, allow_child_writes=True)
            )
        marker = canonical / "runtime/restore-in-progress.json"
        expected_guard = {
            "corrupt_restore_copy_format": 1,
            "copy_id": proof["copy_id"],
            "journal_name": copy_journal.name,
            "data_root_identity": proof["data_root_identity"],
            "preparation_sha256": proof["preparation_sha256"],
        }
        leases.enter_context(_open_read_lock(marker))
        guard = _record(marker)
        if (
            guard != expected_guard
            or type(guard.get("corrupt_restore_copy_format")) is not int
        ):
            raise ValueError("Continuation guard changed")
        if {path.name for path in (canonical / "runtime").iterdir()} != {
            "app-data.lock",
            marker.name,
        }:
            raise RuntimeError("Other runtime work requires recovery")
        preparation_path = canonical / copy["preparation_manifest"]
        preparation = leases.enter_context(
            hold_corrupt_preparation(
                canonical,
                preparation_path,
                proof["preparation_sha256"],
                timeout_seconds=remaining(),
                cancelled=cancelled,
            )
        )
        candidate_value = copy.get("candidate_directory")
        if not isinstance(candidate_value, str):
            raise ValueError("Explicit bound candidate required")
        candidate = Path(candidate_value)
        manifest = verify_candidate(candidate, installation)
        leases.enter_context(RuntimeHandle(candidate, directory=True))
        for name in ("release-manifest.json", *manifest["files"]):
            if _regular(candidate / name) is None:
                raise ValueError("Unaliased candidate files required")
            leases.enter_context(_open_read_lock(candidate / name))
        backup = canonical / "backups" / preparation["backup_id"]
        for name in ("backup-manifest.json", "creator_loop.sqlite3"):
            leases.enter_context(_open_read_lock(backup / name))
        snapshot, metadata, fresh_manifest = _restore_inputs(
            canonical, preparation["backup_id"], installation, candidate
        )
        if (
            metadata["schema_version"] != preparation["schema_from"]
            or _digest(snapshot) != preparation["backup_sha256"]
            or manifest_identity(fresh_manifest)
            != preparation["candidate_manifest_identity"]
            or manifest_identity(manifest) != preparation["candidate_manifest_identity"]
        ):
            raise ValueError("Continuation backup/candidate binding changed")
        registry = canonical / "manifests/storage-roots.json"
        if _regular(registry) is not None:
            leases.enter_context(_open_read_lock(registry))
        registry_sha = _digest(registry) if registry.exists() else None
        staged = preparation_path.parent / "restored.sqlite3"
        with closing(
            sqlite3.connect(staged.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as reader:
            reader.execute("PRAGMA foreign_keys=ON")
            media_before = _media_assessment(canonical, reader, deadline, leases)
        if (
            any(entry["status"] != "valid" for entry in media_before)
            and confirm_media_issues is not True
        ):
            raise ValueError("Explicit current media-issue acknowledgement required")
        if (
            shutil.disk_usage(canonical).free
            < preparation["staged_database_byte_size"] * 3 + 1024**2
        ):
            raise OSError("Insufficient continuation space")
        originals = canonical / "backups" / copy["retained_directory"]
        leases.enter_context(
            RuntimeHandle(originals, directory=True, allow_child_writes=True)
        )
        locations = {entry["name"]: entry for entry in proof["original_locations"]}
        current = {entry["name"]: entry for entry in proof["current_files"]}
        original_entries = copy["damage"]["raw_files"]
        remaining_originals = {}
        partials = {}
        partial_proofs = []
        expected_retained = set()
        for entry in original_entries:
            check()
            name = entry["name"]
            live = canonical / name
            observed = current[name]
            if observed["present"]:
                pin = leases.enter_context(hold_existing_corrupt_source(live))
                if _hash(pin.stream, check) != (
                    observed["sha256"],
                    observed["byte_size"],
                ):
                    raise RuntimeError(
                        "Live bytes changed in exclusive lease transition"
                    )
                if entry["present"] and locations[name]["in_live_source"]:
                    remaining_originals[name] = pin
                elif unknown or (
                    name == "creator_loop.sqlite3"
                    and proof["actual_state"] == "EMPTY_CURRENT_DATABASE_GUARDED"
                ):
                    partials[name] = pin
                    partial_proofs.append(dict(observed))
                else:
                    raise RuntimeError("Foreign live input appeared; retain all bytes")
            elif _regular(live) is not None:
                raise RuntimeError("Previously absent live file appeared")
            if entry["present"] and locations[name]["in_retention"]:
                old = originals / name
                stream = leases.enter_context(_open_read_lock(old))
                if _hash(stream, check) != (entry["sha256"], entry["byte_size"]):
                    raise RuntimeError("Already retained original bytes changed")
                expected_retained.add(name)
        if {path.name for path in originals.iterdir()} != expected_retained:
            raise RuntimeError("Retained original inventory changed")
        resume_id = uuid4().hex
        journal = canonical / "manifests" / f"corrupt-resume-{resume_id}.json"
        partial_directory = canonical / "backups" / f"corrupt-original-{resume_id}"
        record: dict[str, Any] = {
            "corrupt_resume_format": 1,
            "resume_id": resume_id,
            "copy_id": proof["copy_id"],
            "copy_journal_name": copy_journal.name,
            "copy_journal_sha256": proof["journal_sha256"],
            "data_root_identity": proof["data_root_identity"],
            "preparation_sha256": proof["preparation_sha256"],
            "reviewed_inspection": reviewed_inspection,
            "confirmed_lost_changes": True,
            "confirmed_keep_partial": confirm_keep_partial is True,
            "confirmed_preserve_unknown": confirm_preserve_unknown is True,
            "confirmed_media_issues": confirm_media_issues is True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "RESUME_INPUTS_PINNED",
            "activation_pending": True,
            "restored": False,
            "consistent_partial_backup": False,
            "retained_partial_directory": partial_directory.name if partials else None,
            "retained_partial_files": partial_proofs,
        }
        _journal(journal, record)
        check()
        if partials:
            partial_directory.mkdir()
            leases.enter_context(
                RuntimeHandle(
                    partial_directory, directory=True, allow_child_writes=True
                )
            )
            for name, partial in partials.items():
                check()
                partial.retain(partial_directory)
                record.update(
                    phase="UNKNOWN_PARTIAL_RETENTION_CONTINUED"
                    if unknown
                    else "EMPTY_PARTIAL_RETAINED",
                    last_partial_name=name,
                )
                _journal(journal, record)
        for name, pin in remaining_originals.items():
            check()
            pin.retain(originals)
            record.update(phase="ORIGINAL_RETENTION_CONTINUED", last_retained_name=name)
            _journal(journal, record)
        if {path.name for path in originals.iterdir()} != {
            entry["name"] for entry in original_entries if entry["present"]
        } or any(_regular(canonical / name) is not None for name in _NAMES):
            raise RuntimeError("Live/retained inventories changed before new target")
        check()
        source = canonical / "creator_loop.sqlite3"
        target_pin = leases.enter_context(hold_new_sqlite_target(source))
        record["phase"] = "RESUME_COPY_STARTED"
        _journal(journal, record)
        with (
            closing(
                sqlite3.connect(staged.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as reader,
            closing(
                sqlite3.connect(
                    source.as_uri() + "?mode=rw", uri=True, timeout=min(10, remaining())
                )
            ) as target,
        ):
            target.execute("PRAGMA foreign_keys=ON")
            target.set_progress_handler(
                lambda: int(
                    time.monotonic() >= deadline or bool(cancelled and cancelled())
                ),
                1000,
            )

            def progress(status: int, remaining_pages: int, total: int) -> None:
                check()

            reader.backup(target, pages=256, progress=progress, sleep=0.01)
            validate(target)
            actual, counts = _database_identity(target, deadline)
            if (
                actual != preparation["staged_database_identity"]
                or counts != preparation["staged_counts"]
                or _media_assessment(canonical, target, deadline, leases)
                != media_before
            ):
                raise RuntimeError("Continued copy differs from validated stage/media")
        target_pin.flush()
        os.fsync(target_pin.fileno())
        digest, size = _hash(target_pin, check)
        if partials:
            if {path.name for path in partial_directory.iterdir()} != set(partials):
                raise RuntimeError("Partial retention inventory changed before receipt")
            for partial in partials.values():
                partial.verify()
                if _hash(partial.stream, check) != (
                    current[partial.source.name]["sha256"],
                    current[partial.source.name]["byte_size"],
                ):
                    raise RuntimeError("Retained partial bytes changed before receipt")
        if (
            {path.name for path in originals.iterdir()}
            != {entry["name"] for entry in original_entries if entry["present"]}
            or {path.name for path in (canonical / "runtime").iterdir()}
            != {"app-data.lock", marker.name}
            or _digest(copy_journal) != proof["journal_sha256"]
            or _record(marker) != expected_guard
            or any(
                _regular(canonical / name) is not None
                for name in _NAMES
                if name != source.name
            )
            or (_digest(registry) if registry.exists() else None) != registry_sha
            or manifest_identity(verify_candidate(candidate, installation))
            != preparation["candidate_manifest_identity"]
        ):
            raise RuntimeError("Continuation inputs changed before verified receipt")
        record.update(
            phase="RESUME_DB_COMMITTED_GUARDED",
            copied_database_sha256=digest,
            copied_database_byte_size=size,
        )
        _journal(journal, record)
        return journal
