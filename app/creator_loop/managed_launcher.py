"""Launch only a verified healthy pointer; child checks schema again under lock."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import validate
from creator_loop.installation_stage import _installation_root, load_release_manifest
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.update_activation import active_candidate
from creator_loop.update_preparation import _journal
from creator_loop.windows_paths import file_io_path


def launch_managed(
    root: Path, installation_root: Path, *, smoke: bool = False, ui_smoke: bool = False
) -> int:
    canonical = root.resolve(strict=True)
    installation = _installation_root(installation_root, canonical)
    source = canonical / "creator_loop.sqlite3"
    logs = canonical / "logs"
    if (
        source.is_symlink()
        or source.is_junction()
        or not source.is_file()
        or logs.is_symlink()
        or logs.is_junction()
        or not logs.is_dir()
    ):
        raise ValueError("Managed launching requires real existing data and logs")
    with (
        AppDataLock(canonical),
        closing(
            sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=10)
        ) as db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            require_no_pending_restore(canonical)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            validate(db, expected_version=version)
            candidate, pointer = active_candidate(installation, version)
            manifest = load_release_manifest(candidate / "release-manifest.json")
        finally:
            db.rollback()
    # Release the parent locks before the child acquires its own. The child
    # MUST recheck schema under AppDataLock and MUST NOT auto-migrate. An updater
    # winning this gap either makes it busy or causes compatibility refusal.
    arguments = ["--compatible-only"]
    if smoke:
        arguments.append("--smoke")
    elif ui_smoke:
        arguments.append("--ui-smoke")
    environment = os.environ.copy()
    environment["CREATOR_LOOP_DATA_ROOT"] = str(canonical)
    log_directory = logs / f"managed-launch-{uuid4().hex}"
    with OwnedWindowsProcess(
        file_io_path(candidate / "CreatorLoop/CreatorLoop.exe"),
        arguments,
        log_directory,
        component_version=manifest["app_version"],
        environment=environment,
        capture_output=False,
    ) as process:
        record: dict[str, object] = {
            "launch_format": 1,
            "ownership": process.record,
            "update_id": pointer["update_id"],
            "output_capture": False,
        }
        try:
            if smoke or ui_smoke:
                outcome = process.wait(60)
            else:
                polled = process.poll()
                while polled is None:
                    time.sleep(0.1)
                    polled = process.poll()
                outcome = polled
            record.update(asdict(outcome))
            return outcome.exit_code
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            _journal(log_directory / "result.json", record)
