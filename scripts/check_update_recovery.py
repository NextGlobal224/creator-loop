"""CI-only fixture: recover a lagging journal with the exact staged EXE.

Source tests exercise actual process exits around COMMIT. This artifact probe
simulates the stale coordination phase, verifies readonly inspection, then
checks the packaged resume/health/metadata path without DB or media restore.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.update_activation import _read_record
from creator_loop.update_backup import _inventory
from creator_loop.update_preparation import _journal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    database = root / "creator_loop.sqlite3"
    with AppDataLock(root), closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            validate(db)
            before = _inventory(db, root)
            record = _read_record(args.journal)
            if record["phase"] != "COMPLETED":
                raise RuntimeError("Use only the completed private CI fixture")
            previous_backup = record["backup_id"]
            record.update(phase="MIGRATION_VALIDATED", migration_committed=False)
            _journal(args.journal, record)
        finally:
            db.rollback()
    env = os.environ.copy()
    env["CREATOR_LOOP_DATA_ROOT"] = str(root)
    common = [str(args.journal), "--installation-root", str(args.installation_root)]
    journal_bytes = args.journal.read_bytes()
    inspection = subprocess.run(
        [str(args.executable), "--inspect-update", *common],
        env=env,
        capture_output=True,
        timeout=60,
        check=True,
    )
    evidence = json.loads(inspection.stdout)
    if (
        evidence["actual_schema"] != SCHEMA_VERSION
        or not evidence["recovery_ready"]
        or args.journal.read_bytes() != journal_bytes
    ):
        raise RuntimeError(
            "Packaged inspection must use actual state without changing journal"
        )
    subprocess.run(
        [str(args.executable), "--resume-update", *common],
        env=env,
        timeout=120,
        check=True,
    )
    recovered = _read_record(args.journal)
    if (
        recovered["phase"] != "COMPLETED"
        or not recovered["recovery_commit_completed"]
        or recovered["backup_id"] == previous_backup
        or not (root / "backups" / previous_backup).is_dir()
    ):
        raise RuntimeError(
            "Packaged recovery must retain the original and current-state backups"
        )
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        validate(db)
        if _inventory(db, root) != before:
            raise RuntimeError("Recovery changed storage reference inventory")
    pointer = _read_record(args.installation_root / "active-installation.json")
    if pointer["status"] != "ACTIVE" or pointer["update_id"] != recovered["update_id"]:
        raise RuntimeError("Fresh health did not activate the recovered installation")
    print(
        json.dumps(
            {
                "recovery_format": 1,
                "actual_schema": SCHEMA_VERSION,
                "phase": recovered["phase"],
                "backup_retained": True,
                "fresh_health": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
