"""Exact candidate probe on a new private damaged-source fixture, never live data."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
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
    preserve = [
        str(args.executable),
        *prefix,
        "--preserve-corrupt-source",
        body["damage"]["damage_identity"],
    ]
    mixed = subprocess.run(
        preserve + ["--smoke"], env=environment, capture_output=True, timeout=20
    )
    if mixed.returncode != 2:
        raise RuntimeError(
            "Candidate mixed raw-source preservation and DB initialization"
        )
    stale = subprocess.run(
        [*preserve[:-1], "0" * 64], env=environment, capture_output=True, timeout=60
    )
    if stale.returncode != 4 or list((root / "backups").glob("raw-source-*")):
        raise RuntimeError("Candidate accepted an unreviewed damaged-source identity")
    saved = subprocess.run(preserve, env=environment, capture_output=True, timeout=180)
    if saved.returncode != 0 or not 0 < len(saved.stdout) <= 128 * 1024:
        raise RuntimeError(
            f"Exact candidate raw-source preservation refused: exit {saved.returncode}"
        )
    receipt = json.loads(saved.stdout)
    if receipt["raw_source_preserved"] is not True or any(
        receipt[key] is not False
        for key in ("consistent_backup", "restore_authorized", "restored")
    ):
        raise RuntimeError("Candidate claimed unsupported raw-source recovery")
    relative = Path(receipt["raw_source_manifest"])
    retained_manifest = (root / relative).resolve(strict=True)
    if (
        relative.is_absolute()
        or retained_manifest.parent.parent != root / "backups"
        or retained_manifest.name != "raw-source-manifest.json"
    ):
        raise RuntimeError("Candidate raw-source receipt escaped the private fixture")
    retained = json.loads(retained_manifest.read_text(encoding="utf-8"))
    if (
        retained["damage"] != body["damage"]
        or retained["raw_bytes_verified"] is not True
        or any(
            retained[key] is not False
            for key in (
                "consistent_backup",
                "restore_authorized",
                "wal_recoverability_assessed",
                "media_included",
            )
        )
    ):
        raise RuntimeError(
            "Candidate raw-source manifest differs from fresh reviewed source"
        )
    if {path.name for path in retained_manifest.parent.iterdir()} != set(before) | {
        retained_manifest.name
    }:
        raise RuntimeError("Candidate raw-source inventory changed")
    for entry in retained["damage"]["raw_files"]:
        if entry["present"]:
            copied = (retained_manifest.parent / entry["name"]).read_bytes()
            if (
                copied != before[entry["name"]]
                or len(copied) != entry["byte_size"]
                or hashlib.sha256(copied).hexdigest() != entry["sha256"]
            ):
                raise RuntimeError("Candidate raw-source retained bytes/digest differ")
    if b"PRIVATE" in saved.stdout + saved.stderr or before != {
        path.name: path.read_bytes() for path in root.iterdir() if path.is_file()
    }:
        raise RuntimeError("Candidate changed/exposed raw source during preservation")
    preparation = [
        str(args.executable),
        *prefix,
        "--prepare-corrupt-restore",
        record["backup_id"],
        "--restore-candidate",
        record["candidate_directory"],
        "--installation-root",
        str(args.installation_root),
        "--raw-source-manifest",
        str(retained_manifest),
        "--reviewed-restore",
        body["assessment_identity"],
        "--confirm-lost-changes",
    ]
    staged_result = subprocess.run(
        preparation, env=environment, capture_output=True, timeout=180
    )
    if staged_result.returncode != 0 or not 0 < len(staged_result.stdout) <= 128 * 1024:
        raise RuntimeError(
            f"Exact candidate corrupt staging refused: exit {staged_result.returncode}"
        )
    staged_receipt = json.loads(staged_result.stdout)
    relative = Path(staged_receipt["corrupt_restore_preparation"])
    stage_manifest = (root / relative).resolve(strict=True)
    if relative.is_absolute() or stage_manifest.parent.parent != root / "backups":
        raise RuntimeError("Candidate staging receipt escaped the private fixture")
    stage_record = json.loads(stage_manifest.read_text(encoding="utf-8"))
    staged_database = stage_manifest.parent / "restored.sqlite3"
    with closing(open_readonly(staged_database.resolve(strict=True))) as db:
        validate(db, expected_version=SCHEMA_VERSION)
    if (
        any(
            staged_receipt[key] is not False
            for key in ("apply_authorized", "activated", "restored")
        )
        or stage_record["phase"] != "CORRUPT_RESTORE_STAGED"
        or stage_record["reviewed_assessment_identity"] != body["assessment_identity"]
        or stage_record["raw_manifest_sha256"]
        != hashlib.sha256(retained_manifest.read_bytes()).hexdigest()
        or stage_record["staged_database_sha256"]
        != hashlib.sha256(staged_database.read_bytes()).hexdigest()
        or before
        != {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
        or b"PRIVATE" in staged_result.stdout + staged_result.stderr
    ):
        raise RuntimeError("Candidate staging changed source/proof or claimed apply")
    stage_verification = [
        str(args.executable),
        *prefix,
        "--verify-corrupt-preparation",
        str(stage_manifest),
        "--reviewed-preparation",
        hashlib.sha256(stage_manifest.read_bytes()).hexdigest(),
    ]
    stage_checked = subprocess.run(
        stage_verification, env=environment, capture_output=True, timeout=90
    )
    if stage_checked.returncode != 0 or not 0 < len(stage_checked.stdout) <= 128 * 1024:
        raise RuntimeError(
            f"Exact stage verification refused: exit {stage_checked.returncode}"
        )
    stage_proof = json.loads(stage_checked.stdout)
    if stage_proof["stage_revalidated"] is not True or any(
        stage_proof[key] is not False
        for key in (
            "current_source_assessed",
            "apply_authorized",
            "activated",
            "restored",
        )
    ):
        raise RuntimeError("Candidate stage verification claimed live-source apply")
    damaged.unlink()  # own synthetic fixture only: missing-source refusal
    verification = [
        str(args.executable),
        *prefix,
        "--verify-preserved-source",
        str(retained_manifest),
        "--reviewed-damage",
        body["damage"]["damage_identity"],
    ]
    checked = subprocess.run(
        verification, env=environment, capture_output=True, timeout=90
    )
    if checked.returncode != 0 or not 0 < len(checked.stdout) <= 128 * 1024:
        raise RuntimeError(
            f"Exact candidate archive revalidation refused: exit {checked.returncode}"
        )
    archive_receipt = json.loads(checked.stdout)
    if (
        archive_receipt["archive_revalidated"] is not True
        or archive_receipt["damage"] != body["damage"]
        or any(
            archive_receipt[key] is not False
            for key in (
                "consistent_backup",
                "restore_authorized",
                "current_source_assessed",
                "restored",
            )
        )
    ):
        raise RuntimeError(
            "Candidate archive verification claimed live-source recovery"
        )
    if (
        damaged.exists()
        or b"PRIVATE" in checked.stdout + checked.stderr
        or archive_receipt["archive_manifest_sha256"]
        != hashlib.sha256(retained_manifest.read_bytes()).hexdigest()
    ):
        raise RuntimeError(
            "Candidate archive verification changed/exposed source or manifest"
        )
    for name, content in before.items():
        if (retained_manifest.parent / name).read_bytes() != content:
            raise RuntimeError("Candidate archive verification changed retained bytes")
    missing = subprocess.run(command, env=environment, capture_output=True, timeout=20)
    if missing.returncode != 4 or damaged.exists():
        raise RuntimeError("Candidate initialized missing damaged source")
    missing_stage = subprocess.run(
        preparation, env=environment, capture_output=True, timeout=20
    )
    if missing_stage.returncode != 4 or damaged.exists():
        raise RuntimeError("Candidate staging initialized missing damaged source")
    absent_live = subprocess.run(
        stage_verification, env=environment, capture_output=True, timeout=90
    )
    if absent_live.returncode != 0 or damaged.exists():
        raise RuntimeError(
            "Stage verification requires/initializes the missing live source"
        )
    print(
        "Exact damaged-source assessment/raw retention/archive/BackupAPI staging/stage verification PASS; missing live DB not created; apply/recovery/health unverified"
    )


if __name__ == "__main__":
    main()
