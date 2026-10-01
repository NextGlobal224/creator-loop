"""Store exact TEXT original bytes before registering their Library identity."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.database import _connect_write
from creator_loop.library import Asset, LibraryRepository
from creator_loop.originals import READ_CHUNK_SIZE
from creator_loop.paths import data_root
from creator_loop.windows_owned_file import OwnedWindowsFile


@dataclass(frozen=True)
class ImportedTextOriginal:
    asset_id: str
    file_id: str
    storage_key: str


def _new_id() -> str:
    return uuid4().hex


def _stored_digest_and_size(owned: OwnedWindowsFile) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    owned.stream.seek(0)
    for chunk in iter(lambda: owned.stream.read(READ_CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _registration_exists(db_path: Path, file_id: str) -> bool:
    """Fail closed if a failed commit may nevertheless have published the row."""
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as check:
            return (
                check.execute(
                    "SELECT 1 FROM asset_files WHERE file_id=?", (file_id,)
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return True


def intake_text_original(
    source_path: Path, *, root: Path | None = None
) -> ImportedTextOriginal:
    """Copy a text file byte-for-byte, then commit its Asset and ORIGINAL row."""
    source_path = Path(source_path)
    original_name = source_path.name
    if not original_name or any(
        char in original_name
        for char in (chr(0), "\\", ":", "<", ">", '"', "|", "?", "*")
    ):
        raise ValueError("Source filename cannot be stored as an original")

    root = (data_root() if root is None else Path(root)).resolve()
    originals_root = root / "storage" / "originals"
    for parent in (root / "storage", originals_root):
        if parent.exists() and not parent.resolve().is_relative_to(root):
            raise ValueError("Originals storage escapes the data root")
    originals_root.mkdir(parents=True, exist_ok=True)
    if not originals_root.resolve().is_relative_to(root):
        raise ValueError("Originals storage escapes the data root")

    asset_id = _new_id()
    file_id = _new_id()
    stored_name = f"{file_id}-{original_name}"
    destination = originals_root / stored_name
    storage_key = f"storage/originals/{stored_name}"
    db_path = root / "creator_loop.sqlite3"
    owned: OwnedWindowsFile | None = None
    committed = False
    commit_started = False

    try:
        with source_path.open("rb") as source:
            owned = OwnedWindowsFile.create_new(destination)
            for chunk in iter(lambda: source.read(READ_CHUNK_SIZE), b""):
                remaining = memoryview(chunk)
                while remaining:
                    written = owned.stream.write(remaining)
                    if type(written) is not int or not 0 < written <= len(remaining):
                        raise OSError("Stored original write made no forward progress")
                    remaining = remaining[written:]
            owned.stream.flush()
            os.fsync(owned.stream.fileno())

        digest, size = _stored_digest_and_size(owned)
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        db = _connect_write(db_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            repo = LibraryRepository(db)
            repo.create_asset(Asset(asset_id, "TEXT", original_name, timestamp, None))
            repo.create_asset_file(
                asset_id=asset_id,
                file_id=file_id,
                role="ORIGINAL",
                storage_key=storage_key,
                sha256=digest,
                byte_size=size,
                mime_type="text/plain",
                parent_file_id=None,
                processing_run_id=None,
                created_at=timestamp,
                commit=False,
            )
            recorded = db.execute(
                "SELECT role, storage_key, byte_size, sha256 "
                "FROM asset_files WHERE file_id=?",
                (file_id,),
            ).fetchone()
            if recorded != ("ORIGINAL", storage_key, size, digest):
                raise RuntimeError(
                    "Pending original registration does not match stored bytes"
                )
            commit_started = True
            db.commit()
            committed = True
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    except BaseException as registration_error:
        if owned is not None and not committed:
            if not commit_started or not _registration_exists(db_path, file_id):
                try:
                    owned.discard()
                except BaseException as cleanup_error:
                    raise cleanup_error from registration_error
        raise
    finally:
        if owned is not None:
            owned.close()

    return ImportedTextOriginal(asset_id, file_id, storage_key)
