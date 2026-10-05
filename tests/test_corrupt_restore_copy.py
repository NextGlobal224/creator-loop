"""Actual guarded Windows source retention/copy and crash outcomes on fixtures."""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_corrupt_restore_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_restore_copy import copy_corrupt_restore
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
from creator_loop.restore_guard import PendingRestore, require_no_pending_restore


class CorruptRestoreCopyTests(unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def setup_fixture(self, *, closed_current_seed=True):
        self.fixture = preparation_fixture.CorruptRestorePreparationTests()
        self.fixture.setup_fixture(
            6 if closed_current_seed else 1, closed_current_seed=closed_current_seed
        )
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.manifest = self.fixture.prepare()
        self.preparation_sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.source = self.fixture.fixture.path
        self.source_before = {
            name: (self.root / name).read_bytes()
            for name in (
                "creator_loop.sqlite3",
                "creator_loop.sqlite3-wal",
            )
        }
        self.protected = self.fixture.protected_bytes()
        for name in self.source_before:
            self.protected.pop(str(self.root / name))

    def copy(self, **kwargs):
        options = {
            "reviewed_preparation": self.preparation_sha,
            "reviewed_identity": self.fixture.assessment["assessment_identity"],
            "confirm_lost_changes": True,
        }
        options.update(kwargs)
        return copy_corrupt_restore(
            self.root,
            self.manifest,
            self.fixture.fixture.installation,
            self.fixture.candidate,
            **options,
        )

    @unittest.skipUnless(sys.platform == "win32", "native legacy BackupAPI copy")
    def test_legacy_schema1_backup_still_prepares_and_copies_actual_migrated_stage(
        self,
    ):
        self.setup_fixture(closed_current_seed=False)
        journal = self.copy()
        record = json.loads(journal.read_text())
        self.assertEqual(record["schema_from"], 1)
        self.assertEqual(record["schema_to"], SCHEMA_VERSION)
        with closing(open_readonly(self.source.resolve(strict=True))) as db:
            validate(db)
        self.assert_retained_or_original()

    def assert_protected(self):
        self.assertEqual(
            {path: Path(path).read_bytes() for path in self.protected}, self.protected
        )
        self.assertEqual(
            hashlib.sha256(self.manifest.read_bytes()).hexdigest(), self.preparation_sha
        )

    def assert_retained_or_original(self):
        directories = list((self.root / "backups").glob("corrupt-original-*"))
        for name, body in self.source_before.items():
            candidates = [self.root / name, *(folder / name for folder in directories)]
            self.assertEqual(
                sum(
                    path.is_file() and path.read_bytes() == body for path in candidates
                ),
                1,
                name,
            )
        self.assert_protected()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_real_backupapi_copy_retains_original_inodes_and_blocks_launch_until_recovery(
        self,
    ):
        before = self.source.stat()
        journal = self.copy()
        record = json.loads(journal.read_text())
        self.assertEqual(record["phase"], "CORRUPT_DB_COMMITTED_GUARDED")
        self.assertFalse(record["restored"])
        self.assertTrue(record["activation_pending"])
        retained = (
            self.root / "backups" / record["retained_directory"] / self.source.name
        )
        self.assertEqual(
            (retained.stat().st_dev, retained.stat().st_ino),
            (before.st_dev, before.st_ino),
        )
        with closing(open_readonly(self.source.resolve())) as db:
            validate(db, expected_version=SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone(), (1,))
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            record["copied_database_sha256"],
        )
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assertFalse(
            (self.fixture.fixture.installation / "active-installation.json").exists()
        )
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_all_present_sidecars_are_retained_without_replay_or_deletion(self):
        from creator_loop.corrupt_source_preservation import preserve_corrupt_source

        for suffix in ("-shm", "-journal"):
            name = "creator_loop.sqlite3" + suffix
            body = ("KEEP " + suffix).encode()
            (self.root / name).write_bytes(body)
            self.source_before[name] = body
        self.fixture.assessment = self.fixture.assess()
        self.fixture.raw_manifest = preserve_corrupt_source(
            self.root, self.fixture.assessment["damage"]["damage_identity"]
        )
        self.manifest = self.fixture.prepare()
        self.preparation_sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        journal = self.copy()
        record = json.loads(journal.read_text())
        folder = self.root / "backups" / record["retained_directory"]
        self.assertEqual(
            {path.name for path in folder.iterdir()}, set(self.source_before)
        )
        self.assert_retained_or_original()
        for name in self.source_before:
            if name != self.source.name:
                self.assertFalse((self.root / name).exists())

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_foreign_sidecar_in_transition_keeps_guard_and_refuses_copy(self):
        from creator_loop.update_preparation import _journal

        def journal(path, record):
            _journal(path, record)
            if record["phase"] == "CORRUPT_COPY_GUARDED":
                (self.root / "creator_loop.sqlite3-shm").write_bytes(b"FOREIGN SHM")

        with patch("creator_loop.corrupt_restore_copy._journal", journal):
            with self.assertRaisesRegex(RuntimeError, "sidecar appeared"):
                self.copy()
        self.assertEqual(
            (self.root / "creator_loop.sqlite3-shm").read_bytes(), b"FOREIGN SHM"
        )
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_stale_review_preparation_and_no_consent_refuse_before_guard_or_rename(
        self,
    ):
        for options in (
            {"confirm_lost_changes": False},
            {"confirm_lost_changes": 1},
            {"reviewed_identity": "0" * 64},
            {"reviewed_preparation": "0" * 64},
            {"timeout_seconds": 0},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.copy(**options)
        self.assertFalse((self.root / "runtime/restore-in-progress.json").exists())
        self.assertFalse(list((self.root / "backups").glob("corrupt-original-*")))
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_busy_cancel_disk_pending_runtime_and_changed_source_refuse_untouched(self):
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            self.copy()
        with self.assertRaises(InterruptedError):
            self.copy(cancelled=lambda: True)
        with patch(
            "creator_loop.corrupt_restore_copy.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaises(OSError):
                self.copy()
        pending = self.root / "runtime/restore-in-progress.json"
        pending.write_bytes(b"KEEP EXISTING GUARD")
        with self.assertRaises(PendingRestore):
            self.copy()
        self.assertEqual(pending.read_bytes(), b"KEEP EXISTING GUARD")
        pending.unlink()  # own fixture only
        self.source.write_bytes(b"CHANGED DAMAGED SOURCE")
        with self.assertRaises(ValueError):
            self.copy()
        self.assertEqual(self.source.read_bytes(), b"CHANGED DAMAGED SOURCE")
        self.assert_protected()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_changed_media_requires_new_preparation_not_reusing_old_consents(self):
        self.fixture.fixture.media.write_bytes(b"NEW MEDIA")
        with self.assertRaises(ValueError):
            self.copy(confirm_media_issues=True)
        self.assertEqual(self.fixture.fixture.media.read_bytes(), b"NEW MEDIA")
        self.assertFalse((self.root / "runtime/restore-in-progress.json").exists())
        self.assertEqual(self.source.read_bytes(), self.source_before[self.source.name])

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_native_candidate_archive_media_stage_held_through_source_transition(self):
        from contextlib import contextmanager

        from creator_loop.corrupt_source_retention import hold_existing_corrupt_source

        @contextmanager
        def acquire(path):
            for protected in (
                self.fixture.candidate / "CreatorLoop/CreatorLoop.exe",
                self.fixture.raw_manifest,
                self.fixture.fixture.media,
                self.manifest.parent / "restored.sqlite3",
            ):
                with self.assertRaises(OSError):
                    protected.write_bytes(b"NEVER MODIFY")
                with self.assertRaises(OSError):
                    protected.unlink()
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    pass
            with hold_existing_corrupt_source(path) as pin:
                yield pin

        with patch(
            "creator_loop.corrupt_restore_copy.hold_existing_corrupt_source", acquire
        ):
            self.copy()
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_journal_failure_after_guard_retains_guard_originals_and_partial_evidence(
        self,
    ):
        with patch(
            "creator_loop.corrupt_restore_copy._journal",
            side_effect=OSError("fixture journal failure"),
        ):
            with self.assertRaises(OSError):
                self.copy()
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assertEqual(self.source.read_bytes(), self.source_before[self.source.name])
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_actual_crashes_at_guard_each_retention_copy_and_completed_copy_keep_bytes(
        self,
    ):
        for phase in (
            "CORRUPT_COPY_GUARDED",
            "CORRUPT_SOURCE_RETENTION_STARTED",
            "CORRUPT_SOURCE_RETAINED",
            "CORRUPT_COPY_STARTED",
            "CORRUPT_DB_COMMITTED_GUARDED",
        ):
            with self.subTest(phase=phase):
                if phase != "CORRUPT_COPY_GUARDED":
                    self.setUp()
                code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_restore_copy as module
original=module._journal
def crash(path,record):
    original(path,record)
    if record['phase']==sys.argv[7]: os._exit(31)
module._journal=crash
module.copy_corrupt_restore(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),
    reviewed_preparation=sys.argv[5],reviewed_identity=sys.argv[6],confirm_lost_changes=True)
"""
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(self.manifest),
                        str(self.fixture.fixture.installation),
                        str(self.fixture.candidate),
                        self.preparation_sha,
                        self.fixture.assessment["assessment_identity"],
                        phase,
                    ],
                    env={**os.environ, "PYTHONPATH": "app"},
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 31, result.stderr)
                with self.assertRaises(PendingRestore):
                    require_no_pending_restore(self.root)
                self.assert_retained_or_original()
                if phase == "CORRUPT_DB_COMMITTED_GUARDED":
                    with closing(open_readonly(self.source.resolve())) as db:
                        validate(db)
                if phase == "CORRUPT_SOURCE_RETAINED":
                    self.assertFalse(self.source.exists())

    @unittest.skipIf(sys.platform == "win32", "portable native refusal")
    def test_portable_refuses_before_any_mutation(self):
        with self.assertRaises(OSError):
            self.copy()
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_actual_cli_requires_fresh_confirmed_inputs_and_never_initializes_missing_source(
        self,
    ):
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--copy-corrupt-restore",
            str(self.manifest),
            "--reviewed-preparation",
            self.preparation_sha,
            "--reviewed-restore",
            self.fixture.assessment["assessment_identity"],
            "--installation-root",
            str(self.fixture.fixture.installation),
            "--restore-candidate",
            str(self.fixture.candidate),
            "--confirm-lost-changes",
        ]
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        for arguments in (command + ["--smoke"], command[:-1]):
            result = subprocess.run(arguments, env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.source.unlink()  # own fixture only
        missing = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(missing.returncode, 4)
        self.assertFalse(self.source.exists())
        self.source.write_bytes(self.source_before[self.source.name])
        result = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["requires_recovery_health"])
        self.assertFalse(receipt["restored"])
        self.assertFalse(receipt["activated"])
        self.assertTrue((self.root / receipt["corrupt_restore_journal"]).is_file())
        self.assert_retained_or_original()
        self.assertNotIn(b"PRIVATE", result.stdout + result.stderr + missing.stderr)

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_source_write_in_lease_gap_is_detected_before_guard_or_move(self):
        from contextlib import contextmanager

        from creator_loop.corrupt_source_retention import hold_existing_corrupt_source

        @contextmanager
        def acquire(path):
            path.write_bytes(b"EXTERNAL CHANGE DURING GAP")
            with hold_existing_corrupt_source(path) as pin:
                yield pin

        with patch(
            "creator_loop.corrupt_restore_copy.hold_existing_corrupt_source", acquire
        ):
            with self.assertRaisesRegex(RuntimeError, "lease transition"):
                self.copy()
        self.assertFalse((self.root / "runtime/restore-in-progress.json").exists())
        self.assertEqual(self.source.read_bytes(), b"EXTERNAL CHANGE DURING GAP")
        self.assert_protected()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_cancel_after_retention_keeps_missing_live_source_and_guard(self):
        from creator_loop.update_preparation import _journal

        cancelled = False

        def journal(path, record):
            nonlocal cancelled
            _journal(path, record)
            if record["phase"] == "CORRUPT_SOURCE_RETAINED":
                cancelled = True

        with patch("creator_loop.corrupt_restore_copy._journal", journal):
            with self.assertRaises(InterruptedError):
                self.copy(cancelled=lambda: cancelled)
        self.assertFalse(self.source.exists())
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assert_retained_or_original()

    @unittest.skipUnless(sys.platform == "win32", "native guarded Windows copy")
    def test_actual_crash_before_guard_leaves_original_source_and_no_copy(self):
        code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_restore_copy as module
original=module._publish_new_manifest
def crash(*args):
    original(*args)
    os._exit(32)
module._publish_new_manifest=crash
module.copy_corrupt_restore(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),
    reviewed_preparation=sys.argv[5],reviewed_identity=sys.argv[6],confirm_lost_changes=True)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.root),
                str(self.manifest),
                str(self.fixture.fixture.installation),
                str(self.fixture.candidate),
                self.preparation_sha,
                self.fixture.assessment["assessment_identity"],
            ],
            env={**os.environ, "PYTHONPATH": "app"},
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 32, result.stderr)
        self.assertFalse((self.root / "runtime/restore-in-progress.json").exists())
        self.assert_retained_or_original()
