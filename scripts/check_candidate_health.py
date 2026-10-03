"""CI fixture: run the exact staged launcher under native ownership and locks."""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.app_lock import AppDataLock
from creator_loop.update_health import run_health_check


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--log-directory", required=True, type=Path)
    parser.add_argument("--component-version", required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    database = root / "creator_loop.sqlite3"
    # CI has a private fresh fixture; include a real reference in the health.
    media = root / "storage/originals/health-fixture.txt"
    media.write_bytes(b"Creator Loop health fixture")
    with AppDataLock(root), closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)",
            ("health-asset", "TEXT", "CI fixture", "2026-10-03T00:00:00Z", None),
        )
        db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "health-file",
                "health-asset",
                "ORIGINAL",
                "storage/originals/health-fixture.txt",
                hashlib.sha256(media.read_bytes()).hexdigest(),
                media.stat().st_size,
                "text/plain",
                "2026-10-03T00:00:00Z",
            ),
        )
        db.commit()
        before = database.read_bytes()
        db.execute("BEGIN IMMEDIATE")
        try:
            payload = run_health_check(
                args.executable,
                root,
                args.log_directory,
                component_version=args.component_version,
            )
            if payload["storage_reference"] != "resolved":
                raise RuntimeError("Candidate did not resolve fixture reference")
        finally:
            db.rollback()
        if database.read_bytes() != before:
            raise RuntimeError("Read-only candidate health changed the database")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
