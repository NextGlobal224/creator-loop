"""Exact candidate probe on a new private damaged-source fixture, never live data."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.paths import ensure_data_root
from creator_loop.update_activation import verify_prepared_backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.journal.read_text(encoding="utf-8"))
    source = args.data_root.resolve(strict=True)
    snapshot = verify_prepared_backup(source, record)
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)  # own new fixture, no overwrite
    ensure_data_root(root)
    backup = root / "backups" / record["backup_id"]
    backup.mkdir()
    # Published validated backup is a closed SQLite snapshot, not a copy of
    # live WAL DB. Preserve its exact digest/manifest in this isolated fixture.
    for name in ("creator_loop.sqlite3", "backup-manifest.json"):
        shutil.copyfile(snapshot.parent / name, backup / name)
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db:
        for role, key, size in db.execute(
            "SELECT role,storage_key,byte_size FROM asset_files"
        ):
            if (
                role != "ORIGINAL"
                or key
                not in (
                    "storage/originals/health-fixture.txt",
                    "storage/originals/text.txt",
                )
                or not 0 < size <= 4096
            ):
                raise ValueError("Use only the supported private CI media fixture")
            (root / key).write_bytes((source / key).read_bytes())
    damaged = root / "creator_loop.sqlite3"
    payload = b"PRIVATE SYNTHETIC DAMAGED SQLITE\x00\xff" * 100
    damaged.write_bytes(payload)
    wal = root / "creator_loop.sqlite3-wal"
    wal.write_bytes(b"PRIVATE SYNTHETIC WAL")
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )
    command = [
        str(args.executable),
        *prefix,
        "--inspect-corrupt-restore",
        record["backup_id"],
        "--restore-candidate",
        record["candidate_directory"],
        "--installation-root",
        str(args.installation_root),
    ]
    environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(root)}
    result = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"Exact candidate damage review refused: exit {result.returncode}"
        )
    if not 0 < len(result.stdout) <= 128 * 1024:
        raise RuntimeError("Candidate damage review exceeded output budget")
    body = json.loads(result.stdout)
    if (
        body["source_kind"] != "DAMAGED_SQLITE"
        or body["current_counts"] is not None
        or body["current_changes_assessable"] is not False
        or body["apply_supported"] is not False
        or body["raw_source_preserved"] is not False
        or body["restored"] is not False
        or body["media_issue_count"] != 0
    ):
        raise RuntimeError("Candidate claimed unsupported source/apply assessment")
    if b"PRIVATE" in result.stdout + result.stderr:
        raise RuntimeError("Candidate exposed private source bytes")
    refused = subprocess.run(
        command + ["--confirm-lost-changes"],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    if refused.returncode != 2:
        raise RuntimeError("Candidate mixed assessment with lost-change consent")
    if before != {
        path.name: path.read_bytes() for path in root.iterdir() if path.is_file()
    }:
        raise RuntimeError("Candidate changed raw source/sidecars")
    damaged.unlink()  # own synthetic fixture only: missing-source refusal
    missing = subprocess.run(command, env=environment, capture_output=True, timeout=20)
    if missing.returncode != 4 or damaged.exists():
        raise RuntimeError("Candidate initialized missing damaged source")
    print(
        "Exact damaged-source backup/candidate/media assessment PASS; raw bytes retained; apply/recovery unverified"
    )


if __name__ == "__main__":
    main()
