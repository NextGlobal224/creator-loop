"""Actual crash positions, valid-copy phase mismatch and read-only unknown states."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

import test_corrupt_restore_copy as copy_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy
from creator_loop.restore_guard import PendingRestore, require_no_pending_restore


@unittest.skipUnless(sys.platform == "win32", "real guarded native copy fixtures")
class CorruptRestoreInspectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def snapshot_bytes(self):
        # Only our small synthetic fixture, not arbitrary user DB/media content.
        return {
            str(path): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and path.name != "app-data.lock"
        }

    def inspect_unchanged(self, journal):
        before = self.snapshot_bytes()
        result = inspect_corrupt_copy(self.root, journal)
        self.assertEqual(self.snapshot_bytes(), before)
        self.assertTrue(result["guard_retained"])
        self.assertFalse(result["restored"])
        self.assertFalse(result["activated"])
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        return result

    def test_valid_copy_is_derived_from_actual_database_not_recorded_phase(self):
        journal = self.fixture.copy()
        for phase in (
            "CORRUPT_COPY_GUARDED",
            "CORRUPT_COPY_STARTED",
            "CORRUPT_DB_COMMITTED_GUARDED",
            "PRIVATE UNTRUSTED PHASE",
        ):
            record = json.loads(journal.read_text())
            record["phase"] = phase
            journal.write_text(json.dumps(record), encoding="utf-8")
            result = self.inspect_unchanged(journal)
            self.assertEqual(result["actual_state"], "VALIDATED_COPY_GUARDED")
            self.assertEqual(
                result["current_database_identity"],
                record["restored_database_identity"],
            )
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_actual_crashes_classify_unmoved_partial_missing_empty_and_valid_states(
        self,
    ):
        expected = {
            "CORRUPT_COPY_GUARDED": "SOURCE_NOT_MOVED_GUARDED",
            "CORRUPT_SOURCE_RETENTION_STARTED": "SOURCE_PARTIALLY_RETAINED_GUARDED",
            "CORRUPT_SOURCE_RETAINED": "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
            "CORRUPT_COPY_STARTED": "EMPTY_CURRENT_DATABASE_GUARDED",
            "CORRUPT_DB_COMMITTED_GUARDED": "VALIDATED_COPY_GUARDED",
        }
        for index, (phase, state) in enumerate(expected.items()):
            with self.subTest(phase=phase):
                if index:
                    self.setUp()
                code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_restore_copy as module
original=module._journal
def crash(path,record):
    original(path,record)
    if record['phase']==sys.argv[7]: os._exit(33)
module._journal=crash
module.copy_corrupt_restore(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),
 reviewed_preparation=sys.argv[5],reviewed_identity=sys.argv[6],confirm_lost_changes=True)
"""
                f = self.fixture
                run = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(f.manifest),
                        str(f.fixture.fixture.installation),
                        str(f.fixture.candidate),
                        f.preparation_sha,
                        f.fixture.assessment["assessment_identity"],
                        phase,
                    ],
                    env={**os.environ, "PYTHONPATH": "app"},
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(run.returncode, 33, run.stderr)
                journal = next((self.root / "manifests").glob("corrupt-restore-*.json"))
                self.assertEqual(self.inspect_unchanged(journal)["actual_state"], state)
                self.fixture.assert_retained_or_original()

    def test_changed_current_database_and_foreign_sidecar_remain_unknown_and_untouched(
        self,
    ):
        journal = self.fixture.copy()
        with closing(sqlite3.connect(self.fixture.source)) as db:
            db.execute("UPDATE assets SET display_name='foreign change'")
            db.commit()
        result = self.inspect_unchanged(journal)
        self.assertEqual(result["actual_state"], "UNKNOWN_CURRENT_DATABASE")
        (self.root / "creator_loop.sqlite3-wal").write_bytes(b"PRIVATE FOREIGN WAL")
        result = self.inspect_unchanged(journal)
        self.assertIn(
            result["actual_state"], ("UNKNOWN_LIVE_SIDECARS", "UNKNOWN_SOURCE_EVIDENCE")
        )
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_missing_or_duplicated_originals_never_grant_valid_copy_claim(self):
        journal = self.fixture.copy()
        record = json.loads(journal.read_text())
        retained = self.root / "backups" / record["retained_directory"]
        old = retained / "creator_loop.sqlite3-wal"
        body = old.read_bytes()
        old.unlink()  # own fixture mutation only
        self.assertEqual(
            self.inspect_unchanged(journal)["actual_state"], "UNKNOWN_SOURCE_EVIDENCE"
        )
        old.write_bytes(body)
        (self.root / old.name).write_bytes(body)
        self.assertEqual(
            self.inspect_unchanged(journal)["actual_state"], "UNKNOWN_SOURCE_EVIDENCE"
        )

    def test_guard_binding_metadata_duplicate_fields_and_changed_archive_refuse_safely(
        self,
    ):
        journal = self.fixture.copy()
        guard = self.root / "runtime/restore-in-progress.json"
        original = guard.read_bytes()
        record = json.loads(original)
        for changes in (
            {"copy_id": "0" * 32},
            {"corrupt_restore_copy_format": True},
            {"preparation_sha256": "0" * 64},
        ):
            guard.write_text(json.dumps({**record, **changes}), encoding="utf-8")
            before = self.snapshot_bytes()
            with self.assertRaises(ValueError):
                inspect_corrupt_copy(self.root, journal)
            self.assertEqual(self.snapshot_bytes(), before)
        guard.write_bytes(original[:-1] + b',"copy_id":"duplicate"}')
        with self.assertRaises(ValueError):
            inspect_corrupt_copy(self.root, journal)
        guard.write_bytes(original)
        archive = self.fixture.fixture.raw_manifest.parent / "creator_loop.sqlite3"
        archive.write_bytes(b"KEEP ARCHIVE TAMPER")
        before = self.snapshot_bytes()
        with self.assertRaises(ValueError):
            inspect_corrupt_copy(self.root, journal)
        self.assertEqual(self.snapshot_bytes(), before)

    def test_app_lock_contention_and_missing_guard_do_not_change_database(self):
        journal = self.fixture.copy()
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            inspect_corrupt_copy(self.root, journal)
        guard = self.root / "runtime/restore-in-progress.json"
        guard.unlink()  # own fixture only
        before = self.snapshot_bytes()
        with self.assertRaises(ValueError):
            inspect_corrupt_copy(self.root, journal)
        self.assertEqual(self.snapshot_bytes(), before)

    def test_actual_cli_reports_readonly_state_proof_and_refuses_mixed_modes(self):
        journal = self.fixture.copy()
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--inspect-corrupt-copy",
            str(journal),
        ]
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        before = self.snapshot_bytes()
        result = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["actual_state"], "VALIDATED_COPY_GUARDED")
        self.assertEqual(len(receipt["inspection_identity"]), 64)
        self.assertFalse(receipt["restored"])
        self.assertEqual(self.snapshot_bytes(), before)
        mixed = subprocess.run(
            command + ["--smoke"], env=env, capture_output=True, timeout=20
        )
        self.assertEqual(mixed.returncode, 2)
        self.assertEqual(self.snapshot_bytes(), before)
        self.assertNotIn(b"PRIVATE", result.stdout + result.stderr)

    def test_new_sidecar_during_inspection_refuses_and_keeps_foreign_bytes(self):
        from creator_loop.restore_assessment import _database_identity

        journal = self.fixture.copy()
        sidecar = self.root / "creator_loop.sqlite3-shm"

        def database_identity(db, deadline):
            result = _database_identity(db, deadline)
            sidecar.write_bytes(b"KEEP EXTERNAL SHM")
            return result

        with patch(
            "creator_loop.corrupt_restore_inspection._database_identity",
            database_identity,
        ):
            with self.assertRaisesRegex(RuntimeError, "absent copy evidence appeared"):
                inspect_corrupt_copy(self.root, journal)
        self.assertEqual(sidecar.read_bytes(), b"KEEP EXTERNAL SHM")
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
