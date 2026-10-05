"""Initial source Qt controls -> supplied CLI on a new private damaged fixture."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.damaged_restore_ui import DamagedRestoreWindow
from creator_loop.maintenance_ui import MaintenanceWindow
from creator_loop.paths import ensure_data_root
from creator_loop.update_activation import verify_prepared_backup
from creator_loop.update_backup import _digest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--installation-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    source = args.data_root.resolve(strict=True)
    record = json.loads(args.journal.read_text(encoding="utf-8"))
    snapshot = verify_prepared_backup(source, record)
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)  # Never overwrite/delete caller data.
    ensure_data_root(root)
    backup = root / "backups" / record["backup_id"]
    backup.mkdir()
    # Published closed Backup API snapshot, never a live WAL main-file copy.
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
                raise ValueError("Only the supported private media fixture is allowed")
            (root / key).write_bytes((source / key).read_bytes())
    damaged = root / "creator_loop.sqlite3"
    damaged.write_bytes(b"PRIVATE INITIAL UI DAMAGED SQLITE\x00\xff" * 100)
    (root / "creator_loop.sqlite3-wal").write_bytes(b"PRIVATE INITIAL UI WAL")
    original = {path.name: _digest(path) for path in root.iterdir() if path.is_file()}
    protected = {
        path: _digest(path)
        for folder in (backup, root / "storage")
        for path in folder.rglob("*")
        if path.is_file()
    }
    app = QApplication.instance() or QApplication([])
    parent = MaintenanceWindow(root, args.installation_root)
    parent.candidate.setText(record["candidate_directory"])
    parent.backup_id.setText(record["backup_id"])
    parent.buttons["damaged-restore"].click()
    window = parent.damaged_window
    if not isinstance(window, DamagedRestoreWindow):
        raise RuntimeError("Maintenance did not open initial damaged-source UI")
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()
    prefix = args.executable.name.lower().startswith("python")

    def wait():
        deadline = time.monotonic() + 240
        while window.command.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if window.command.busy:
            raise TimeoutError("Initial damaged-source UI child exceeded probe budget")

    def review():
        window.buttons["review"].click()
        wait()
        if window.proof is None or window.loss.isChecked() or window.media.isChecked():
            raise RuntimeError(
                "UI failed fresh review or defaulted to loss/media consent"
            )
        if (
            window.proof["assessment"]["backup_created_at"]
            not in window.review.toPlainText()
        ):
            raise RuntimeError("UI omitted selected backup time")

    try:
        with (
            patch.object(sys, "executable", str(args.executable)),
            patch.object(sys, "frozen", not prefix, create=True),
        ):
            review()
            if (
                window.buttons["prepare"].isEnabled()
                or window.buttons["copy"].isEnabled()
            ):
                raise RuntimeError("UI granted initial unpreserved/unconfirmed apply")
            window.buttons["preserve"].click()
            wait()
            if not window.raw.text() or window.proof is not None:
                raise RuntimeError("UI did not retain raw receipt and invalidate proof")
            review()
            if window.buttons["prepare"].isEnabled():
                raise RuntimeError("UI staged without fresh loss consent")
            window.loss.setChecked(True)
            window.buttons["prepare"].click()
            wait()
            if (
                not window.preparation.text()
                or window.proof is not None
                or window.loss.isChecked()
            ):
                raise RuntimeError("UI did not require re-review/consent after staging")
            review()
            if not window.proof["copy_supported"] or window.buttons["copy"].isEnabled():
                raise RuntimeError("UI omitted stage proof or bypassed copy consent")
            window.loss.setChecked(True)
            window.buttons["copy"].click()
            wait()
            journal = Path(parent.journal.text())
            if (
                not journal.is_file()
                or parent.copy_review is not None
                or parent.health_consent.isChecked()
            ):
                raise RuntimeError(
                    "UI did not hand off journal with fresh health consent required"
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
        raise RuntimeError("UI command cleanup/responsiveness failed")
    copied = json.loads(journal.read_text(encoding="utf-8"))
    retained = root / "backups" / copied["retained_directory"]
    if (
        copied["phase"] != "CORRUPT_DB_COMMITTED_GUARDED"
        or {path.name: _digest(path) for path in retained.iterdir()} != original
        or not (root / "runtime/restore-in-progress.json").is_file()
    ):
        raise RuntimeError(
            "Initial UI copy lost original bytes or incorrectly cleared guard"
        )
    recovery = subprocess.run(
        [
            sys.executable,
            "scripts/check_corrupt_copy_recovery.py",
            "--executable",
            str(args.executable),
            "--data-root",
            str(root),
            "--installation-root",
            str(args.installation_root),
            "--maintenance-ui",
        ],
        capture_output=True,
        timeout=300,
    )
    if recovery.returncode or {path: _digest(path) for path in protected} != protected:
        raise RuntimeError(
            "Actual separate UI/candidate health recovery or protected media/backup verification failed"
        )
    print(
        "Initial source Qt -> supplied CLI review/raw/fresh consent/staging/guarded copy/journal handoff, separate actual candidate health/recovery PASS; original/backup/media bytes preserved; not full frozen parent UI/product/8GB/release"
    )


if __name__ == "__main__":
    main()
