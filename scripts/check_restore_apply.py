"""Private CI fixture: explicit confirmation and restore with the exact EXE."""

import argparse
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    record = json.loads(args.journal.read_text(encoding="utf-8"))
    if record["phase"] != "COMPLETED":
        raise RuntimeError("Use only a completed private CI update fixture")
    source = root / "creator_loop.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)",
            (
                "restore-new-row",
                "TEXT",
                "private CI fixture after backup",
                "2026-10-03T00:00:00Z",
                None,
            ),
        )
        db.commit()
    newer = root / "storage/originals/restore-newer-unreferenced.txt"
    newer.write_bytes(b"private newer fixture retained")
    env = os.environ.copy()
    env["CREATOR_LOOP_DATA_ROOT"] = str(root)
    common = [
        record["backup_id"],
        "--restore-candidate",
        record["candidate_directory"],
        "--installation-root",
        str(args.installation_root),
    ]
    inspected = subprocess.run(
        [str(args.executable), "--inspect-restore", *common],
        env=env,
        capture_output=True,
        timeout=120,
        check=True,
    )
    review = json.loads(inspected.stdout)
    if review["media_issue_count"] or not review["requires_lost_changes_confirmation"]:
        raise RuntimeError("Restore fixture assessment unexpectedly unavailable")
    before = source.read_bytes()
    unconfirmed = subprocess.run(
        [
            str(args.executable),
            "--apply-restore",
            *common,
            "--reviewed-restore",
            review["assessment_identity"],
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    if unconfirmed.returncode != 2 or source.read_bytes() != before:
        raise RuntimeError(
            "Unconfirmed packaged restore must be refused without DB writes"
        )
    subprocess.run(
        [
            str(args.executable),
            "--apply-restore",
            *common,
            "--reviewed-restore",
            review["assessment_identity"],
            "--confirm-lost-changes",
        ],
        env=env,
        timeout=180,
        check=True,
    )
    restores = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "manifests").glob("update-*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("restore_format") == 1
    ]
    if (
        len(restores) != 1
        or restores[0]["phase"] != "COMPLETED"
        or (root / "runtime/restore-in-progress.json").exists()
    ):
        raise RuntimeError(
            "Packaged restore has not passed health/metadata or cleared its guard"
        )
    with closing(sqlite3.connect(source)) as db:
        if db.execute(
            "SELECT 1 FROM assets WHERE asset_id='restore-new-row'"
        ).fetchone():
            raise RuntimeError("Confirmed fixture restore did not restore the backup")
    current_backup = (
        root / "backups" / restores[0]["pre_restore_backup_id"] / "creator_loop.sqlite3"
    )
    with closing(sqlite3.connect(current_backup.as_uri() + "?mode=ro", uri=True)) as db:
        if (
            db.execute(
                "SELECT 1 FROM assets WHERE asset_id='restore-new-row'"
            ).fetchone()
            is None
        ):
            raise RuntimeError("Pre-restore backup did not retain fixture's newer data")
    if newer.read_bytes() != b"private newer fixture retained":
        raise RuntimeError("Restore changed newer media")
    print(
        json.dumps(
            {
                "restore_format": 1,
                "confirmed": True,
                "phase": restores[0]["phase"],
                "pre_restore_data_retained": True,
                "newer_media_retained": True,
                "fresh_health": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
