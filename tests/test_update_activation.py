"""Activation pointer/health locks and failure compatibility, with no DB restore."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_update_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.database import SCHEMA_VERSION
from creator_loop.update_activation import activate_prepared_update, active_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_health import readonly_health


class UpdateActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        self.journal = self.fixture._prepare()
        self.record = json.loads(self.journal.read_text())
        self.candidate = Path(self.record["candidate_directory"])
        self.pointer = self.installation / "active-installation.json"
        self.before = self.fixture.path.read_bytes()

    def _activate(self):
        return activate_prepared_update(self.root, self.journal, self.installation)

    def _health(self, executable, root, log_directory, **kwargs):
        self.assertEqual(executable, self.candidate / "CreatorLoop/CreatorLoop.exe")
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "PENDING_HEALTH"
        )
        with self.assertRaises(DataRootBusy):
            with AppDataLock(root):
                self.fail("Activation released app lock before health")
        with closing(sqlite3.connect(self.fixture.path, timeout=0.02)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("BEGIN IMMEDIATE")
        log_directory.mkdir()
        (log_directory / "fixture.log").write_text(
            "bounded native health is tested separately"
        )
        return readonly_health(root)

    def _assert_preserved(self):
        self.assertEqual(self.fixture.path.read_bytes(), self.before)
        self.fixture._assert_source(SCHEMA_VERSION)
        self.assertTrue((self.root / "backups" / self.record["backup_id"]).is_dir())
        self.assertTrue(self.candidate.is_dir())

    def _previous(self, compatible=True):
        # A separately staged previous installation and its explicit read range.
        self.fixture._legacy(SCHEMA_VERSION)
        journal = self.fixture._prepare()
        previous = Path(json.loads(journal.read_text())["candidate_directory"])
        manifest_path = previous / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if not compatible:
            manifest["schema_to"] = SCHEMA_VERSION - 1
            manifest["schema_read_min"] = SCHEMA_VERSION - 1
            manifest["schema_read_max"] = SCHEMA_VERSION - 1
            manifest_path.write_text(json.dumps(manifest))
        pointer = {
            "pointer_format": 1,
            "status": "ACTIVE",
            "candidate_name": previous.name,
            "manifest_sha256": _digest(manifest_path),
            "update_id": "previous",
        }
        self.pointer.write_text(json.dumps(pointer))
        self.before = self.fixture.path.read_bytes()
        return previous, pointer

    def test_activate_pointer_then_readonly_health_under_continuous_locks(self):
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=self._health
        ):
            self.assertEqual(self._activate(), self.candidate)
        candidate, _ = active_candidate(self.installation, SCHEMA_VERSION)
        self.assertEqual(candidate, self.candidate)
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["phase"], "COMPLETED")
        self.assertFalse(record["activation_pending"])
        self._assert_preserved()

    def test_health_failure_without_previous_blocks_pointer_and_retains_db(self):
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=TimeoutError("private fixture"),
        ):
            with self.assertRaises(TimeoutError):
                self._activate()
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
        )
        with self.assertRaisesRegex(RuntimeError, "health-validated"):
            active_candidate(self.installation, SCHEMA_VERSION)
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["phase"], "HEALTH_FAILED")
        self.assertNotIn("private fixture", json.dumps(record))
        self._assert_preserved()

    def test_health_failure_can_return_only_to_verified_schema_compatible_previous(
        self,
    ):
        previous, pointer = self._previous()
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=RuntimeError("fixture"),
        ):
            with self.assertRaises(RuntimeError):
                self._activate()
        self.assertEqual(json.loads(self.pointer.read_text()), pointer)
        self.assertEqual(
            active_candidate(self.installation, SCHEMA_VERSION)[0], previous
        )
        self._assert_preserved()

    def test_incompatible_previous_is_retained_but_not_activated_on_failure(self):
        previous, _ = self._previous(compatible=False)
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=RuntimeError("fixture"),
        ):
            with self.assertRaises(RuntimeError):
                self._activate()
        self.assertTrue(previous.is_dir())
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
        )
        self._assert_preserved()

    def test_previous_changed_during_failed_health_is_not_selected_again(self):
        previous, _ = self._previous()

        def failure(*args, **kwargs):
            (previous / "CreatorLoop/CreatorLoop.exe").write_bytes(b"changed")
            raise RuntimeError("fixture changed previous")

        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=failure
        ):
            with self.assertRaises(RuntimeError):
                self._activate()
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
        )
        self._assert_preserved()

    def test_changed_candidate_or_backup_refuses_before_pointer_or_health(self):
        executable = self.candidate / "CreatorLoop/CreatorLoop.exe"
        original = executable.read_bytes()
        for changed in (
            executable,
            self.root / "backups" / self.record["backup_id"] / "creator_loop.sqlite3",
        ):
            with self.subTest(path=changed):
                data = changed.read_bytes()
                changed.write_bytes(data + b"changed")
                with patch("creator_loop.update_activation.run_health_check") as health:
                    with self.assertRaises(ValueError):
                        self._activate()
                    health.assert_not_called()
                changed.write_bytes(data)
                self.assertFalse(self.pointer.exists())
        self.assertEqual(executable.read_bytes(), original)
        self._assert_preserved()

    def test_runtime_record_and_missing_media_refuse_without_activation(self):
        stale = self.root / "runtime/stale-worker.json"
        stale.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "workers require recovery"):
            self._activate()
        stale.unlink()
        self.fixture.media.unlink()
        with self.assertRaisesRegex(RuntimeError, "Storage reference"):
            self._activate()
        self.assertFalse(self.pointer.exists())
        self.assertEqual(self.fixture.path.read_bytes(), self.before)

    def test_pending_crash_phase_requires_recovery_instead_of_blind_retry(self):
        self.record["phase"] = "ACTIVATED_PENDING_HEALTH"
        self.journal.write_text(json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, "prepared committed"):
            self._activate()
        self.assertFalse(self.pointer.exists())
        self._assert_preserved()

    def test_post_health_metadata_failure_does_not_claim_success_or_restore_db(self):
        from creator_loop.update_activation import _journal

        def journal(path, record):
            if path == self.journal and record["phase"] == "COMPLETED":
                raise OSError("fixture metadata failure")
            _journal(path, record)

        with (
            patch("creator_loop.update_activation._journal", side_effect=journal),
            patch(
                "creator_loop.update_activation.run_health_check",
                side_effect=self._health,
            ),
        ):
            with self.assertRaises(OSError):
                self._activate()
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
        )
        self.assertEqual(json.loads(self.journal.read_text())["phase"], "HEALTH_FAILED")
        self._assert_preserved()
