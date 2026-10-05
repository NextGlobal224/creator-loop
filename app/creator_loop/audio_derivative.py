"""Persist verified video PCM with its own task/run; no transcription or review.

Call in an owned I/O process, holding the app lock and Qt event loop. The caller's
process deadline also bounds hashing, SQLite, output writes and native cleanup.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import qVersion

from creator_loop.app_lock import AppDataLock
from creator_loop.database import _connect_write, validate
from creator_loop.isolated_decode import decode_isolated
from creator_loop.library import LibraryRepository
from creator_loop.originals import verify_original_file
from creator_loop.processing_witness import record_processing_executor
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.storage_paths import new_storage_destination, resolve_storage_path
from creator_loop.text_intake import _registration_exists, _stored_digest_and_size
from creator_loop.windows_owned_file import OwnedWindowsFile


@dataclass(frozen=True)
class AudioDerivative:
    file_id: str
    run_id: str
    storage_key: str
    frames: int
    duration_ms: int
    sha256: str


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def create_video_audio_derivative(
    file_id: str,
    root: Path,
    coordination: AppDataLock,
    *,
    timeout_seconds: float = 120,
    cancelled: Callable[[], bool] = lambda: False,
) -> AudioDerivative:
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError("Audio derivative requires this root's held app lock")
    require_no_pending_restore(canonical)
    db_path = canonical / "creator_loop.sqlite3"
    with closing(_connect_write(db_path)) as db:
        validate(db)
        row = db.execute(
            """SELECT f.asset_id,f.storage_key,f.duration_ms FROM asset_files f
               JOIN assets a ON a.asset_id=f.asset_id WHERE f.file_id=?
               AND a.media_type='VIDEO' AND a.deleted_at IS NULL
               AND f.role='ORIGINAL' AND f.mime_type='video/mp4'""",
            (file_id,),
        ).fetchone()
        if row is None or type(row[2]) is not int or row[2] <= 0:
            raise ValueError("PCM requires a VIDEO original with verified duration")
        asset_id, input_key, original_duration = row
        run_id, new_id = uuid4().hex, uuid4().hex
        LibraryRepository(db).create_processing_run(
            run_id=run_id,
            asset_id=asset_id,
            input_file_id=file_id,
            task_type="VIDEO_AUDIO_DECODE",
            status="QUEUED",
            tool_name="PySide6 Qt audio decoder",
            tool_version=qVersion(),
            model_name=None,
            model_version=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            created_at=_now(),
        )
        owned = None
        committed = commit_started = False
        try:
            record_processing_executor(
                canonical,
                run_id,
                asset_id,
                file_id,
                "VIDEO_AUDIO_DECODE",
                "qt-pcm/" + qVersion(),
            )
            if cancelled():
                raise InterruptedError("Audio derivative cancelled before decode")
            db.execute(
                "UPDATE processing_runs SET status='RUNNING',started_at=? WHERE run_id=? AND status='QUEUED'",
                (_now(), run_id),
            )
            db.commit()
            path = resolve_storage_path(canonical, "ORIGINAL", input_key)
            with _open_read_lock(path) as source:
                verify_original_file(db, file_id, canonical)
                body, _ = decode_isolated(
                    path,
                    canonical,
                    mode="pcm",
                    start_ms=0,
                    timeout_seconds=timeout_seconds,
                    source=source,
                    cancelled=cancelled,
                )
                verify_original_file(db, file_id, canonical)
            if body["duration_ms"] > original_duration:
                raise ValueError("PCM duration exceeds original VIDEO; no clipping")
            raw = body["pcm_wav"]
            key, destination = new_storage_destination(
                canonical, "DERIVED_AUDIO", new_id + ".wav"
            )
            with RuntimeHandle(
                destination.parent, directory=True, allow_child_writes=True
            ):
                owned = OwnedWindowsFile.create_new(destination)
                remaining = memoryview(raw)
                while remaining:
                    if cancelled():
                        raise InterruptedError("Audio derivative write cancelled")
                    written = owned.stream.write(remaining[: 1024**2])
                    if type(written) is not int or not 0 < written <= min(
                        len(remaining), 1024**2
                    ):
                        raise OSError("PCM write made no forward progress")
                    remaining = remaining[written:]
                owned.stream.flush()
                os.fsync(owned.stream.fileno())
                digest, size = _stored_digest_and_size(owned)
                if size != len(raw) or digest != hashlib.sha256(raw).hexdigest():
                    raise ValueError("Stored PCM digest/size mismatch")
                if cancelled():
                    raise InterruptedError(
                        "Audio derivative cancelled before registration"
                    )
                db.execute("BEGIN IMMEDIATE")
                if resolve_storage_path(canonical, "DERIVED_AUDIO", key) != destination:
                    raise ValueError("PCM storage changed before registration")
                db.execute(
                    """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,
                       byte_size,mime_type,parent_file_id,processing_run_id,created_at,duration_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id,
                        asset_id,
                        "DERIVED_AUDIO",
                        key,
                        digest,
                        size,
                        "audio/wav",
                        file_id,
                        run_id,
                        _now(),
                        body["duration_ms"],
                    ),
                )
                db.execute(
                    "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=? AND status='RUNNING'",
                    (_now(), run_id),
                )
                commit_started = True
                db.commit()
                committed = True
        except BaseException as error:
            db.rollback()
            registered = commit_started and _registration_exists(db_path, new_id)
            if owned is not None and not registered:
                try:
                    owned.discard()
                except BaseException as cleanup_error:
                    error.add_note("Owned PCM cleanup failed: " + str(cleanup_error))
            if not registered:
                cancelled_run = isinstance(error, InterruptedError)
                db.execute(
                    """UPDATE processing_runs SET status=?,finished_at=?,error_code=?,error_message=?
                       WHERE run_id=? AND status IN ('QUEUED','RUNNING')""",
                    (
                        "CANCELLED" if cancelled_run else "FAILED",
                        _now(),
                        "CANCELLED" if cancelled_run else "PCM_FAILED",
                        str(error)[:1024],
                        run_id,
                    ),
                )
                db.commit()
            raise
        finally:
            if owned is not None:
                owned.close()
        if not committed:
            raise RuntimeError("PCM registration did not commit")
        return AudioDerivative(
            new_id, run_id, key, body["frames"], body["duration_ms"], digest
        )
