"""CI-only fixture: inspect a backup using exact EXE and detect changed media."""

import argparse
import json
import os
import subprocess
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
        raise RuntimeError("Use a completed private CI update fixture")
    command = [
        str(args.executable),
        "--inspect-restore",
        record["backup_id"],
        "--restore-candidate",
        record["candidate_directory"],
        "--installation-root",
        str(args.installation_root),
    ]
    env = os.environ.copy()
    env["CREATOR_LOOP_DATA_ROOT"] = str(root)
    before = (root / "creator_loop.sqlite3").read_bytes(), args.journal.read_bytes()
    result = subprocess.run(
        command, env=env, capture_output=True, timeout=120, check=True
    )
    assessment = json.loads(result.stdout)
    if (
        assessment["restored"]
        or assessment["media_issue_count"]
        or not assessment["requires_lost_changes_confirmation"]
    ):
        raise RuntimeError(
            "Packaged assessment must report valid media and loss confirmation"
        )
    # This specific file was created by check_candidate_health.py, never select
    # an arbitrary user file. Restore its exact fixture bytes even on failure.
    media = root / "storage/originals/health-fixture.txt"
    original = media.read_bytes()
    try:
        media.write_bytes(b"x" * len(original))
        changed = subprocess.run(
            command, env=env, capture_output=True, timeout=120, check=True
        )
        altered = json.loads(changed.stdout)
        entries = [
            entry
            for entry in altered["media_assessment"]
            if entry["file_id"] == "health-file"
        ]
        if (
            len(entries) != 1
            or entries[0]["status"] != "digest_mismatch"
            or not altered["requires_media_issues_confirmation"]
            or altered["assessment_identity"] == assessment["assessment_identity"]
        ):
            raise RuntimeError("Packaged assessment missed same-size changed media")
    finally:
        media.write_bytes(original)
    if (
        (root / "creator_loop.sqlite3").read_bytes(),
        args.journal.read_bytes(),
    ) != before:
        raise RuntimeError("Restore assessment modified DB or update journal")
    print(
        json.dumps(
            {
                "restore_assessment_format": 1,
                "restored": False,
                "same_size_digest_mismatch_detected": True,
                "db_and_journal_preserved": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
