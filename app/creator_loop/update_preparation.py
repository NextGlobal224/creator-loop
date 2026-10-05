"""Backup, staging and migration under one app/DB writer lock; no activation yet."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    _migrate_locked,
    _migration_sql,
)
from creator_loop.installation_stage import (
    load_release_manifest,
    preflight_installation,
    stage_installation,
)
from creator_loop.update_backup import _backup_from_guard, _inventory
from creator_loop.windows_paths import file_io_path


def _journal(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary_io = file_io_path(temporary)
    created = False
    try:
        with temporary_io.open("x", encoding="utf-8") as stream:
            created = True
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_io.replace(file_io_path(path))
    finally:
        if created:
            temporary_io.unlink(missing_ok=True)


def manifest_identity(manifest: dict[str, Any]) -> str:
    """Bind coordination to the supplied manifest, independent of whitespace."""
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_candidate_migrations(
    manifest: dict[str, Any], candidate: Path | None = None
) -> None:
    if (
        manifest["schema_to"] != SCHEMA_VERSION
        or manifest["migration_ids"] != list(MIGRATIONS)
        or manifest.get("migration_checksums")
        != {migration: _migration_sql(migration)[1] for migration in MIGRATIONS}
    ):
        raise ValueError("Candidate migrations do not match this updater")
    for migration in MIGRATIONS:
        name = f"CreatorLoop/_internal/migrations/{migration}.sql"
        if name not in manifest["files"]:
            raise ValueError("Candidate lacks packaged migration files")
        if candidate is not None:
            sql = file_io_path(candidate / name).read_text(encoding="utf-8")
            if (
                hashlib.sha256(sql.encode("utf-8")).hexdigest()
                != _migration_sql(migration)[1]
            ):
                raise RuntimeError("Candidate packaged migration checksum mismatch")


def prepare_update(
    root: Path, artifact: Path, manifest_path: Path, installation_root: Path
) -> Path:
    """Explicit update preparation; migrate only with this updater's known code.

    Keep backup, previous installations and journal on all failures. A failed
    uncommitted migration rolls back; a post-commit failure is recorded honestly
    and never 'fixed' by switching an executable or silently restoring the DB.
    """
    canonical = root.resolve(strict=True)
    manifest = load_release_manifest(manifest_path)
    validate_candidate_migrations(manifest)
    with AppDataLock(canonical):
        preflight_installation(installation_root, canonical, manifest)
        manifests = canonical / "manifests"
        if not manifests.is_dir() or manifests.is_symlink() or manifests.is_junction():
            raise OSError("A real update journal directory is required")
        source = canonical / "creator_loop.sqlite3"
        if source.is_symlink() or source.is_junction():
            raise OSError("A real source database is required")
        update_id = uuid4().hex
        journal = manifests / f"update-{update_id}.json"
        record: dict[str, Any] = {
            "update_format": 1,
            "update_id": update_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": manifest["git_commit"],
            "candidate_manifest_identity": manifest_identity(manifest),
            "schema_to": SCHEMA_VERSION,
            "phase": "STARTED",
            "migration_committed": False,
            "backup_id": None,
            "candidate_directory": None,
            "activation_pending": True,
        }
        _journal(journal, record)
        try:
            with closing(
                sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
            ) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                try:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if not manifest["schema_from"] <= version <= manifest["schema_to"]:
                        raise ValueError(
                            "Source schema is outside candidate upgrade range"
                        )
                    record["schema_from"] = version
                    snapshot = _backup_from_guard(canonical, db, 30)
                    record["backup_id"] = snapshot.name
                    record["phase"] = "BACKUP_VALIDATED"
                    _journal(journal, record)
                    before = _inventory(db, canonical)
                    candidate = stage_installation(
                        artifact,
                        manifest_path,
                        installation_root,
                        user_data_root=canonical,
                    )
                    record["candidate_directory"] = str(candidate)
                    if (
                        load_release_manifest(candidate / "release-manifest.json")
                        != manifest
                    ):
                        raise RuntimeError(
                            "Candidate manifest changed during preparation"
                        )
                    validate_candidate_migrations(manifest, candidate)
                    record["phase"] = "INSTALLATION_STAGED"
                    _journal(journal, record)
                    _migrate_locked(db, version)
                    if _inventory(db, canonical) != before:
                        raise RuntimeError(
                            "Migration changed storage reference inventory"
                        )
                    record["phase"] = "MIGRATION_VALIDATED"
                    _journal(journal, record)
                    db.commit()
                    record["migration_committed"] = True
                except BaseException:
                    db.rollback()
                    raise
            record["phase"] = "PREPARED"
            record["prepared_at"] = datetime.now(timezone.utc).isoformat()
            _journal(journal, record)
            return journal
        except BaseException as exc:
            record["phase"] = (
                "FAILED_POST_COMMIT"
                if record["migration_committed"]
                else "FAILED_ROLLED_BACK"
            )
            record["error_type"] = type(exc).__name__
            _journal(journal, record)
            raise
