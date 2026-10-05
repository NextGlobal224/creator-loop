"""Exact CLI refuses changed completed copies; only a new synthetic work root."""

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
    for name in (
        "executable",
        "data-root",
        "journal",
        "installation-root",
        "work-root",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    prepared = subprocess.run(
        [
            sys.executable,
            "scripts/check_corrupt_restore_assessment.py",
            "--executable",
            str(args.executable),
            "--data-root",
            str(args.data_root),
            "--journal",
            str(args.journal),
            "--installation-root",
            str(args.installation_root),
            "--work-root",
            str(args.work_root),
        ],
        capture_output=True,
        timeout=240,
    )
    if prepared.returncode:
        raise RuntimeError("Completed private guarded-copy fixture failed")
    root = args.work_root.resolve(strict=True)
    copies = list((root / "manifests").glob("corrupt-restore-*.json"))
    if len(copies) != 1:
        raise ValueError("Exactly one completed private copy required")
    journal = copies[0]
    record = json.loads(journal.read_text(encoding="utf-8"))
    if record["phase"] != "CORRUPT_DB_COMMITTED_GUARDED" or not record.get(
        "copied_database_sha256"
    ):
        raise ValueError("Actual completed physical copy receipt required")
    source = root / "creator_loop.sqlite3"
    closed = source.read_bytes()  # Own completed, closed synthetic fixture only.
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )
    environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(root)}

    def files():
        return {path: _digest(path) for path in root.rglob("*") if path.is_file()}

    def invoke(arguments):
        result = subprocess.run(
            [str(args.executable), *prefix, *arguments],
            env=environment,
            capture_output=True,
            timeout=90,
        )
        if (
            len(result.stdout) > 128 * 1024
            or len(result.stderr) > 128 * 1024
            or b"PRIVATE" in result.stdout + result.stderr
        ):
            raise RuntimeError(
                "Candidate exceeded output budget or exposed private bytes"
            )
        return result

    for mode in ("empty", "missing", "unknown", "sidecar"):
        source.write_bytes(closed)
        if mode == "empty":
            source.write_bytes(b"")
        elif mode == "missing":
            source.unlink()
        elif mode == "unknown":
            source.write_bytes(b"PRIVATE CHANGED COMPLETED COPY")
        else:
            (root / "creator_loop.sqlite3-journal").write_bytes(
                b"PRIVATE NEW HOT JOURNAL"
            )
        before = files()
        inspected = invoke(["--inspect-corrupt-copy", str(journal)])
        if inspected.returncode:
            raise RuntimeError("Actual completed-copy inspection failed")
        proof = json.loads(inspected.stdout)
        review = invoke(
            [
                "--review-corrupt-copy",
                str(journal),
                "--installation-root",
                str(args.installation_root),
            ]
        )
        if review.returncode:
            raise RuntimeError("Actual completed-copy review failed")
        body = json.loads(review.stdout)
        if (
            body.get("requires_fresh_restore_decision") is not True
            or body.get("continuation_allowed") is not False
            or body.get("unknown_continuation_allowed") is not False
        ):
            raise RuntimeError(
                "Candidate granted interrupted-copy readiness to changed completed copy"
            )
        refused = invoke(
            [
                "--resume-corrupt-copy",
                str(journal),
                "--installation-root",
                str(args.installation_root),
                "--reviewed-inspection",
                proof["inspection_identity"],
                "--confirm-lost-changes",
                "--confirm-keep-partial",
                "--confirm-preserve-unknown",
            ]
        )
        if refused.returncode != 4 or files() != before:
            raise RuntimeError(
                "Completed-copy refusal rewrote/initialized files or created a resume journal"
            )
    print(
        "Exact CLI completed empty/missing/unknown/sidecar review and interrupted-consent refusal PASS; all protected bytes/guard/journals preserved; fresh restore apply remains separate"
    )


if __name__ == "__main__":
    main()
