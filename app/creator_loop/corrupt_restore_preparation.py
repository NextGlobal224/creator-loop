"""Prepare a consistent replacement separately; never replace the damaged DB."""

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
from typing import Any
from uuid import uuid4

from creator_loop.corrupt_database import _identity, _regular, hold_corrupt_database
from creator_loop.corrupt_restore_assessment import _assess_corrupt_locked
from creator_loop.corrupt_source_preservation import _create_file, _publish_new_manifest
from creator_loop.database import (
    SCHEMA_VERSION,
    _migrate_locked,
    open_readonly,
    validate,
)
from creator_loop.installation_stage import _installation_root
from creator_loop.preserved_source_validation import hold_preserved_source
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_apply import _refs
from creator_loop.restore_assessment import _database_identity, _media_assessment
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.sqlite_stage_file import hold_new_sqlite_target
from creator_loop.update_activation import verify_prepared_backup


def prepare_corrupt_restore(
    root: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    raw_manifest: Path,
    *,
    reviewed_identity: str,
    confirm_lost_changes: bool = False,
    confirm_media_issues: bool = False,
    timeout_seconds: float = 180,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Fresh review/consent, held raw archive and BackupAPI into a new pinned DB.

    Keep all source/sidecar/archive/backup/installation/media bytes on failure.
    Partial stage folders also remain. The manifest is preparation evidence,
    not permission to replace data later: apply must revalidate under its lock,
    journal before mutation and perform crash recovery and fresh runtime health.
    Use an outer process timeout for blocking filesystem/SQLite native calls.
    """
    if (
        confirm_lost_changes is not True
        or re.fullmatch(r"[0-9a-f]{64}", reviewed_identity) is None
        or re.fullmatch(r"[0-9a-f]{32}", backup_id) is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError(
            "Explicit review/loss consent and bounded work budget required"
        )
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Corrupt restore preparation cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Corrupt restore preparation exceeded its work budget")

    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    with (
        hold_corrupt_database(
            canonical, timeout_seconds=timeout_seconds, cancelled=cancelled
        ) as damage,
        ExitStack() as handles,
    ):
        archive = handles.enter_context(
            hold_preserved_source(
                canonical,
                raw_manifest,
                damage["damage_identity"],
                timeout_seconds=timeout_seconds,
                cancelled=cancelled,
            )
        )
        check()
        assessment = _assess_corrupt_locked(
            canonical,
            backup_id,
            installation,
            candidate_directory,
            damage,
            deadline,
            handles,
        )
        if assessment["assessment_identity"] != reviewed_identity:
            raise ValueError(
                "Corrupt restore review is stale; inspect and confirm again"
            )
        if assessment["media_issue_count"] and confirm_media_issues is not True:
            raise ValueError(
                "Explicit acknowledgement of assessed media issues required"
            )
        snapshot = verify_prepared_backup(
            canonical,
            {"backup_id": backup_id, "schema_from": assessment["backup_schema"]},
        )
        parent = canonical / "backups"
        if parent.is_symlink() or parent.is_junction() or not parent.is_dir():
            raise ValueError("Real corrupt restore stage parent required")
        check()
        if shutil.disk_usage(parent).free < snapshot.stat().st_size * 3 + 1024**2:
            raise OSError("Insufficient space for a staged validated restore")
        if sys.platform == "win32":
            handles.enter_context(
                RuntimeHandle(parent, directory=True, allow_child_writes=True)
            )
        restore_id = uuid4().hex
        folder = parent / f"corrupt-restore-{restore_id}"
        folder.mkdir()
        if sys.platform == "win32":
            handles.enter_context(
                RuntimeHandle(folder, directory=True, allow_child_writes=True)
            )
        staged = folder / "restored.sqlite3"
        pin = handles.enter_context(hold_new_sqlite_target(staged))
        with (
            closing(open_readonly(snapshot.resolve(strict=True))) as reader,
            closing(
                sqlite3.connect(staged.as_uri() + "?mode=rw", uri=True, timeout=10)
            ) as target,
        ):
            target.execute("PRAGMA foreign_keys=ON")

            # Native progress checks bound BackupAPI page batches; cancellation
            # also interrupts long migration/validation statements below.
            def progress(status: int, remaining: int, total: int) -> None:
                check()

            reader.backup(target, pages=256, progress=progress, sleep=0.01)
            # A closed stage must be self-contained, including when an operator
            # later opens it read-only. Never change the backup/source's mode.
            if target.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise RuntimeError("Staged database could not leave WAL mode")
            target.set_progress_handler(
                lambda: int(
                    time.monotonic() >= deadline or bool(cancelled and cancelled())
                ),
                1000,
            )
            validate(target, expected_version=assessment["backup_schema"])
            if target.execute(
                "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
            ).fetchone():
                raise RuntimeError(
                    "Backup processing needs explicit recovery before staging"
                )
            before_refs = _refs(target)
            target.execute("BEGIN IMMEDIATE")
            try:
                _migrate_locked(target, assessment["backup_schema"])
                if _refs(target) != before_refs:
                    raise RuntimeError(
                        "Corrupt restore migration changed media references"
                    )
                target.commit()
            except BaseException:
                target.rollback()
                raise
            validate(target, expected_version=SCHEMA_VERSION)
            database_identity, counts = _database_identity(target, deadline)
            media = _media_assessment(canonical, target, deadline, handles)
            if media != assessment["media_assessment"]:
                raise RuntimeError("Media changed during corrupt restore preparation")
        check()
        pin.flush()
        os.fsync(pin.fileno())
        pin.seek(0)
        digest, size = hashlib.sha256(), 0
        while chunk := pin.read(1024**2):
            check()
            digest.update(chunk)
            size += len(chunk)
        if {path.name for path in folder.iterdir()} != {staged.name}:
            raise RuntimeError("Unknown files in corrupt restore stage; keep evidence")
        # Recheck selected backup/candidate/registry/media before publication.
        fresh = _assess_corrupt_locked(
            canonical,
            backup_id,
            installation,
            candidate_directory,
            damage,
            deadline,
            handles,
        )
        if fresh["assessment_identity"] != reviewed_identity:
            raise RuntimeError("Restore inputs changed during staging")
        # The original damage lease pins native source files. Portable platforms
        # must also detect mutations made while BackupAPI/migrations ran.
        for entry in damage["raw_files"]:
            check()
            source = canonical / entry["name"]
            before = _regular(source)
            if (before is not None) != entry["present"]:
                raise RuntimeError("Damaged source inventory changed during staging")
            if before is None:
                continue
            with _open_read_lock(source) as held:
                source_digest, source_size = hashlib.sha256(), 0
                while chunk := held.read(1024**2):
                    check()
                    source_digest.update(chunk)
                    source_size += len(chunk)
                after = _regular(source)
                if (
                    source_size != entry["byte_size"]
                    or source_digest.hexdigest() != entry["sha256"]
                    or after is None
                    or _identity(before) != _identity(after)
                    or _identity(before) != _identity(os.fstat(held.fileno()))
                ):
                    raise RuntimeError("Damaged source bytes changed during staging")
        staged_stat = os.fstat(pin.fileno())
        record: dict[str, Any] = {
            "corrupt_restore_preparation_format": 1,
            "restore_id": restore_id,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "phase": "CORRUPT_RESTORE_STAGED",
            "data_root_identity": damage["data_root_identity"],
            "reviewed_assessment_identity": reviewed_identity,
            "damage_identity": damage["damage_identity"],
            "raw_manifest": str(
                raw_manifest.resolve(strict=True).relative_to(canonical)
            ),
            "raw_manifest_sha256": archive["archive_manifest_sha256"],
            "backup_id": backup_id,
            "backup_sha256": assessment["backup_sha256"],
            "schema_from": assessment["backup_schema"],
            "schema_to": SCHEMA_VERSION,
            "candidate_name": assessment["candidate_name"],
            "candidate_manifest_identity": assessment["candidate_manifest_identity"],
            "registry_sha256": assessment["registry_sha256"],
            "staged_database": staged.name,
            "staged_database_sha256": digest.hexdigest(),
            "staged_database_byte_size": size,
            "staged_database_identity": database_identity,
            "staged_counts": counts,
            "media_assessment": media,
            "confirmed_lost_changes": True,
            "confirmed_media_issues": confirm_media_issues is True,
            "current_changes_assessable": False,
            "media_included": False,
            "raw_source_preserved": True,
            "apply_authorized": False,
            "activated": False,
            "restored": False,
        }
        temporary = folder / "preparation.json.pending"
        manifest = folder / "preparation.json"
        stream = _create_file(handles, temporary)
        body = json.dumps(record, sort_keys=True, indent=2).encode("utf-8")
        if stream.write(body) != len(body):
            raise OSError("Incomplete preparation manifest; keep partial evidence")
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read(len(body) + 1) != body:
            raise RuntimeError("Preparation manifest readback failed")
        check()
        if {path.name for path in folder.iterdir()} != {staged.name, temporary.name}:
            raise RuntimeError("Corrupt restore folder changed before publication")
        _publish_new_manifest(temporary, manifest, stream)
        if _identity(os.fstat(pin.fileno())) != _identity(staged_stat):
            raise RuntimeError("Staged database changed during manifest publication")
        return manifest
