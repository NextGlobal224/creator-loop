"""Versioned SQLite bootstrap and read-only health probes."""

import hashlib
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1


def _connect_write(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def migration_path() -> Path:
    return (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        / "migrations"
        / "0001_initial.sql"
    )


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect_write(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        sql = migration_path().read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if version == SCHEMA_VERSION:
            row = db.execute(
                "SELECT checksum FROM schema_migrations WHERE id='0001_initial'"
            ).fetchone()
            if not row or row[0] != digest:
                raise RuntimeError("Migration checksum mismatch")
            validate(db)
            return
        if version != 0:
            raise RuntimeError(
                f"Unsupported schema version {version}; expected {SCHEMA_VERSION}"
            )
        # A fresh database only. Upgrade migrations will use the same versioned contract.
        db.executescript(
            "BEGIN IMMEDIATE;\n"
            + sql
            + "\n"
            + "INSERT INTO schema_migrations(id, checksum, applied_at, app_version) "
            + f"VALUES ('0001_initial', '{digest}', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '0.1.0');\n"
            + "PRAGMA user_version=1;\nCOMMIT;"
        )
        validate(db)


def validate(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("Schema version mismatch")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("SQLite foreign key check failed")


def backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _connect_write(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        validate(dst)


def open_readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA foreign_keys=ON")
    return db
