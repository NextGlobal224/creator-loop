"""Actual supplied CLI fresh-copy/health probe in exclusive synthetic fixtures."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

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
    parser.add_argument("--maintenance-ui", action="store_true")
    args = parser.parse_args()
    parent = args.work_root.resolve()
    parent.mkdir(parents=True, exist_ok=False)
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )
    for mode in ("missing", "empty", "unknown", "sidecar"):
        root = parent / mode
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
                str(root),
            ],
            capture_output=True,
            timeout=240,
        )
        if prepared.returncode:
            raise RuntimeError("Private completed-copy fixture failed")
        copies = list((root / "manifests").glob("corrupt-restore-*.json"))
        if len(copies) != 1:
            raise ValueError("Exactly one private completed copy required")
        journal = copies[0]
        original = journal.read_bytes()
        record = json.loads(original)
        if record["phase"] != "CORRUPT_DB_COMMITTED_GUARDED":
            raise ValueError("Actual completed copy receipt required")
        source = root / "creator_loop.sqlite3"
        if mode == "missing":
            source.unlink()  # Only this newly created closed private fixture.
        elif mode == "empty":
            source.write_bytes(b"")
        elif mode == "unknown":
            source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        else:
            for suffix in ("-wal", "-shm", "-journal"):
                (root / (source.name + suffix)).write_bytes(
                    b"PRIVATE CURRENT " + suffix.encode()
                )
        current = {
            path.name: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
            for path in root.iterdir()
            if path.is_file() and path.name.startswith(source.name)
        }
        protected = {
            path: _digest(path)
            for path in root.rglob("*")
            if path.is_file() and path.parent != root
        }
        environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(root)}

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
                raise RuntimeError("CLI output budget/private content failure")
            return result

        selectors = [
            "--fresh-restore-backup",
            record["backup_id"],
            "--restore-candidate",
            record["candidate_directory"],
            "--installation-root",
            str(args.installation_root),
        ]
        review = invoke(["--review-completed-copy-restore", str(journal), *selectors])
        if review.returncode:
            raise RuntimeError("Actual distinct fresh review failed")
        choice = json.loads(review.stdout)
        if (
            choice.get("apply_supported") is not True
            or choice.get("apply_authorized") is not False
            or choice.get("consistent_current_backup") is not False
        ):
            raise RuntimeError(
                "Review confused capability with consent or raw retention with backup"
            )
        command = [
            "--apply-completed-copy-restore",
            str(journal),
            *selectors,
            "--reviewed-fresh-restore",
            choice["assessment_identity"],
            "--confirm-fresh-restore",
            "--confirm-lost-changes",
        ]
        for arguments, expected in (
            (command[:-1], 2),
            (command + ["--confirm-preserve-unknown"], 2),
        ):
            if invoke(arguments).returncode != expected:
                raise RuntimeError("Fresh CLI accepted missing/mixed consent")
        stale = list(command)
        stale[stale.index("--reviewed-fresh-restore") + 1] = "0" * 64
        if invoke(stale).returncode != 4:
            raise RuntimeError("Fresh CLI accepted stale review")
        if any(_digest(path) != digest for path, digest in protected.items()) or any(
            (root / name).read_bytes() != entry[0] for name, entry in current.items()
        ):
            raise RuntimeError("Refused fresh copy changed protected files")
        if args.maintenance_ui:
            receipt = _ui_copy(
                root, journal, record, args.executable, args.installation_root
            )
        else:
            copied = invoke(command)
            if copied.returncode:
                raise RuntimeError("Actual confirmed fresh copy failed")
            receipt = json.loads(copied.stdout)
        if (
            receipt.get("phase") != "FRESH_DB_COMMITTED_GUARDED"
            or receipt.get("guard_retained") is not True
            or receipt.get("activated") is not False
            or receipt.get("restored") is not False
        ):
            raise RuntimeError("Fresh copy claimed activation or health")
        fresh_path = root / "manifests" / receipt["journal_name"]
        fresh = json.loads(fresh_path.read_text(encoding="utf-8"))
        fresh_sha = _digest(fresh_path)
        if current:
            retained = root / "backups" / fresh["retained_partial_directory"]
            if {path.name for path in retained.iterdir()} != set(current):
                raise RuntimeError("Fresh bundle retention incomplete")
            for name, (payload, device, inode) in current.items():
                path = retained / name
                if path.read_bytes() != payload or (
                    path.stat().st_dev,
                    path.stat().st_ino,
                ) != (device, inode):
                    raise RuntimeError(
                        "Fresh current original bytes/inodes not retained"
                    )
        elif fresh["retained_partial_directory"] is not None:
            raise RuntimeError("Missing bundle invented retained data")
        if any(
            _digest(path) != digest
            for path, digest in protected.items()
            if path.name != "restore-in-progress.json"
        ):
            raise RuntimeError(
                "Fresh copy changed original/raw/backup/stage/media bytes"
            )
        guard = json.loads(
            (root / "runtime/restore-in-progress.json").read_text(encoding="utf-8")
        )
        if guard.get("fresh_restore_journals") != [fresh_path.name]:
            raise RuntimeError(
                "Fresh history not anchored in guard before current mutation"
            )
        archive_guard = root / "backups" / f"corrupt-guard-{record['copy_id']}.json"
        if (
            _digest(archive_guard)
            != protected[root / "runtime/restore-in-progress.json"]
        ):
            raise RuntimeError("Original guard bytes not preserved before extension")
        inspected = invoke(["--inspect-corrupt-copy", str(journal)])
        if inspected.returncode:
            raise RuntimeError("Actual fresh history inspection failed")
        proof = json.loads(inspected.stdout)
        if (
            proof["actual_state"] != "VALIDATED_COPY_GUARDED"
            or len(proof.get("fresh_retention_history", [])) != 1
        ):
            raise RuntimeError("Fresh copy missing actual state/history proof")
        if args.maintenance_ui:
            _ui_health(root, journal, args.executable, args.installation_root)
            recovered_code = 0
        else:
            recovered_code = invoke(
                [
                    "--recover-corrupt-copy",
                    str(journal),
                    "--installation-root",
                    str(args.installation_root),
                    "--reviewed-inspection",
                    proof["inspection_identity"],
                    "--confirm-recovery",
                ]
            ).returncode
        if recovered_code or (root / "runtime/restore-in-progress.json").exists():
            raise RuntimeError("Separate actual owned health/guard clear failed")
        recovery = json.loads(
            (
                root / "manifests" / f"corrupt-recovery-{record['copy_id']}.json"
            ).read_text(encoding="utf-8")
        )
        health = json.loads(
            (Path(recovery["health_log_directory"]) / "result.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            recovery["phase"] != "RESTORE_COMPLETED"
            or health["exit_code"] != 0
            or health["timed_out"] is not False
            or journal.read_bytes() != original
            or _digest(fresh_path) != fresh_sha
        ):
            raise RuntimeError("Actual fresh recovery/immutable receipts failed")
        for path, digest in protected.items():
            if path.name != "restore-in-progress.json" and _digest(path) != digest:
                raise RuntimeError(
                    "Health changed prior original/raw/backup/stage/media bytes"
                )
        if current and any(
            (retained / name).read_bytes() != entry[0]
            for name, entry in current.items()
        ):
            raise RuntimeError("Health changed fresh originals")
    print(
        "Supplied CLI fresh missing/empty/unknown/sidecar choice, distinct consent, native retention, guarded copy and separate actual owned health PASS; not final GUI/product/hardware/release acceptance"
    )
    if args.maintenance_ui:
        print(
            "SOURCE Qt fresh4state -> supplied exact CLI/candidate, default-off consent, guarded handoff, re-review and separate actual owned health PASS; not full frozen GUI/product acceptance"
        )


def _wait_ui(app, window):
    deadline = time.monotonic() + 180
    while window.command.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    if window.command.busy:
        raise TimeoutError("Owned fresh UI command exceeded probe deadline")


def _ui_copy(root, journal, record, executable, installation):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.fresh_restore_ui import FreshRestoreWindow
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    parent = MaintenanceWindow(root, installation)
    parent.candidate.setText(record["candidate_directory"])
    parent.backup_id.setText(record["backup_id"])
    parent.journal.setText(str(journal))
    parent.buttons["fresh-completed-restore"].click()
    window = parent.fresh_window
    if not isinstance(window, FreshRestoreWindow):
        parent.close()
        raise RuntimeError("Parent did not open distinct fresh dialog")
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()
    try:
        with (
            patch.object(sys, "executable", str(executable)),
            patch.object(
                sys,
                "frozen",
                not executable.name.lower().startswith("python"),
                create=True,
            ),
        ):
            window.review_button.click()
            _wait_ui(app, window)
            if window.proof is None or any(
                box.isChecked()
                for box in (
                    window.fresh_consent,
                    window.loss_consent,
                    window.media_consent,
                )
            ):
                raise RuntimeError("Fresh UI failed review/default-off consent")
            if window.proof["backup_created_at"] not in window.review.toPlainText():
                raise RuntimeError("Fresh UI omitted selected backup time")
            window.loss_consent.setChecked(True)
            if window.copy_button.isEnabled():
                raise RuntimeError("Loss consent bypassed distinct fresh consent")
            window.fresh_consent.setChecked(True)
            if not window.copy_button.isEnabled():
                raise RuntimeError("Fresh reviewed copy not enabled")
            window.copy_button.click()
            _wait_ui(app, window)
            if (
                window.isVisible()
                or parent.copy_review is not None
                or parent.health_consent.isChecked()
            ):
                raise RuntimeError(
                    "Fresh UI did not hand off guarded copy with new health required"
                )
            if (
                parent.journal.text() != str(journal)
                or window.command.log_directory is None
            ):
                raise RuntimeError("Fresh handoff changed original journal")
            receipt = json.loads(
                (window.command.log_directory / "stdout.log").read_text(
                    encoding="utf-8"
                )
            )
    finally:
        parent.close()
        heartbeat.stop()
        app.processEvents()
    if (
        window.command.process is not None
        or parent.command.process is not None
        or len(ticks) < 2
    ):
        raise RuntimeError("Fresh UI owned cleanup/heartbeat failed")
    return receipt


def _ui_health(root, journal, executable, installation):
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    parent = MaintenanceWindow(root, installation)
    parent.journal.setText(str(journal))
    try:
        with (
            patch.object(sys, "executable", str(executable)),
            patch.object(
                sys,
                "frozen",
                not executable.name.lower().startswith("python"),
                create=True,
            ),
        ):
            parent.buttons["review-corrupt-copy"].click()
            _wait_ui(app, parent)
            if (
                parent.copy_review is None
                or parent.copy_review["inspection"]["actual_state"]
                != "VALIDATED_COPY_GUARDED"
            ):
                raise RuntimeError("Fresh parent actual-state re-review failed")
            if (
                parent.buttons["recover-corrupt-copy"].isEnabled()
                or parent.health_consent.isChecked()
            ):
                raise RuntimeError("Fresh parent defaulted to health consent")
            parent.health_consent.setChecked(True)
            if not parent.buttons["recover-corrupt-copy"].isEnabled():
                raise RuntimeError("Separate consented actual health not enabled")
            parent.buttons["recover-corrupt-copy"].click()
            _wait_ui(app, parent)
            if (root / "runtime/restore-in-progress.json").exists():
                raise RuntimeError("Actual fresh parent health did not clear guard")
    finally:
        parent.close()
        app.processEvents()
    if parent.command.process is not None:
        raise RuntimeError("Fresh parent owned health cleanup incomplete")


if __name__ == "__main__":
    main()
