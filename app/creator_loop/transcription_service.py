"""Register immutable machine RAW and provenance for a verified audio derivative.

An owned I/O caller holds the data-root app lock and bounds this entire service
by a process deadline. This service never creates Evidence or review events.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import _connect_write, validate
from creator_loop.library import LibraryRepository
from creator_loop.local_components import ComponentSpec
from creator_loop.originals import verify_original_file
from creator_loop.processing_witness import record_processing_executor
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.storage_paths import new_storage_destination, resolve_storage_path
from creator_loop.text_intake import _registration_exists, _stored_digest_and_size
from creator_loop.whisper_adapter import WhisperSettings, transcribe_whisper_wav
from creator_loop.windows_owned_file import OwnedWindowsFile


@dataclass(frozen=True)
class RegisteredTranscript:
    run_id: str
    raw_file_id: str
    provenance_file_id: str
    raw_sha256: str
    workspace: Path


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def transcribe_audio_derivative(
    file_id: str,
    root: Path,
    coordination: AppDataLock,
    specs: tuple[ComponentSpec, ...],
    *,
    settings: WhisperSettings,
    cancelled: Callable[[], bool] = lambda: False,
) -> RegisteredTranscript:
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError("Transcription requires this root's held app lock")
    require_no_pending_restore(canonical)
    db_path = canonical / "creator_loop.sqlite3"
    with closing(_connect_write(db_path)) as db, ExitStack() as held:
        validate(db)
        row = db.execute(
            """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size,f.parent_file_id,f.duration_ms
               FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
               JOIN asset_files p ON p.file_id=f.parent_file_id AND p.asset_id=f.asset_id
               JOIN processing_runs r ON r.run_id=f.processing_run_id AND r.asset_id=f.asset_id
               WHERE f.file_id=? AND f.role='DERIVED_AUDIO' AND f.mime_type='audio/wav'
               AND a.media_type='VIDEO' AND a.deleted_at IS NULL AND p.role='ORIGINAL'
               AND p.mime_type='video/mp4' AND r.task_type='VIDEO_AUDIO_DECODE'
               AND r.status='SUCCEEDED' AND r.input_file_id=p.file_id""",
            (file_id,),
        ).fetchone()
        if row is None or type(row[5]) is not int or row[5] <= 0:
            raise ValueError(
                "Transcription requires a successful VIDEO audio derivative"
            )
        asset_id, key, expected_digest, expected_size, original_id, duration = row
        run_id, raw_id, provenance_id = uuid4().hex, uuid4().hex, uuid4().hex
        LibraryRepository(db).create_processing_run(
            run_id=run_id,
            asset_id=asset_id,
            input_file_id=file_id,
            task_type="AUDIO_TRANSCRIPTION",
            status="QUEUED",
            tool_name="whisper.cpp",
            tool_version="1.8.7",
            model_name="Whisper base multilingual",
            model_version="5359861c739e955e79d9a303bcbc70fb988958b1",
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            created_at=_now(),
        )
        commit_started = False
        try:
            record_processing_executor(
                canonical,
                run_id,
                asset_id,
                file_id,
                "AUDIO_TRANSCRIPTION",
                "whisper.cpp/1.8.7/base-multilingual",
            )
            if cancelled():
                raise InterruptedError("Transcription cancelled before preflight")
            db.execute(
                "UPDATE processing_runs SET status='RUNNING',started_at=? WHERE run_id=? AND status='QUEUED'",
                (_now(), run_id),
            )
            db.commit()
            original = db.execute(
                "SELECT storage_key FROM asset_files WHERE file_id=?", (original_id,)
            ).fetchone()[0]
            held.enter_context(
                _open_read_lock(resolve_storage_path(canonical, "ORIGINAL", original))
            )
            verify_original_file(db, original_id, canonical)
            path = resolve_storage_path(canonical, "DERIVED_AUDIO", key)
            result = transcribe_whisper_wav(
                specs,
                path,
                canonical,
                coordination,
                settings=settings,
                expected_sha256=expected_digest,
                expected_size=expected_size,
                cancelled=cancelled,
            )
            verify_original_file(db, original_id, canonical)
            # Runtime proof is not a second authority for domain state. SQLite
            # remains canonical; immutable asset_files records bind these bytes.
            provenance = json.dumps(
                {
                    "format": 1,
                    "kind": "WHISPER_TRANSCRIPTION_PROVENANCE",
                    "run_id": run_id,
                    "input_file_id": file_id,
                    "original_file_id": original_id,
                    "raw_file_id": raw_id,
                    "raw_sha256": result.raw_sha256,
                    "input_sha256": result.input_sha256,
                    "input_bytes": result.input_bytes,
                    "components": [
                        {
                            "component_id": spec.component_id,
                            "kind": spec.kind,
                            "version": spec.version,
                            "sha256": spec.sha256,
                            "byte_size": spec.byte_size,
                            "source_url": spec.source_url,
                            "license": spec.license,
                            "license_url": spec.license_url,
                        }
                        for spec in specs
                    ],
                    "settings": {
                        "language": settings.language,
                        "threads": settings.threads,
                        "memory_limit_bytes": settings.memory_limit_bytes,
                        "timeout_seconds": settings.timeout_seconds,
                        "no_gpu": True,
                        "beam_size": 1,
                        "best_of": 1,
                    },
                    "ownership": result.ownership,
                    "peak_job_commit_bytes": result.peak_job_commit_bytes,
                    "elapsed_seconds": result.elapsed_seconds,
                    "quality_accepted": False,
                },
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            if hashlib.sha256(result.raw_json).hexdigest() != result.raw_sha256:
                raise ValueError("Adapter RAW digest mismatch")
            entries = []
            for identity, name, payload, parent in (
                (raw_id, raw_id + "-raw.json", result.raw_json, file_id),
                (provenance_id, provenance_id + "-provenance.json", provenance, raw_id),
            ):
                key, destination = new_storage_destination(canonical, "OTHER", name)
                held.enter_context(
                    RuntimeHandle(
                        destination.parent, directory=True, allow_child_writes=True
                    )
                )
                owned = OwnedWindowsFile.create_new(destination)
                held.callback(owned.close)
                remaining = memoryview(payload)
                while remaining:
                    if cancelled():
                        raise InterruptedError("Transcription registration cancelled")
                    count = owned.stream.write(remaining[: 1024**2])
                    if type(count) is not int or not 0 < count <= min(
                        len(remaining), 1024**2
                    ):
                        raise OSError("RAW write made no forward progress")
                    remaining = remaining[count:]
                owned.stream.flush()
                os.fsync(owned.stream.fileno())
                digest, size = _stored_digest_and_size(owned)
                if (
                    size != len(payload)
                    or digest != hashlib.sha256(payload).hexdigest()
                ):
                    raise ValueError("Stored RAW/provenance identity mismatch")
                entries.append((identity, key, destination, digest, size, parent))
            if cancelled():
                raise InterruptedError("Transcription cancelled before registration")
            db.execute("BEGIN IMMEDIATE")
            for identity, key, destination, digest, size, parent in entries:
                if resolve_storage_path(canonical, "OTHER", key) != destination:
                    raise ValueError("RAW storage changed before registration")
                db.execute(
                    """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,
                    byte_size,mime_type,parent_file_id,processing_run_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identity,
                        asset_id,
                        "OTHER",
                        key,
                        digest,
                        size,
                        "application/json",
                        parent,
                        run_id,
                        _now(),
                    ),
                )
            db.execute(
                "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=? AND status='RUNNING'",
                (_now(), run_id),
            )
            commit_started = True
            db.commit()
            return RegisteredTranscript(
                run_id, raw_id, provenance_id, result.raw_sha256, result.workspace
            )
        except BaseException as error:
            db.rollback()
            registered = commit_started and _registration_exists(db_path, raw_id)
            if not registered:
                is_cancel = isinstance(error, InterruptedError)
                db.execute(
                    """UPDATE processing_runs SET status=?,finished_at=?,error_code=?,error_message=?
                    WHERE run_id=? AND status IN ('QUEUED','RUNNING')""",
                    (
                        "CANCELLED" if is_cancel else "FAILED",
                        _now(),
                        "CANCELLED" if is_cancel else "TRANSCRIPTION_FAILED",
                        str(error)[:1024],
                        run_id,
                    ),
                )
                db.commit()
            # Retain every RAW/provenance copy and workspace on any failure.
            # No guessed cleanup, inferred success or human review event.
            raise
