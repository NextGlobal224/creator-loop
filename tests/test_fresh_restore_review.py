"""Fresh choice is explicit, advisory, byte-preserving and never legacy consent."""

import json
import os
import subprocess
import sys
import unittest

import test_corrupt_restore_copy as copy_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.fresh_restore_review import review_fresh_restore
from creator_loop.restore_assessment import assessment_identity


@unittest.skipUnless(os.name == "nt", "Actual guarded source/stage/native leases")
class FreshRestoreReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation
        self.candidate = self.fixture.fixture.candidate
        self.backup_id = self.fixture.fixture.backup_id
        self.copy = self.fixture.copy()

    def review(self, **changes):
        return review_fresh_restore(
            self.root,
            self.copy,
            changes.get("backup_id", self.backup_id),
            self.installation,
            changes.get("candidate", self.candidate),
        )

    def files(self):
        return {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def test_changed_completed_bundle_is_reviewed_without_mutation_or_apply(self):
        self.fixture.source.write_bytes(b"PRIVATE NEW CURRENT DB")
        (self.root / "creator_loop.sqlite3-journal").write_bytes(b"PRIVATE HOT JOURNAL")
        before = self.files()
        choice = self.review()
        self.assertTrue(choice["apply_supported"])
        self.assertFalse(choice["apply_authorized"])
        self.assertFalse(choice["restored"])
        self.assertFalse(choice["activated"])
        self.assertFalse(choice["consistent_current_backup"])
        self.assertFalse(choice["current_changes_assessable"])
        self.assertIsNone(choice["current_counts"])
        self.assertEqual(choice["assessment_identity"], assessment_identity(choice))
        present = [
            entry for entry in choice["current_source_files"] if entry["present"]
        ]
        self.assertEqual(len(present), 2)
        self.assertTrue(all(entry["inode"] > 0 for entry in present))
        self.assertEqual(self.files(), before)
        self.assertNotIn("PRIVATE", str(choice))

    def test_missing_or_empty_source_is_not_initialized_and_changed_choice_is_stale(
        self,
    ):
        self.fixture.source.write_bytes(b"")
        first = self.review()
        self.fixture.source.unlink()
        before = self.files()
        second = self.review()
        self.assertNotEqual(first["assessment_identity"], second["assessment_identity"])
        self.assertFalse(
            any(entry["present"] for entry in second["current_source_files"])
        )
        self.assertFalse(self.fixture.source.exists())
        self.assertEqual(self.files(), before)

    def test_foreign_backup_candidate_validated_state_or_busy_never_grants_choice(self):
        with self.assertRaisesRegex(ValueError, "Completed physical receipt"):
            self.review()
        self.fixture.source.write_bytes(b"PRIVATE CHANGED COMPLETED")
        before = self.files()
        with self.assertRaisesRegex(ValueError, "Explicit selected bound"):
            self.review(backup_id="f" * 32)
        with self.assertRaisesRegex(ValueError, "Explicit selected bound"):
            self.review(candidate=self.candidate.parent)
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            self.review()
        self.assertEqual(self.files(), before)

    def test_actual_cli_fresh_readonly_and_orphan_or_mixed_flags_never_initialize(self):
        self.fixture.source.write_bytes(b"PRIVATE CHANGED COMPLETED")
        before = self.files()
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--review-completed-copy-restore",
            str(self.copy),
            "--fresh-restore-backup",
            self.backup_id,
            "--installation-root",
            str(self.installation),
            "--restore-candidate",
            str(self.candidate),
        ]
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        result = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        choice = json.loads(result.stdout)
        self.assertTrue(choice["apply_supported"])
        self.assertFalse(choice["apply_authorized"])
        self.assertEqual(choice["assessment_identity"], assessment_identity(choice))
        self.assertEqual(self.files(), before)
        self.assertNotIn(b"PRIVATE", result.stdout + result.stderr)
        mixed = subprocess.run(
            command + ["--confirm-lost-changes"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(mixed.returncode, 2)
        self.assertEqual(self.files(), before)
        missing = self.root / "OWN MISSING DATA ROOT"
        for flags in (
            ["--fresh-restore-backup", self.backup_id, "--smoke"],
            ["--review-completed-copy-restore", str(self.copy)],
        ):
            refused = subprocess.run(
                [sys.executable, "-m", "creator_loop", *flags],
                env={**env, "CREATOR_LOOP_DATA_ROOT": str(missing)},
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertFalse(missing.exists())
