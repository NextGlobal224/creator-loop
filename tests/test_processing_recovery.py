"""Startup interruption reconciliation preserves provenance and terminal history."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from creator_loop.app_lock import AppDataLock
from creator_loop.database import initialize
from creator_loop.paths import ensure_data_root
from creator_loop.processing_recovery import recover_processing_startup


class ProcessingRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Processing Hue space"
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.raw = (Path(__file__).parent / "fixtures/recovery-image.png").read_bytes()
        self.db.execute(
            "INSERT INTO assets(asset_id,media_type,display_name,created_at) VALUES('image','IMAGE','Fixture','2026-10-03T00:00:00Z')"
        )
        self.db.execute(
            """INSERT INTO asset_files(file_id,asset_id,role,storage_key,
            sha256,byte_size,mime_type,created_at) VALUES('original','image','ORIGINAL',
            'storage/originals/fixture.png',?,?,'image/png','2026-10-03T00:00:00Z')""",
            (hashlib.sha256(self.raw).hexdigest(), len(self.raw)),
        )
        self.original = self.root / "storage/originals/fixture.png"
        self.original.write_bytes(self.raw)
        self.db.commit()

    def run_row(
        self,
        run_id,
        status="RUNNING",
        task="IMAGE_THUMBNAIL",
        tool="PySide6 Qt image scaler",
        model=None,
    ):
        self.db.execute(
            """INSERT INTO processing_runs(run_id,asset_id,input_file_id,
            task_type,status,tool_name,tool_version,model_name,started_at,finished_at,
            error_code,error_message,created_at) VALUES(?,'image','original',?,?,?,
            'legacy-version',?,'2026-10-03T00:00:00Z',?,?,?,'2026-10-03T00:00:00Z')""",
            (
                run_id,
                task,
                status,
                tool,
                model,
                "2026-10-03T00:00:01Z"
                if status in ("SUCCEEDED", "FAILED", "CANCELLED")
                else None,
                "OLD_ERROR" if status == "FAILED" else None,
                "existing context" if status == "FAILED" else None,
            ),
        )
        self.db.commit()

    def snapshot(self):
        return list(self.db.execute("SELECT * FROM processing_runs ORDER BY run_id"))

    def test_known_interruption_preserves_queued_unknown_and_terminal_provenance(self):
        for status in ("RUNNING", "QUEUED", "SUCCEEDED", "FAILED", "CANCELLED"):
            self.run_row(status, status=status)
        self.run_row("foreign-tool", tool="external scaler")
        self.run_row("different-task", task="TRANSCRIBE")
        self.run_row("model-run", model="unselected model")
        before = {row[0]: row for row in self.snapshot()}
        files = list(self.db.execute("SELECT * FROM asset_files"))
        with AppDataLock(self.root) as lock:
            result = recover_processing_startup(self.root, lock)
        self.assertEqual(result.interrupted_run_ids, ("RUNNING",))
        self.assertEqual(result.queued_run_ids, ("QUEUED",))
        self.assertEqual(
            result.unverified_running_ids,
            ("different-task", "foreign-tool", "model-run"),
        )
        after = {row[0]: row for row in self.snapshot()}
        for run_id in before.keys() - {"RUNNING"}:
            self.assertEqual(after[run_id], before[run_id])
        row = self.db.execute(
            "SELECT status,finished_at,error_code,tool_version,model_name,input_file_id FROM processing_runs WHERE run_id='RUNNING'"
        ).fetchone()
        self.assertEqual(row[0], "FAILED")
        self.assertTrue(row[1].endswith("Z"))
        self.assertEqual(
            row[2:], ("INTERRUPTED_AT_STARTUP", "legacy-version", None, "original")
        )
        self.assertEqual(list(self.db.execute("SELECT * FROM asset_files")), files)
        self.assertEqual(self.original.read_bytes(), self.raw)

    def test_repeated_startup_is_idempotent_and_preserves_existing_error_context(self):
        self.run_row("interrupted")
        self.db.execute(
            "UPDATE processing_runs SET error_message='prior context' WHERE run_id='interrupted'"
        )
        self.db.commit()
        with AppDataLock(self.root) as lock:
            recover_processing_startup(self.root, lock)
        first = self.snapshot()
        with AppDataLock(self.root) as lock:
            self.assertEqual(
                recover_processing_startup(self.root, lock).interrupted_run_ids, ()
            )
        self.assertEqual(self.snapshot(), first)
        self.assertTrue(
            self.db.execute("SELECT error_message FROM processing_runs")
            .fetchone()[0]
            .startswith("prior context\n")
        )

    def test_requires_held_lock_for_this_root_before_any_mutation(self):
        self.run_row("interrupted")
        lock = AppDataLock(self.root)
        with self.assertRaises(RuntimeError):
            recover_processing_startup(self.root, lock)
        other = self.root.parent / "other"
        ensure_data_root(other)
        with AppDataLock(other) as foreign:
            with self.assertRaises(RuntimeError):
                recover_processing_startup(self.root, foreign)
        with lock:
            self.assertTrue(lock.held)
        self.assertFalse(lock.held)
        with self.assertRaises(RuntimeError):
            recover_processing_startup(self.root, lock)
        self.assertEqual(
            self.db.execute("SELECT status FROM processing_runs").fetchone()[0],
            "RUNNING",
        )

    def test_writer_failure_rolls_back_all_reconciliations_without_deleting_files(self):
        self.run_row("a")
        self.run_row("b")
        self.db.execute("""CREATE TRIGGER reject_recovery BEFORE UPDATE ON processing_runs
            WHEN NEW.run_id='b' BEGIN SELECT RAISE(ABORT,'blocked recovery'); END""")
        self.db.commit()
        before = self.snapshot()
        with AppDataLock(self.root) as lock:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "blocked recovery"):
                recover_processing_startup(self.root, lock)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.original.read_bytes(), self.raw)
        self.db.execute("DROP TRIGGER reject_recovery")
        self.db.commit()
        with AppDataLock(self.root) as lock:
            self.assertEqual(
                recover_processing_startup(self.root, lock).interrupted_run_ids,
                ("a", "b"),
            )

    def test_missing_database_is_not_created_by_recovery(self):
        other = self.root.parent / "missing"
        ensure_data_root(other)
        with AppDataLock(other) as lock:
            with self.assertRaises(sqlite3.OperationalError):
                recover_processing_startup(other, lock)
        self.assertFalse((other / "creator_loop.sqlite3").exists())

    def test_schema_smoke_modes_do_not_reconcile_domain_runs(self):
        self.run_row("interrupted")
        before = self.snapshot()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        for arguments in (["--smoke"], ["--compatible-only", "--smoke"]):
            result = subprocess.run(
                [sys.executable, "-m", "creator_loop", *arguments],
                env=env,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(sys.platform == "win32", "actual Qt UI startup")
    def test_actual_ui_startup_reconciles_then_displays_notice_and_smoke_stays_readonly(
        self,
    ):
        self.run_row("interrupted")
        self.run_row("unknown", tool="external executor")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        env["QT_QPA_PLATFORM"] = "offscreen"
        before = self.snapshot()
        for arguments in (["--ui-smoke"], ["--compatible-only", "--ui-smoke"]):
            result = subprocess.run(
                [sys.executable, "-m", "creator_loop", *arguments],
                env=env,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(), before)
        code = """import json,sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel
from creator_loop.library_ui import LibraryWindow
from creator_loop.__main__ import main
root=Path(sys.argv[1])
class AutoLibrary(LibraryWindow):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        QTimer.singleShot(20,self.finish)
    def finish(self):
        if self._worker is not None:
            QTimer.singleShot(20,self.finish)
            return
        messages=[item.text() for item in self.findChildren(QLabel)]
        (root/'ui-notice.json').write_text(json.dumps(messages),encoding='utf-8')
        self.close()
sys.argv=['creator-loop','--compatible-only']
with patch('creator_loop.library_ui.LibraryWindow',AutoLibrary):
    raise SystemExit(main())
"""
        normal = subprocess.run(
            [sys.executable, "-c", code, str(self.root)],
            env=env,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(normal.returncode, 0, normal.stderr)
        messages = json.loads(
            (self.root / "ui-notice.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                "1" in message
                and "tác vụ xử lý bị gián đoạn" in message
                and "giữ nguyên" in message
                for message in messages
            )
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM processing_runs WHERE run_id='interrupted'"
            ).fetchone()[0],
            "FAILED",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM processing_runs WHERE run_id='unknown'"
            ).fetchone()[0],
            "RUNNING",
        )

    @unittest.skipUnless(sys.platform == "win32", "actual Qt thumbnail crash fixture")
    def test_real_thumbnail_registration_survives_owner_crash_then_reconciles(self):
        script = """import os,sys
from pathlib import Path
from unittest.mock import patch
from creator_loop.app_lock import AppDataLock
from creator_loop.image_thumbnail import create_image_thumbnail
root=Path(sys.argv[1])
with AppDataLock(root),patch('creator_loop.image_thumbnail.read_verified_image',side_effect=lambda *args: os._exit(17)):
    create_image_thumbnail('original',data_root=root)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=env,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        before = self.db.execute(
            "SELECT run_id,status,tool_name,tool_version FROM processing_runs"
        ).fetchone()
        self.assertEqual(before[1], "RUNNING")
        with AppDataLock(self.root) as lock:
            recovered = recover_processing_startup(self.root, lock)
        self.assertEqual(recovered.interrupted_run_ids, (before[0],))
        self.assertEqual(
            self.db.execute(
                "SELECT status,tool_name,tool_version FROM processing_runs"
            ).fetchone(),
            ("FAILED", before[2], before[3]),
        )
        self.assertEqual(self.original.read_bytes(), self.raw)
