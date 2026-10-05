"""Reconcile interrupted known in-app tasks before starting a new UI session."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from creator_loop.app_lock import AppDataLock
from creator_loop.database import validate
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.transactions import atomic_transaction


def _interrupted_owned_audio(db: sqlite3.Connection, root: Path) -> tuple[str, ...]:
    """Only known executors with bound runtime proof and a dead native owner.

    Never kill processes or infer success from output. Missing/malformed/live
    witnesses leave the run unchanged; preserved RAW/orphans are not cleaned.
    """
    import sys

    if sys.platform != "win32":
        return ()
    rows = db.execute("""SELECT r.run_id,r.asset_id,r.input_file_id,r.task_type,r.tool_name,r.tool_version,
        r.model_name,r.model_version,f.role,f.mime_type,a.media_type
        FROM processing_runs r JOIN asset_files f ON f.file_id=r.input_file_id AND f.asset_id=r.asset_id
        JOIN assets a ON a.asset_id=r.asset_id WHERE r.status='RUNNING'
        AND r.task_type IN ('VIDEO_AUDIO_DECODE','AUDIO_TRANSCRIPTION') ORDER BY r.run_id""").fetchall()
    found = []
    for (
        run,
        asset,
        file_id,
        task,
        tool,
        version,
        model,
        model_version,
        role,
        mime,
        media,
    ) in rows:
        if (
            not isinstance(run, str)
            or not re.fullmatch("[0-9a-f]{32}", run)
            or media != "VIDEO"
            or not isinstance(version, str)
        ):
            continue
        if (
            task == "VIDEO_AUDIO_DECODE"
            and tool == "PySide6 Qt audio decoder"
            and role == "ORIGINAL"
            and mime == "video/mp4"
            and model is None
            and model_version is None
        ):
            component = "qt-pcm/" + version
        elif (
            task == "AUDIO_TRANSCRIPTION"
            and tool == "whisper.cpp"
            and version == "1.8.7"
            and role == "DERIVED_AUDIO"
            and mime == "audio/wav"
            and model == "Whisper base multilingual"
            and model_version == "5359861c739e955e79d9a303bcbc70fb988958b1"
        ):
            component = "whisper.cpp/1.8.7/base-multilingual"
        else:
            continue
        directory = root / "runtime" / ("processing-" + run)
        try:
            with (
                RuntimeHandle(root / "runtime", directory=True),
                RuntimeHandle(directory, directory=True),
                RuntimeHandle(directory / "ownership.json") as marker_handle,
                RuntimeHandle(directory / "task.json") as task_handle,
            ):
                if {entry.name for entry in directory.iterdir()} != {
                    "ownership.json",
                    "task.json",
                }:
                    continue
                marker = marker_handle.read_json(reject_duplicates=True)
                witness = task_handle.read_json(reject_duplicates=True)
                if (
                    not isinstance(marker, dict)
                    or marker.get("format") != 1
                    or marker.get("kind") != "PROCESSING_EXECUTOR_WITNESS"
                    or marker.get("data_root") != str(root)
                    or marker.get("workspace") != directory.name
                    or marker.get("component_version") != component
                ):
                    continue
                if witness != {
                    "format": 1,
                    "run_id": run,
                    "asset_id": asset,
                    "input_file_id": file_id,
                    "task_type": task,
                    "component_version": component,
                }:
                    continue
                parent = marker.get("parent")
                if not isinstance(parent, dict) or not _identity_dead(parent):
                    continue
                found.append(run)
        except (ValueError, OSError, RecursionError):
            continue
    return tuple(found)


@dataclass(frozen=True)
class ProcessingRecovery:
    interrupted_run_ids: tuple[str, ...]
    queued_run_ids: tuple[str, ...]
    unverified_running_ids: tuple[str, ...]


def recover_processing_startup(
    root: Path, coordination: AppDataLock
) -> ProcessingRecovery:
    """Only call after acquiring a fresh app lock, before creating any workers.

    IMAGE_THUMBNAIL's in-app QThread cannot outlive the former app process.
    Known audio executors need a matching runtime witness and dead native owner.
    Unknown/unproven tasks are left alone.
    No file deletion, PID termination, inferred success or queued-task restart.
    """
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError(
            "Processing startup recovery requires this root's held app lock"
        )
    require_no_pending_restore(canonical)
    path = canonical / "creator_loop.sqlite3"
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    ) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        with atomic_transaction(db):
            validate(db)
            known = tuple(
                str(row[0])
                for row in db.execute(
                    """SELECT r.run_id FROM processing_runs r
                   JOIN assets a ON a.asset_id=r.asset_id
                   JOIN asset_files f ON f.file_id=r.input_file_id
                   WHERE r.status='RUNNING' AND r.task_type='IMAGE_THUMBNAIL'
                   AND r.tool_name='PySide6 Qt image scaler'
                   AND r.model_name IS NULL AND r.model_version IS NULL
                   AND a.media_type='IMAGE' AND f.role='ORIGINAL'
                   AND f.mime_type IN ('image/png','image/jpeg') ORDER BY r.run_id"""
                )
            ) + _interrupted_owned_audio(db, canonical)
            finished = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            message = "Previous owned worker ended before recording a terminal result."
            for run_id in known:
                db.execute(
                    """UPDATE processing_runs SET status='FAILED',finished_at=?,
                       error_code='INTERRUPTED_AT_STARTUP',
                       error_message=CASE WHEN error_message IS NULL THEN ?
                         ELSE error_message || char(10) || ? END
                       WHERE run_id=? AND status='RUNNING'""",
                    (finished, message, message, run_id),
                )
            queued = tuple(
                str(row[0])
                for row in db.execute(
                    "SELECT run_id FROM processing_runs WHERE status='QUEUED' ORDER BY run_id"
                )
            )
            unverified = tuple(
                str(row[0])
                for row in db.execute(
                    "SELECT run_id FROM processing_runs WHERE status='RUNNING' ORDER BY run_id"
                )
            )
    return ProcessingRecovery(known, queued, unverified)
