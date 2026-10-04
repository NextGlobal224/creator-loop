"""Actual source-copy crash, then exact candidate continuation on own new fixture."""

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
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--unknown-partial", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise OSError("Native source crash and candidate continuation require Windows")
    # The existing probe creates its own new work root exclusively. It never
    # removes/overwrites a caller's existing tree. All destructive/crash work
    # below is limited to that freshly created synthetic root.
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
            "--stop-before-copy",
        ],
        capture_output=True,
        timeout=240,
    )
    if prepared.returncode:
        raise RuntimeError(
            f"Private candidate fixture preparation failed: exit {prepared.returncode}"
        )
    root = args.work_root.resolve(strict=True)
    stages = list((root / "backups").glob("corrupt-restore-*/preparation.json"))
    if len(stages) != 1:
        raise ValueError("Exactly one own synthetic preparation required")
    stage = stages[0]
    metadata = json.loads(stage.read_text(encoding="utf-8"))
    outer = json.loads(args.journal.read_text(encoding="utf-8"))
    before_originals = {
        path.name: _digest(path) for path in root.iterdir() if path.is_file()
    }
    environment = {
        **os.environ,
        "PYTHONPATH": "app",
        "CREATOR_LOOP_DATA_ROOT": str(root),
    }
    # Crash injection exists only in the private source fixture process, never
    # in the production CLI or frozen executable. Actual native APIs perform
    # original retention and CREATE_NEW before this os._exit.
    code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_restore_copy as m
original=m._journal
def crash(path,record):
    original(path,record)
    if record['phase']=='CORRUPT_COPY_STARTED': os._exit(44)
m._journal=crash
m.copy_corrupt_restore(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),reviewed_preparation=sys.argv[5],reviewed_identity=sys.argv[6],confirm_lost_changes=True)
"""
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(root),
            str(stage),
            str(args.installation_root),
            outer["candidate_directory"],
            _digest(stage),
            metadata["reviewed_assessment_identity"],
        ],
        env=environment,
        capture_output=True,
        timeout=90,
    )
    if crashed.returncode != 44:
        raise RuntimeError(
            f"Actual source-copy crash did not reach expected checkpoint: {crashed.returncode}"
        )
    partial_bytes = {"creator_loop.sqlite3": b""}
    if args.unknown_partial:
        # Own synthetic foreign bundle represents bytes remaining after an
        # interrupted copy. This is not a frozen mid-BackupAPI crash proof.
        partial_bytes = {
            "creator_loop.sqlite3": b"PRIVATE NONEMPTY PARTIAL DB",
            "creator_loop.sqlite3-wal": b"PRIVATE FOREIGN WAL NO REPLAY",
            "creator_loop.sqlite3-shm": b"PRIVATE FOREIGN SHM",
            "creator_loop.sqlite3-journal": b"PRIVATE HOT JOURNAL NO REPLAY",
        }
        for name, content in partial_bytes.items():
            (root / name).write_bytes(content)
    copies = list((root / "manifests").glob("corrupt-restore-*.json"))
    if len(copies) != 1:
        raise ValueError("Exactly one actual interrupted source copy required")
    copy = copies[0]
    record = json.loads(copy.read_text(encoding="utf-8"))
    retained = root / "backups" / record["retained_directory"]
    protected = [copy, root / "runtime/restore-in-progress.json", stage]
    protected += [path for path in retained.iterdir() if path.is_file()]
    protected += [path for path in stage.parent.iterdir() if path.is_file()]
    raw = (root / metadata["raw_manifest"]).parent
    protected += [path for path in raw.iterdir() if path.is_file()]
    protected += [
        path
        for path in (root / "backups" / record["backup_id"]).iterdir()
        if path.is_file()
    ]
    before = {path: _digest(path) for path in protected}
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )

    def invoke(arguments, timeout=90):
        result = subprocess.run(
            [str(args.executable), *prefix, *arguments],
            env=environment,
            capture_output=True,
            timeout=timeout,
        )
        if (
            len(result.stdout) + len(result.stderr) > 128 * 1024
            or b"PRIVATE" in result.stdout + result.stderr
        ):
            raise RuntimeError(
                "Candidate resume output exceeded budget or disclosed fixture bytes"
            )
        return result

    inspected = invoke(["--inspect-corrupt-copy", str(copy)])
    if inspected.returncode:
        raise RuntimeError("Exact candidate failed to inspect interrupted source copy")
    proof = json.loads(inspected.stdout)
    if (
        not proof["actual_state"].startswith("UNKNOWN_")
        if args.unknown_partial
        else proof["actual_state"] != "EMPTY_CURRENT_DATABASE_GUARDED"
    ) or proof["corrupt_restore_inspection_format"] != 2:
        raise RuntimeError(
            "Candidate inspection did not bind actual empty state with format2"
        )
    command = [
        "--resume-corrupt-copy",
        str(copy),
        "--reviewed-inspection",
        proof["inspection_identity"],
        "--installation-root",
        str(args.installation_root),
        "--confirm-lost-changes",
    ]
    if (
        invoke(command).returncode != 4
        or {name: (root / name).read_bytes() for name in partial_bytes} != partial_bytes
    ):
        raise RuntimeError(
            "Candidate silently continued without explicit partial retention consent"
        )
    consent = (
        "--confirm-preserve-unknown"
        if args.unknown_partial
        else "--confirm-keep-partial"
    )
    resumed = invoke(command + [consent], 240)
    if resumed.returncode:
        raise RuntimeError(
            f"Exact candidate continuation failed: exit {resumed.returncode}"
        )
    receipt = json.loads(resumed.stdout)
    if (
        receipt["restored"] is not False
        or receipt["activated"] is not False
        or receipt["guard_retained"] is not True
    ):
        raise RuntimeError("Resume falsely claimed activation/recovery")
    journal = root / "manifests" / receipt["journal_name"]
    if journal.parent != root / "manifests" or not journal.name.startswith(
        "corrupt-resume-"
    ):
        raise RuntimeError("Resume receipt escaped own fixture")
    resumed_record = json.loads(journal.read_text(encoding="utf-8"))
    if (
        resumed_record["phase"] != "RESUME_DB_COMMITTED_GUARDED"
        or resumed_record["consistent_partial_backup"] is not False
    ):
        raise RuntimeError("Resume journal omitted guarded/nonconsistent status")
    partial = root / "backups" / resumed_record["retained_partial_directory"]
    if {
        path.name: path.read_bytes() for path in partial.iterdir()
    } != partial_bytes or {path: _digest(path) for path in protected} != before:
        raise RuntimeError(
            "Resume lost partial or changed original/journal/raw/backup/stage/guard bytes"
        )
    if {
        name: _digest(retained / name) for name in before_originals
    } != before_originals:
        raise RuntimeError("Interrupted copy/resume lost original bytes")
    fresh = invoke(["--inspect-corrupt-copy", str(copy)])
    if (
        fresh.returncode
        or json.loads(fresh.stdout)["actual_state"] != "VALIDATED_COPY_GUARDED"
    ):
        raise RuntimeError("Actual resumed DB is not validated and still guarded")
    print(
        f"Actual source-copy crash -> exact candidate {'synthetic UNKNOWN bundle' if args.unknown_partial else 'empty target'} consented retention/resume/guarded validation PASS; no frozen mid-copy crash, candidate health/activation or power-loss acceptance"
    )


if __name__ == "__main__":
    main()
