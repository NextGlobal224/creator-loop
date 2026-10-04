"""Recover only the private damaged fixture left by the exact candidate probe."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.update_backup import _digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    journals = list((root / "manifests").glob("corrupt-restore-*.json"))
    if len(journals) != 1:
        raise ValueError("Select the one private guarded-copy fixture")
    journal = journals[0]
    record = json.loads(journal.read_text(encoding="utf-8"))
    if not (root / "runtime/restore-in-progress.json").is_file():
        raise ValueError("Private copy fixture must still be guarded")
    preparation = root / record["preparation_manifest"]
    staged = json.loads(preparation.read_text(encoding="utf-8"))
    raw = root / staged["raw_manifest"]
    retained = root / "backups" / record["retained_directory"]
    protected = [root / "creator_loop.sqlite3", journal, preparation]
    protected.extend(path for path in retained.iterdir() if path.is_file())
    protected.extend(path for path in raw.parent.iterdir() if path.is_file())
    protected.extend(
        (
            preparation.parent / "restored.sqlite3",
            root / "backups" / record["backup_id"] / "creator_loop.sqlite3",
        )
    )
    before = {path: _digest(path) for path in protected}
    environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(root)}
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )

    def invoke(arguments, timeout):
        result = subprocess.run(
            [str(args.executable), *prefix, *arguments],
            env=environment,
            capture_output=True,
            timeout=timeout,
        )
        if (
            len(result.stdout) > 128 * 1024
            or len(result.stderr) > 128 * 1024
            or b"PRIVATE" in result.stdout + result.stderr
        ):
            raise RuntimeError(
                "Recovery candidate exceeded output budget or disclosed fixture bytes"
            )
        return result

    inspected = invoke(["--inspect-corrupt-copy", str(journal)], 90)
    if inspected.returncode:
        raise RuntimeError(f"Candidate inspection failed: exit {inspected.returncode}")
    proof = json.loads(inspected.stdout)
    command = [
        "--recover-corrupt-copy",
        str(journal),
        "--reviewed-inspection",
        proof["inspection_identity"],
        "--installation-root",
        str(args.installation_root),
    ]
    for arguments in (command, command + ["--confirm-recovery", "--smoke"]):
        if invoke(arguments, 30).returncode != 2:
            raise RuntimeError(
                "Exact recovery failed to require unmixed explicit consent"
            )
    stale = [
        "0" * 64 if value == proof["inspection_identity"] else value
        for value in command
    ]
    if invoke(stale + ["--confirm-recovery"], 90).returncode != 4:
        raise RuntimeError("Exact recovery accepted stale review")
    if {path: _digest(path) for path in protected} != before:
        raise RuntimeError("Refused recovery mutated private evidence")
    recovered = invoke(command + ["--confirm-recovery"], 240)
    if recovered.returncode != 0:
        raise RuntimeError(
            f"Exact native recovery/health failed: exit {recovered.returncode}"
        )
    receipt = json.loads(recovered.stdout)
    if (
        receipt.get("activated") is not True
        or receipt.get("restored") is not True
        or receipt.get("guard_retained") is not False
    ):
        raise RuntimeError("Recovery receipt did not establish completion")
    if (root / "runtime/restore-in-progress.json").exists() or {
        path: _digest(path) for path in protected
    } != before:
        raise RuntimeError(
            "Completed recovery retained guard or changed protected bytes"
        )
    recovery = json.loads(
        (root / "manifests" / f"corrupt-recovery-{record['copy_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    pointer = json.loads(
        (args.installation_root / "active-installation.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        recovery["phase"] != "RESTORE_COMPLETED"
        or pointer["status"] != "ACTIVE"
        or pointer["update_id"] != record["copy_id"]
        or pointer["candidate_name"] != Path(record["candidate_directory"]).name
    ):
        raise RuntimeError("Actual activation/journal does not match recovered copy")
    health = json.loads(
        (Path(recovery["health_log_directory"]) / "result.json").read_text(
            encoding="utf-8"
        )
    )
    if health["exit_code"] != 0 or health["timed_out"] is not False:
        raise RuntimeError("Real owned candidate health did not complete")
    print(
        "Exact guarded-copy recovery/owned candidate health/activation/metadata/guard clear PASS; original/raw/backup/stage/current bytes unchanged; not final product or hardware acceptance"
    )


if __name__ == "__main__":
    main()
