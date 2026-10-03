"""Versioned SQLite bootstrap and read-only health probes."""

import hashlib
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

MIGRATIONS = (
    "0001_initial",
    "0002_claim_citation_seal",
    "0003_draft_snapshot_seal",
    "0004_selection_snapshot_seal",
)
SCHEMA_VERSION = len(MIGRATIONS)


def _connect_write(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def migration_path(migration_id: str = MIGRATIONS[0]) -> Path:
    return (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        / "migrations"
        / f"{migration_id}.sql"
    )


def _migration_sql(migration_id: str) -> tuple[str, str]:
    sql = migration_path(migration_id).read_text(encoding="utf-8")
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _execute_migration(db: sqlite3.Connection, sql: str) -> None:
    # executescript() commits an open transaction; keep all version steps atomic.
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete migration SQL")


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = _connect_write(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported schema version {version}; expected {SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            validate(db)
            return
        data_version: int | None = None
        if version > 0:
            validate(db, expected_version=version)
            data_version = db.execute("PRAGMA data_version").fetchone()[0]
            snapshot = path.with_name(
                f"{path.name}.pre-v{SCHEMA_VERSION}-{uuid4().hex}.sqlite3"
            )
            backup(path, snapshot, expected_version=version)
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] != version:
                raise RuntimeError("Schema changed while preparing migration")
            if data_version is not None:
                if db.execute("PRAGMA data_version").fetchone()[0] != data_version:
                    raise RuntimeError(
                        "Database changed while preparing migration backup"
                    )
                validate(db, expected_version=version)
            for step in range(version + 1, SCHEMA_VERSION + 1):
                migration_id = MIGRATIONS[step - 1]
                sql, digest = _migration_sql(migration_id)
                _execute_migration(db, sql)
                db.execute(
                    """INSERT INTO schema_migrations(id,checksum,applied_at,app_version)
                       VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)""",
                    (migration_id, digest, "0.1.0"),
                )
                db.execute(f"PRAGMA user_version={step}")
            validate(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.close()


def validate(db: sqlite3.Connection, *, expected_version: int = SCHEMA_VERSION) -> None:
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys are disabled")
    if db.execute("PRAGMA user_version").fetchone()[0] != expected_version:
        raise RuntimeError("Schema version mismatch")
    expected = {
        migration_id: _migration_sql(migration_id)[1]
        for migration_id in MIGRATIONS[:expected_version]
    }
    actual = dict(db.execute("SELECT id,checksum FROM schema_migrations"))
    if actual != expected:
        raise RuntimeError("Migration checksum mismatch")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("SQLite foreign key check failed")


def backup(
    source: Path, destination: Path, *, expected_version: int = SCHEMA_VERSION
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    src = _connect_write(source)
    dst = sqlite3.connect(destination)
    try:
        dst.execute("PRAGMA foreign_keys=ON")
        validate(src, expected_version=expected_version)
        src.backup(dst)
        validate(dst, expected_version=expected_version)
    finally:
        dst.close()
        src.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA foreign_keys=ON")
    return db
