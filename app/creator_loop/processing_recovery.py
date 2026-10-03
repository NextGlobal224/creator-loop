"""Reconcile interrupted known in-app tasks before starting a new UI session."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from creator_loop.app_lock import AppDataLock
from creator_loop.database import validate
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.transactions import atomic_transaction


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
    Unknown/external tasks require executor ownership evidence; leave them alone.
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
            )
            finished = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            message = (
                "Previous thumbnail worker ended before recording a terminal result."
            )
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
