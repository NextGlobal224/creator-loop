"""Explicit guarded Windows replacement, retaining damaged files and all backups."""

from __future__ import annotations

import hashlib
import json
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
from typing import Any, BinaryIO
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_database import _NAMES, _hold_corrupt_source_locked, _regular
from creator_loop.corrupt_restore_assessment import _assess_corrupt_locked
from creator_loop.corrupt_source_preservation import _create_file, _publish_new_manifest
from creator_loop.corrupt_source_retention import hold_existing_corrupt_source
from creator_loop.corrupt_stage_validation import hold_corrupt_preparation
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import _database_identity, _media_assessment
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.sqlite_stage_file import hold_new_sqlite_target
from creator_loop.update_activation import verify_candidate
from creator_loop.update_preparation import _journal, manifest_identity


def _write_new_record(
    handles: ExitStack, path: Path, record: dict[str, Any]
) -> BinaryIO:
    """Exclusive durable record, never overwrite a preexisting coordination file."""
    stream = _create_file(handles, path)
    body = json.dumps(record, sort_keys=True, indent=2).encode("utf-8")
    if stream.write(body) != len(body):
        raise OSError("Incomplete guarded restore record; preserve evidence")
    stream.flush()
    os.fsync(stream.fileno())
    stream.seek(0)
    if stream.read(len(body) + 1) != body:
        raise RuntimeError("Guarded restore record readback failed")
    return stream


def _hash(stream: BinaryIO, check: Callable[[], None]) -> tuple[str, int]:
    stream.seek(0)
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024**2):
        check()
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def copy_corrupt_restore(
    root: Path,
    preparation_manifest: Path,
    installation_root: Path,
    candidate_directory: Path,
    *,
    reviewed_preparation: str,
    reviewed_identity: str,
    confirm_lost_changes: bool = False,
    confirm_media_issues: bool = False,
    timeout_seconds: float = 180,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Fresh explicit consent, durable guard, native source retention, BackupAPI.

    Return a DB_COMMITTED_GUARDED journal, never a completed restore. Normal
    launch remains blocked until explicit actual-state recovery/fresh runtime
    health clears the guard. Keep original inodes, raw archive, consistent
    backup, separate stage, prior installations/media and partial evidence.
    Caller must use an outer process timeout for blocking native calls.
    """
    if sys.platform != "win32":
        raise OSError("Guarded corrupt replacement requires native Windows ownership")
    if (
        confirm_lost_changes is not True
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_identity) is None
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_preparation) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError(
            "Explicit fresh review/preparation/loss consent and bounded budget required"
        )
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled and cancelled():
            raise InterruptedError(
                "Guarded corrupt restore cancelled; keep guard/evidence"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("Guarded corrupt restore exceeded its work budget")

    canonical = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(canonical)):
        raise ValueError("Canonical data root required")
    installation = _installation_root(installation_root, canonical)
    for name in ("runtime", "manifests", "backups", "logs"):
        directory = canonical / name
        if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
            raise ValueError("Real guarded restore coordination directories required")
    with AppDataLock(canonical), ExitStack() as handles:
        require_no_pending_restore(canonical)
        for directory in (
            canonical,
            canonical / "backups",
            canonical / "manifests",
            canonical / "runtime",
            installation,
        ):
            handles.enter_context(
                RuntimeHandle(directory, directory=True, allow_child_writes=True)
            )
        preparation = handles.enter_context(
            hold_corrupt_preparation(
                canonical,
                preparation_manifest,
                reviewed_preparation,
                timeout_seconds=timeout_seconds,
                cancelled=cancelled,
            )
        )
        with _hold_corrupt_source_locked(
            canonical, timeout_seconds=timeout_seconds, cancelled=cancelled
        ) as damage:
            check()
            if damage["damage_identity"] != preparation["damage_identity"]:
                raise ValueError("Damaged source differs from retained preparation")
            assessment = _assess_corrupt_locked(
                canonical,
                preparation["backup_id"],
                installation,
                candidate_directory,
                damage,
                deadline,
                handles,
            )
            if (
                assessment["assessment_identity"] != reviewed_identity
                or reviewed_identity != preparation["reviewed_assessment_identity"]
            ):
                raise ValueError(
                    "Fresh corrupt restore review differs; inspect and prepare again"
                )
            if assessment["media_issue_count"] and confirm_media_issues is not True:
                raise ValueError(
                    "Explicit acknowledgement of current media issues required"
                )
            candidate = candidate_directory.resolve(strict=True)
            manifest = verify_candidate(candidate, installation)
            if (
                manifest_identity(manifest)
                != preparation["candidate_manifest_identity"]
            ):
                raise ValueError("Prepared candidate changed")
            handles.enter_context(RuntimeHandle(candidate, directory=True))
            for name in ("release-manifest.json", *manifest["files"]):
                path = candidate / name
                if _regular(path) is None:
                    raise ValueError("Required candidate file missing")
                handles.enter_context(_open_read_lock(path))
            if (
                manifest_identity(verify_candidate(candidate, installation))
                != preparation["candidate_manifest_identity"]
            ):
                raise ValueError("Candidate changed before retained input leases")
            metadata = (
                canonical
                / "backups"
                / preparation["backup_id"]
                / "backup-manifest.json"
            )
            handles.enter_context(_open_read_lock(metadata))
            if (
                shutil.disk_usage(canonical).free
                < preparation["staged_database_byte_size"] * 3 + 1024**2
            ):
                raise OSError("Insufficient space for guarded replacement")
        # Public read-only source leases end here, while the SAME app lock and
        # archive/stage/backup/media/candidate/root handles remain retained.
        check()
        originals = {}
        entries = damage["raw_files"]
        for entry in entries:
            check()
            path = canonical / entry["name"]
            if not entry["present"]:
                if _regular(path) is not None:
                    raise RuntimeError("New source sidecar appeared in lease gap")
                continue
            pin = handles.enter_context(hold_existing_corrupt_source(path))
            if _hash(pin.stream, check) != (entry["sha256"], entry["byte_size"]):
                raise RuntimeError(
                    "Source bytes changed during exclusive lease transition"
                )
            pin.verify()
            originals[entry["name"]] = pin
        copy_id = uuid4().hex
        retained = canonical / "backups" / f"corrupt-original-{copy_id}"
        retained.mkdir()
        handles.enter_context(
            RuntimeHandle(retained, directory=True, allow_child_writes=True)
        )
        journal = canonical / "manifests" / f"corrupt-restore-{copy_id}.json"
        record: dict[str, Any] = {
            "corrupt_restore_copy_format": 1,
            "copy_id": copy_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "CORRUPT_COPY_GUARD_PENDING",
            "data_root_identity": damage["data_root_identity"],
            "damage": damage,
            "preparation_manifest": str(
                preparation_manifest.resolve(strict=True).relative_to(canonical)
            ),
            "preparation_sha256": reviewed_preparation,
            "reviewed_identity": reviewed_identity,
            "confirmed_lost_changes": True,
            "confirmed_media_issues": confirm_media_issues is True,
            "retained_directory": retained.name,
            "candidate_directory": str(candidate),
            "candidate_manifest_identity": preparation["candidate_manifest_identity"],
            "backup_id": preparation["backup_id"],
            "schema_from": preparation["schema_from"],
            "schema_to": SCHEMA_VERSION,
            "restored_database_identity": preparation["staged_database_identity"],
            "activation_pending": True,
            "restored": False,
        }
        with ExitStack() as publication:
            temporary = journal.with_suffix(".json.pending")
            stream = _write_new_record(publication, temporary, record)
            # Native non-overwrite publication before any source mutation.
            _publish_new_manifest(temporary, journal, stream)
        check()
        marker = canonical / "runtime/restore-in-progress.json"
        _write_new_record(
            handles,
            marker,
            {
                "corrupt_restore_copy_format": 1,
                "copy_id": copy_id,
                "journal_name": journal.name,
                "data_root_identity": damage["data_root_identity"],
                "preparation_sha256": reviewed_preparation,
            },
        )
        # Every error/crash after guard creation keeps the guard. A journal
        # phase never proves which rename/copy reached durable storage.
        record["phase"] = "CORRUPT_COPY_GUARDED"
        _journal(journal, record)
        for entry in entries:
            check()
            if not entry["present"] and _regular(canonical / entry["name"]) is not None:
                raise RuntimeError(
                    "New source sidecar appeared before guarded retention"
                )
        for name, pin in originals.items():
            check()
            pin.retain(retained)
            record["phase"] = "CORRUPT_SOURCE_RETENTION_STARTED"
            record["last_retained_name"] = name
            _journal(journal, record)
        if {path.name for path in retained.iterdir()} != set(originals) or any(
            _regular(canonical / name) is not None for name in _NAMES
        ):
            raise RuntimeError("Source inventory differs after guarded retention")
        record["phase"] = "CORRUPT_SOURCE_RETAINED"
        _journal(journal, record)
        check()
        source = canonical / "creator_loop.sqlite3"
        target_pin = handles.enter_context(hold_new_sqlite_target(source))
        stage = preparation_manifest.parent / "restored.sqlite3"
        record["phase"] = "CORRUPT_COPY_STARTED"
        _journal(journal, record)
        with (
            closing(
                sqlite3.connect(
                    stage.resolve(strict=True).as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
            ) as reader,
            closing(
                sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
            ) as target,
        ):
            target.execute("PRAGMA foreign_keys=ON")

            def progress(status: int, remaining: int, total: int) -> None:
                check()

            reader.backup(target, pages=256, progress=progress, sleep=0.01)
            target.set_progress_handler(
                lambda: int(
                    time.monotonic() >= deadline or bool(cancelled and cancelled())
                ),
                1000,
            )
            validate(target, expected_version=SCHEMA_VERSION)
            actual, counts = _database_identity(target, deadline)
            if (
                actual != preparation["staged_database_identity"]
                or counts != preparation["staged_counts"]
            ):
                raise RuntimeError("Copied database differs from validated preparation")
            if (
                _media_assessment(canonical, target, deadline, handles)
                != assessment["media_assessment"]
            ):
                raise RuntimeError(
                    "Media changed before guarded database copy completed"
                )
        check()
        target_pin.flush()
        os.fsync(target_pin.fileno())
        database_sha256, database_size = _hash(target_pin, check)
        if any(
            _regular(canonical / name) is not None
            for name in _NAMES
            if name != source.name
        ):
            raise RuntimeError(
                "Unexpected live sidecar after guarded copy; retain guard"
            )
        if (
            manifest_identity(verify_candidate(candidate, installation))
            != record["candidate_manifest_identity"]
        ):
            raise RuntimeError("Candidate changed after guarded database copy")
        for pin in originals.values():
            pin.verify()
        record.update(
            phase="CORRUPT_DB_COMMITTED_GUARDED",
            copied_database_sha256=database_sha256,
            copied_database_byte_size=database_size,
            copy_verified_at=datetime.now(timezone.utc).isoformat(),
        )
        _journal(journal, record)
        return journal
