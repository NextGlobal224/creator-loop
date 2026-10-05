"""Produce an owned image thumbnail with independent processing provenance."""

from __future__ import annotations

import hashlib
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImageWriter

from creator_loop.database import _connect_write
from creator_loop.image_evidence import read_verified_image
from creator_loop.library import LibraryRepository
from creator_loop.storage_paths import new_storage_destination, resolve_storage_path
from creator_loop.text_intake import _registration_exists, _stored_digest_and_size
from creator_loop.windows_owned_file import OwnedWindowsFile


@dataclass(frozen=True)
class ImageThumbnail:
    file_id: str
    run_id: str
    storage_key: str


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def create_image_thumbnail(
    file_id: str, *, data_root: Path, max_edge: int = 512
) -> ImageThumbnail:
    """Scale a verified IMAGE original and register its file and run together."""
    if type(max_edge) is not int or not 1 <= max_edge <= 4096:
        raise ValueError("Thumbnail maximum edge is outside local budget")
    root = Path(data_root).resolve()
    db_path = root / "creator_loop.sqlite3"
    with closing(_connect_write(db_path)) as db:
        row = db.execute(
            """SELECT f.asset_id,a.media_type,f.role
               FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
               WHERE f.file_id=?""",
            (file_id,),
        ).fetchone()
        if row is None or row[1:] != ("IMAGE", "ORIGINAL"):
            raise ValueError("Thumbnail requires an IMAGE original")
        asset_id = str(row[0])
        run_id = uuid4().hex
        LibraryRepository(db).create_processing_run(
            run_id=run_id,
            asset_id=asset_id,
            input_file_id=file_id,
            task_type="IMAGE_THUMBNAIL",
            status="RUNNING",
            tool_name="PySide6 Qt image scaler",
            tool_version=pyside_version,
            model_name=None,
            model_version=None,
            started_at=_timestamp(),
            finished_at=None,
            error_code=None,
            error_message=None,
            created_at=_timestamp(),
        )
        new_file_id = uuid4().hex
        stored_name = f"{new_file_id}.png"
        owned: OwnedWindowsFile | None = None
        committed = False
        commit_started = False
        try:
            _asset_id, image = read_verified_image(db, file_id, root)
            edge = min(max_edge, max(image.width(), image.height()))
            scaled = image.scaled(
                edge,
                edge,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            encoded = QBuffer()
            encoded.open(QIODevice.OpenModeFlag.WriteOnly)
            if not QImageWriter(encoded, QByteArray(b"PNG")).write(scaled):
                raise ValueError("Thumbnail PNG encoding failed")
            png = encoded.data().data()
            storage_key, destination = new_storage_destination(
                root, "THUMBNAIL", stored_name
            )
            owned = OwnedWindowsFile.create_new(destination)
            remaining = memoryview(png)
            while remaining:
                written = owned.stream.write(remaining)
                if type(written) is not int or not 0 < written <= len(remaining):
                    raise OSError("Thumbnail write made no forward progress")
                remaining = remaining[written:]
            owned.stream.flush()
            os.fsync(owned.stream.fileno())
            digest, size = _stored_digest_and_size(owned)
            if size != len(png) or digest != hashlib.sha256(png).hexdigest():
                raise OSError("Stored thumbnail differs from encoded pixels")
            db.execute("BEGIN IMMEDIATE")
            if resolve_storage_path(root, "THUMBNAIL", storage_key) != destination:
                raise ValueError(
                    "Thumbnail storage location changed before registration"
                )
            db.execute(
                """INSERT INTO asset_files(
                     file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,
                     parent_file_id,processing_run_id,created_at,width_px,height_px)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_file_id,
                    asset_id,
                    "THUMBNAIL",
                    storage_key,
                    digest,
                    size,
                    "image/png",
                    file_id,
                    run_id,
                    _timestamp(),
                    scaled.width(),
                    scaled.height(),
                ),
            )
            db.execute(
                "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=?",
                (_timestamp(), run_id),
            )
            commit_started = True
            db.commit()
            committed = True
        except BaseException as error:
            db.rollback()
            registered = commit_started and _registration_exists(db_path, new_file_id)
            if owned is not None and not registered:
                try:
                    owned.discard()
                except BaseException as cleanup_error:
                    error.add_note(f"Owned thumbnail cleanup failed: {cleanup_error}")
            if not registered:
                db.execute(
                    """UPDATE processing_runs
                       SET status='FAILED',finished_at=?,error_code='THUMBNAIL_FAILED'
                       WHERE run_id=?""",
                    (_timestamp(), run_id),
                )
                db.commit()
            raise
        finally:
            if owned is not None:
                owned.close()
        if not committed:
            raise RuntimeError("Thumbnail registration did not commit")
        return ImageThumbnail(new_file_id, run_id, storage_key)
