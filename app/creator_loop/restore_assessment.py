"""Read-only backup/media assessment for an explicit future DB restore."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
from creator_loop.installation_stage import _installation_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.storage_paths import StoragePathError, resolve_storage_path
from creator_loop.storage_roots import _load
from creator_loop.update_activation import (
    _read_record,
    verify_candidate,
    verify_prepared_backup,
)
from creator_loop.update_backup import _digest
from creator_loop.update_preparation import (
    manifest_identity,
    validate_candidate_migrations,
)

LOSS_WARNING = "Restoring this DB backup loses every database change after its backup time. Media is not restored; newer files are kept."


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Restore assessment exceeded its work budget")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _database_identity(
    db: sqlite3.Connection, deadline: float
) -> tuple[str, dict[str, int]]:
    """Stream a logical fingerprint; include WAL-visible rows without DB-sized RAM.

    Never emit row contents. PK/rowid ordering and typed length framing avoid
    ambiguity. A later restore must recheck this proof under its own locks.
    """
    digest = hashlib.sha256()
    schema = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    digest.update(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    counts: dict[str, int] = {}
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        _check_deadline(deadline)
        digest.update(name.encode("utf-8") + b"\0")
        info = db.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        order = ",".join(_quote(column) for column in primary) or "rowid"
        count = 0
        for row in db.execute(f"SELECT * FROM {_quote(name)} ORDER BY {order}"):
            _check_deadline(deadline)
            count += 1
            digest.update(b"row\0")
            for value in row:
                if value is None:
                    encoded, tag = b"", b"null"
                elif isinstance(value, bytes):
                    encoded, tag = value, b"bytes"
                elif isinstance(value, float):
                    encoded, tag = value.hex().encode("ascii"), b"float"
                else:
                    encoded, tag = (
                        str(value).encode("utf-8"),
                        type(value).__name__.encode("ascii"),
                    )
                digest.update(
                    tag
                    + b":"
                    + str(len(encoded)).encode("ascii")
                    + b":"
                    + encoded
                    + b"\0"
                )
        counts[name] = count
    return digest.hexdigest(), counts


def assessment_identity(assessment: dict[str, Any]) -> str:
    """Bind review to state, not wall-clock timing; this is not a signature."""
    proof = {
        key: value
        for key, value in assessment.items()
        if key not in ("assessed_at", "assessment_identity")
    }
    return hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def assess_restore(
    root: Path,
    backup_id: str,
    installation_root: Path,
    candidate_directory: Path,
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Assess an explicit backup/candidate without restoring or changing a journal.

    This is evidence for user review, not authorization to discard changes.
    Required lost-change confirmation and fresh proof/media rechecks belong to
    the apply operation. Missing/changed media is reported, never silently fixed.
    """
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 600
    ):
        raise ValueError("Assessment work budget must be positive and at most 600s")
    if re.fullmatch(r"[0-9a-f]{32}", backup_id) is None:
        raise ValueError("Choose an explicit published backup ID")
    deadline = time.monotonic() + timeout_seconds
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    for name in ("manifests", "runtime", "backups"):
        folder = canonical / name
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Real restore coordination directories required")
    source = canonical / "creator_loop.sqlite3"
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("A real current database is required for this assessment")
    with (
        AppDataLock(canonical),
        closing(
            sqlite3.connect(
                source.as_uri() + "?mode=rw", uri=True, timeout=min(10, timeout_seconds)
            )
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= SCHEMA_VERSION:
                raise RuntimeError(
                    "Use recovery compatible with the actual current schema"
                )
            validate(db, expected_version=version)
            if (
                any(
                    path.name != "app-data.lock"
                    for path in (canonical / "runtime").iterdir()
                )
                or db.execute(
                    "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                ).fetchone()
            ):
                raise RuntimeError(
                    "Owned workers require recovery before restore assessment"
                )
            backup_folder = canonical / "backups" / backup_id
            if (
                backup_folder.is_symlink()
                or backup_folder.is_junction()
                or backup_folder.resolve(strict=True).parent != canonical / "backups"
            ):
                raise ValueError("Select a real published backup in this data root")
            metadata = _read_record(backup_folder / "backup-manifest.json")
            backup_version = metadata.get("schema_version")
            snapshot = verify_prepared_backup(
                canonical, {"backup_id": backup_id, "schema_from": backup_version}
            )
            if (
                metadata.get("backup_format") != 1
                or metadata.get("media_included") is not False
            ):
                raise ValueError("A supported DB-only backup is required")
            created_at = metadata.get("created_at")
            if (
                not isinstance(created_at, str)
                or datetime.fromisoformat(created_at).utcoffset() is None
            ):
                raise ValueError("Backup needs an explicit zoned creation time")
            manifest = verify_candidate(candidate_directory, installation)
            validate_candidate_migrations(manifest, candidate_directory)
            if (
                not manifest["schema_from"] <= backup_version <= manifest["schema_to"]
                or not manifest["schema_read_min"]
                <= SCHEMA_VERSION
                <= manifest["schema_read_max"]
            ):
                raise ValueError("Candidate cannot migrate/read the restored backup")
            registry_path = canonical / "manifests/storage-roots.json"
            registry = _load(registry_path) if registry_path.exists() else None
            current_identity, current_counts = _database_identity(db, deadline)
            current_files = {
                row[0] for row in db.execute("SELECT file_id FROM asset_files")
            }
            media: list[dict[str, Any]] = []
            with closing(open_readonly(snapshot.resolve(strict=True))) as copy:
                copy.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 10000
                )
                _, backup_counts = _database_identity(copy, deadline)
                for file_id, role, key, expected_size, expected_digest in copy.execute(
                    "SELECT file_id,role,storage_key,byte_size,sha256 FROM asset_files ORDER BY file_id"
                ):
                    _check_deadline(deadline)
                    entry: dict[str, Any] = {
                        "file_id": file_id,
                        "role": role,
                        "expected_size": expected_size,
                        "expected_sha256": expected_digest,
                        "status": "valid",
                    }
                    try:
                        path = resolve_storage_path(canonical, role, key)
                        with _open_read_lock(path) as stream:
                            actual = hashlib.sha256()
                            size = 0
                            while chunk := stream.read(1024**2):
                                _check_deadline(deadline)
                                size += len(chunk)
                                actual.update(chunk)
                            entry.update(
                                actual_size=size, actual_sha256=actual.hexdigest()
                            )
                            if size != expected_size:
                                entry["status"] = "size_mismatch"
                            elif actual.hexdigest() != expected_digest:
                                entry["status"] = "digest_mismatch"
                    except StoragePathError as exc:
                        entry["status"] = exc.reason
                    except OSError as exc:
                        entry.update(
                            status="unavailable_file", error_type=type(exc).__name__
                        )
                    media.append(entry)
            backup_files = {entry["file_id"] for entry in media}
            assessment: dict[str, Any] = {
                "restore_assessment_format": 1,
                "assessed_at": datetime.now(timezone.utc).isoformat(),
                "data_root_identity": hashlib.sha256(
                    str(canonical).encode("utf-8")
                ).hexdigest(),
                "data_root_id": registry.get("data_root_id") if registry else None,
                "registry_sha256": _digest(registry_path) if registry else None,
                "current_schema": version,
                "current_database_identity": current_identity,
                "current_counts": current_counts,
                "backup_id": backup_id,
                "backup_created_at": created_at,
                "backup_schema": backup_version,
                "backup_sha256": metadata["database_sha256"],
                "backup_counts": backup_counts,
                "target_schema": SCHEMA_VERSION,
                "candidate_name": candidate_directory.name,
                "candidate_manifest_identity": manifest_identity(manifest),
                "media_included": False,
                "media_assessment": media,
                "media_issue_count": sum(entry["status"] != "valid" for entry in media),
                "current_file_references_not_in_backup": len(
                    current_files - backup_files
                ),
                "backup_file_references_not_in_current": len(
                    backup_files - current_files
                ),
                "loss_warning": LOSS_WARNING,
                "requires_lost_changes_confirmation": True,
                "requires_media_issues_confirmation": any(
                    entry["status"] != "valid" for entry in media
                ),
                "restored": False,
            }
            _check_deadline(deadline)
            assessment["assessment_identity"] = assessment_identity(assessment)
            return assessment
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Restore assessment exceeded its work budget"
                ) from exc
            raise
        finally:
            db.set_progress_handler(None, 0)
            db.rollback()
