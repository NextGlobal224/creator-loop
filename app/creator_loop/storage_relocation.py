"""Copy verified media before atomically switching its recorded location."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from creator_loop.database import _connect_write, open_readonly
from creator_loop.originals import READ_CHUNK_SIZE
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.storage_roots import resolve_registered_root
from creator_loop.windows_owned_file import OwnedWindowsFile


def _digest(source: BinaryIO) -> tuple[str, int]:
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(READ_CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _may_be_referenced(db_path: Path, keys: list[str]) -> bool:
    try:
        with closing(open_readonly(db_path)) as db:
            return any(
                db.execute(
                    "SELECT 1 FROM asset_files WHERE storage_key=?", (key,)
                ).fetchone()
                is not None
                for key in keys
            )
    except (sqlite3.Error, OSError):
        # An ambiguous commit must never delete potentially published bytes.
        return True


def relocate_asset_files(
    data_root: Path, file_ids: list[str], target_root_id: str
) -> dict[str, str]:
    """Move recorded locations as one transaction, retaining all source files.

    Own a dedicated connection and its write lock throughout copy/verification.
    Other domain writers cannot publish changes while this snapshot is moved.
    Windows handles deny replacement of new files until commit or rollback.
    Never remove an existing source or target; failed copies discard only held
    files created by this invocation. A crash may leave unreferenced copies.
    """
    if not file_ids or len(set(file_ids)) != len(file_ids):
        raise ValueError("Select a nonempty set of distinct file IDs")
    canonical = Path(data_root).resolve(strict=True)
    db_path = canonical / "creator_loop.sqlite3"
    if not db_path.is_file():
        raise ValueError("User database must exist")
    owned: list[OwnedWindowsFile] = []
    keys: dict[str, str] = {}
    committed = False
    commit_started = False
    try:
        with closing(_connect_write(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                target = resolve_registered_root(canonical, target_root_id)
                for file_id in file_ids:
                    row = db.execute(
                        "SELECT role,storage_key,sha256,byte_size FROM asset_files WHERE file_id=?",
                        (file_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("Asset file is missing")
                    role, old_key, digest, size = row
                    source = resolve_storage_path(canonical, role, old_key)
                    folder_name = "originals" if role == "ORIGINAL" else "derived"
                    folder = target / folder_name
                    if not folder.resolve().is_relative_to(target):
                        raise ValueError("Relocation target escapes registered root")
                    folder.mkdir(exist_ok=True)
                    name = f"{uuid4().hex}-{source.name}"
                    key = f"registered/{target_root_id}/{folder_name}/{name}"
                    destination = resolve_storage_path(canonical, role, key)
                    with source.open("rb") as incoming:
                        outgoing = OwnedWindowsFile.create_new(destination)
                        owned.append(outgoing)
                        keys[file_id] = key
                        for chunk in iter(lambda: incoming.read(READ_CHUNK_SIZE), b""):
                            remaining = memoryview(chunk)
                            while remaining:
                                written = outgoing.stream.write(remaining)
                                if type(written) is not int or not 0 < written <= len(
                                    remaining
                                ):
                                    raise OSError("Relocation write made no progress")
                                remaining = remaining[written:]
                        outgoing.stream.flush()
                        os.fsync(outgoing.stream.fileno())
                        if _digest(incoming) != (digest, size):
                            raise ValueError(
                                "Source bytes differ from recorded digest/size"
                            )
                        if _digest(outgoing.stream) != (digest, size):
                            raise ValueError(
                                "Copied bytes differ from recorded digest/size"
                            )
                    if resolve_registered_root(canonical, target_root_id) != target:
                        raise ValueError("Relocation volume changed")
                    db.execute(
                        "UPDATE asset_files SET storage_key=? WHERE file_id=? AND storage_key=?",
                        (key, file_id, old_key),
                    )
                commit_started = True
                db.commit()
                committed = True
            except BaseException:
                db.rollback()
                raise
    except BaseException as error:
        if not committed and (
            not commit_started or not _may_be_referenced(db_path, list(keys.values()))
        ):
            for file in owned:
                try:
                    file.discard()
                except BaseException as cleanup_error:
                    error.add_note(f"Owned relocation cleanup failed: {cleanup_error}")
        raise
    finally:
        for file in owned:
            file.close()
    return keys
