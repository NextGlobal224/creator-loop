"""Source Qt controls calling exact supplied candidate EXE on private copy fixture."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.maintenance_ui import MaintenanceWindow
from creator_loop.update_backup import _digest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    journals = list((root / "manifests").glob("corrupt-restore-*.json"))
    if len(journals) != 1:
        raise ValueError("Only one own private copy fixture required")
    journal = journals[0]
    record = json.loads(journal.read_text(encoding="utf-8"))
    preparation = root / record["preparation_manifest"]
    staged = json.loads(preparation.read_text(encoding="utf-8"))
    protected = [root / "creator_loop.sqlite3", journal, preparation]
    for folder in (
        root / "backups" / record["retained_directory"],
        (root / staged["raw_manifest"]).parent,
        preparation.parent,
        root / "backups" / record["backup_id"],
    ):
        protected += [path for path in folder.iterdir() if path.is_file()]
    before = {path: _digest(path) for path in protected}
    prefix = (
        ["-m", "creator_loop"]
        if args.executable.name.lower().startswith("python")
        else []
    )
    smoke = subprocess.run(
        [str(args.executable), *prefix, "--maintenance", "--ui-smoke"],
        env={**os.environ, "CREATOR_LOOP_DATA_ROOT": str(root)},
        capture_output=True,
        timeout=60,
    )
    if (
        smoke.returncode
        or {path: _digest(path) for path in protected} != before
        or not (root / "runtime/restore-in-progress.json").exists()
    ):
        raise RuntimeError(
            "Exact frozen maintenance startup mutated guarded private data or failed"
        )
    app = QApplication.instance() or QApplication([])
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()
    window = MaintenanceWindow(root, args.installation_root)
    window.journal.setText(str(journal))

    def wait():
        deadline = time.monotonic() + 240
        while window.command.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if window.command.busy:
            raise TimeoutError("Exact candidate UI command exceeded probe budget")

    try:
        with (
            patch.object(sys, "executable", str(args.executable)),
            patch.object(sys, "frozen", not bool(prefix), create=True),
        ):
            window.buttons["review-corrupt-copy"].click()
            wait()
            review = window.copy_review
            if (
                review is None
                or review["inspection"]["actual_state"] != "VALIDATED_COPY_GUARDED"
            ):
                raise RuntimeError(
                    "Exact candidate review did not show validated guarded copy"
                )
            if (
                review["backup_created_at"] not in window.review.toPlainText()
                or window.health_consent.isChecked()
                or window.buttons["recover-corrupt-copy"].isEnabled()
            ):
                raise RuntimeError(
                    "UI omitted backup time or defaulted to health consent"
                )
            window.health_consent.setChecked(True)
            window.buttons["recover-corrupt-copy"].click()
            wait()
            receipt = json.loads(window.review.toPlainText())
            if (
                receipt.get("restored") is not True
                or receipt.get("activated") is not True
                or receipt.get("guard_retained") is not False
            ):
                raise RuntimeError(
                    "Exact candidate health recovery did not complete through UI"
                )
    finally:
        window.close()
        heartbeat.stop()
        app.processEvents()
    if window.command.process is not None or len(ticks) < 2:
        raise RuntimeError("UI failed command cleanup/responsiveness")
    if (root / "runtime/restore-in-progress.json").exists() or {
        path: _digest(path) for path in protected
    } != before:
        raise RuntimeError(
            "UI recovery left guard or changed protected current/original/raw/backup/stage bytes"
        )
    recovery = json.loads(
        (root / "manifests" / f"corrupt-recovery-{record['copy_id']}.json").read_text(
            encoding="utf-8"
        )
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
    ):
        raise RuntimeError(
            "Owned actual candidate health did not match completed recovery"
        )
    print(
        json.dumps(
            {
                **receipt,
                "ui_reviewed": True,
                "commands_cleaned": True,
                "protected_digests_unchanged": True,
                "scope": "exact maintenance startup/source Qt caller to actual EXE review/health/recovery; not full frozen parent UI/product/8GB",
            }
        )
    )


if __name__ == "__main__":
    main()
