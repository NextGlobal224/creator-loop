"""Store exact original bytes before registering their Library identity."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable
from uuid import uuid4

from creator_loop.database import _connect_write
from creator_loop.library import Asset, LibraryRepository
from creator_loop.media_preflight import MediaInfo
from creator_loop.originals import READ_CHUNK_SIZE
from creator_loop.paths import data_root
from creator_loop.storage_paths import new_storage_destination, resolve_storage_path
from creator_loop.windows_owned_file import OwnedWindowsFile


@dataclass(frozen=True)
class ImportedOriginal:
    asset_id: str
    file_id: str
    storage_key: str


ImportedTextOriginal = ImportedOriginal


def _new_id() -> str:
    return uuid4().hex


def _check_cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise InterruptedError("Media decoder cancelled by user")


def _stored_digest_and_size(
    owned: OwnedWindowsFile, cancel: Callable[[], bool] | None = None
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    owned.stream.seek(0)
    for chunk in iter(lambda: owned.stream.read(READ_CHUNK_SIZE), b""):
        _check_cancelled(cancel)
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


def _intake_original(
    source_path: Path,
    *,
    classify: Callable[[BinaryIO], tuple[str, str]],
    preflight: Callable[[BinaryIO, Path], MediaInfo] | None = None,
    root: Path | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ImportedOriginal:
    """Copy exact bytes, classify the held destination, then register it."""
    source_path = Path(source_path)
    original_name = source_path.name
    if not original_name or any(
        char in original_name
        for char in (chr(0), "\\", ":", "<", ">", '"', "|", "?", "*")
    ):
        raise ValueError("Source filename cannot be stored as an original")

    root = (data_root() if root is None else Path(root)).resolve()
    asset_id = _new_id()
    file_id = _new_id()
    stored_name = f"{file_id}-{original_name}"
    storage_key, destination = new_storage_destination(root, "ORIGINAL", stored_name)
    db_path = root / "creator_loop.sqlite3"
    owned: OwnedWindowsFile | None = None
    committed = False
    commit_started = False

    try:
        _check_cancelled(cancel)
        with source_path.open("rb") as source:
            owned = OwnedWindowsFile.create_new(destination)
            for chunk in iter(lambda: source.read(READ_CHUNK_SIZE), b""):
                _check_cancelled(cancel)
                remaining = memoryview(chunk)
                while remaining:
                    written = owned.stream.write(remaining)
                    if type(written) is not int or not 0 < written <= len(remaining):
                        raise OSError("Stored original write made no forward progress")
                    remaining = remaining[written:]
            owned.stream.flush()
            os.fsync(owned.stream.fileno())

        digest, size = _stored_digest_and_size(owned, cancel)
        owned.stream.seek(0)
        media_type, mime_type = classify(owned.stream)
        if media_type not in ("VIDEO", "IMAGE", "TEXT") or not mime_type:
            raise ValueError("Invalid original media classification")
        _check_cancelled(cancel)
        info = preflight(owned.stream, destination) if preflight is not None else None
        _check_cancelled(cancel)
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        db = _connect_write(db_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            if resolve_storage_path(root, "ORIGINAL", storage_key) != destination:
                raise ValueError(
                    "Original storage location changed before registration"
                )
            repo = LibraryRepository(db)
            repo.create_asset(
                Asset(asset_id, media_type, original_name, timestamp, None)
            )
            repo.create_asset_file(
                asset_id=asset_id,
                file_id=file_id,
                role="ORIGINAL",
                storage_key=storage_key,
                sha256=digest,
                byte_size=size,
                mime_type=mime_type,
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
            if info is not None:
                db.execute(
                    "UPDATE asset_files SET width_px=?,height_px=?,duration_ms=? WHERE file_id=?",
                    (info.width_px, info.height_px, info.duration_ms, file_id),
                )
            if recorded != ("ORIGINAL", storage_key, size, digest):
                raise RuntimeError(
                    "Pending original registration does not match stored bytes"
                )
            _check_cancelled(cancel)
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

    return ImportedOriginal(asset_id, file_id, storage_key)


def intake_text_original(
    source_path: Path, *, root: Path | None = None
) -> ImportedTextOriginal:
    """Copy a text file byte-for-byte, then commit its Asset and ORIGINAL row."""
    return _intake_original(
        source_path, classify=lambda _stored: ("TEXT", "text/plain"), root=root
    )
