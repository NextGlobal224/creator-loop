"""Actual damaged source, validated SQLite backup/candidate and no restore."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

import test_update_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_restore_assessment import assess_corrupt_restore
from creator_loop.restore_assessment import assess_restore


class CorruptRestoreAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        self.journal = self.fixture._prepare()
        record = json.loads(self.journal.read_text())
        self.candidate = Path(record["candidate_directory"])
        self.backup_id = record["backup_id"]
        self.snapshot = self.root / "backups" / self.backup_id / "creator_loop.sqlite3"
        self.private = b"PRIVATE DAMAGED SOURCE\x00\xff" * 1024
        self.fixture.path.write_bytes(self.private)

    def assess(self):
        return assess_corrupt_restore(
            self.root, self.backup_id, self.installation, self.candidate
        )

    def test_valid_backup_candidate_media_review_preserves_source_and_does_not_grant_apply(
        self,
    ):
        before = (
            self.fixture.path.read_bytes(),
            self.snapshot.read_bytes(),
            self.journal.read_bytes(),
            self.fixture.media.read_bytes(),
        )
        result = self.assess()
        self.assertEqual(result["source_kind"], "DAMAGED_SQLITE")
        self.assertIsNone(result["current_counts"])
        self.assertIsNone(result["current_schema"])
        self.assertFalse(result["current_changes_assessable"])
        self.assertFalse(result["raw_source_preserved"])
        self.assertFalse(result["apply_supported"])
        self.assertFalse(result["restored"])
        self.assertTrue(result["requires_lost_changes_confirmation"])
        self.assertEqual(result["media_issue_count"], 0)
        self.assertTrue(result["backup_counts"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(
            before,
            (
                self.fixture.path.read_bytes(),
                self.snapshot.read_bytes(),
                self.journal.read_bytes(),
                self.fixture.media.read_bytes(),
            ),
        )
        self.assertEqual(
            result["assessment_identity"], self.assess()["assessment_identity"]
        )
        with self.assertRaises(sqlite3.Error):
            assess_restore(self.root, self.backup_id, self.installation, self.candidate)

    def test_changed_damage_or_media_invalidates_review_and_missing_media_is_visible(
        self,
    ):
        first = self.assess()
        self.fixture.path.write_bytes(self.private + b"changed raw bytes")
        second = self.assess()
        self.assertNotEqual(first["assessment_identity"], second["assessment_identity"])
        self.fixture.media.unlink()  # own fixture only
        third = self.assess()
        self.assertEqual(third["media_issue_count"], 1)
        self.assertTrue(third["requires_media_issues_confirmation"])
        self.assertNotEqual(second["assessment_identity"], third["assessment_identity"])
        self.assertFalse(third["restored"])

    def test_tampered_backup_and_candidate_refuse_without_changing_source(self):
        backup_bytes = self.snapshot.read_bytes()
        self.snapshot.write_bytes(backup_bytes + b"altered")
        with self.assertRaises(ValueError):
            self.assess()
        self.snapshot.write_bytes(backup_bytes)  # own fixture only
        candidate_exe = self.candidate / "CreatorLoop/CreatorLoop.exe"
        candidate_exe.write_bytes(b"tampered fixture")
        with self.assertRaises(ValueError):
            self.assess()
        self.assertEqual(self.fixture.path.read_bytes(), self.private)

    def test_app_lock_contention_and_readable_db_cannot_use_damage_route(self):
        with AppDataLock(self.root):
            with self.assertRaises(DataRootBusy):
                self.assess()
        self.fixture.path.write_bytes(self.snapshot.read_bytes())
        with self.assertRaisesRegex(ValueError, "Readable database"):
            self.assess()

    def test_actual_cli_valid_invalid_combination_missing_source_no_create(self):
        environment = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--inspect-corrupt-restore",
            self.backup_id,
            "--restore-candidate",
            str(self.candidate),
            "--installation-root",
            str(self.installation),
        ]
        result = subprocess.run(
            command, env=environment, capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["apply_supported"])
        invalid = subprocess.run(
            command + ["--confirm-lost-changes"],
            env=environment,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(self.fixture.path.read_bytes(), self.private)
        self.fixture.path.unlink()  # own synthetic fixture only
        missing = subprocess.run(
            command, env=environment, capture_output=True, timeout=15
        )
        self.assertEqual(missing.returncode, 4)
        self.assertFalse(self.fixture.path.exists())
        self.assertNotIn(b"PRIVATE", missing.stderr)
