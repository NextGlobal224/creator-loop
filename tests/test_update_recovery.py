"""Actual crash/SQLite state, current-state backups and fresh activation recovery."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_update_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.database import (
    SCHEMA_VERSION,
    _migrate_locked,
    open_readonly,
    validate,
)
from creator_loop.update_activation import active_candidate
from creator_loop.update_backup import _backup_from_guard, _digest
from creator_loop.update_health import readonly_health
from creator_loop.update_preparation import _journal
from creator_loop.update_recovery import inspect_update, resume_update


class UpdateRecoveryTests(unittest.TestCase):
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
        self.original_backup = self.root / "backups" / self.record["backup_id"]
        self.original_backup_bytes = (
            self.original_backup / "creator_loop.sqlite3"
        ).read_bytes()

    def _save(self):
        self.journal.write_text(json.dumps(self.record), encoding="utf-8")

    def _inspect(self):
        return inspect_update(self.root, self.journal, self.installation)

    def _assert_locks(self):
        with self.assertRaises(DataRootBusy):
            with AppDataLock(self.root):
                self.fail("Recovery released app lock")
        with closing(sqlite3.connect(self.fixture.path, timeout=0.02)) as other:
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")

    def _health(self, executable, root, log_directory, **kwargs):
        self.assertEqual(executable, self.candidate / "CreatorLoop/CreatorLoop.exe")
        self._assert_locks()
        log_directory.mkdir()
        return readonly_health(root)

    def _resume(self):
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=self._health
        ):
            return resume_update(self.root, self.journal, self.installation)

    def _preserved(self):
        self.fixture._assert_source(SCHEMA_VERSION)
        self.assertEqual(
            (self.original_backup / "creator_loop.sqlite3").read_bytes(),
            self.original_backup_bytes,
        )
        self.assertTrue(self.candidate.is_dir())

    def test_inspect_actual_target_ignores_false_commit_flag_without_writes(self):
        self.record.update(phase="MIGRATION_VALIDATED", migration_committed=False)
        self._save()
        before = self.fixture.path.read_bytes(), self.journal.read_bytes()
        evidence = self._inspect()
        self.assertEqual(evidence["actual_schema"], SCHEMA_VERSION)
        self.assertEqual(evidence["commit_outcome"], "not_inferred_from_journal")
        self.assertEqual(evidence["next_action"], "recheck_activation_at_target")
        self.assertTrue(evidence["recovery_ready"])
        self.assertEqual(
            (self.fixture.path.read_bytes(), self.journal.read_bytes()), before
        )
        self.assertFalse(self.pointer.exists())

    def test_every_supported_actual_schema_preserves_new_data_and_old_backups(self):
        original_record = self.record.copy()
        for version in range(1, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                self.fixture._legacy(version)
                with closing(sqlite3.connect(self.fixture.path)) as db:
                    db.execute(
                        "INSERT INTO assets VALUES (?,?,?,?,?)",
                        ("new", "TEXT", "after backup", preparation_fixture.T, None),
                    )
                    db.commit()
                self.record = original_record.copy()
                self.record.update(phase="FAILED_POST_COMMIT", migration_committed=True)
                self._save()
                self.assertEqual(self._inspect()["actual_schema"], version)
                self.assertEqual(self._resume(), self.candidate)
                resumed = json.loads(self.journal.read_text())
                self.assertEqual(resumed["phase"], "COMPLETED")
                self.assertTrue(resumed["recovery_commit_completed"])
                self.assertEqual(resumed["recovery_observed_schema"], version)
                self.assertNotEqual(resumed["backup_id"], original_record["backup_id"])
                snapshot = (
                    self.root
                    / "backups"
                    / resumed["backup_id"]
                    / "creator_loop.sqlite3"
                )
                with closing(open_readonly(snapshot.resolve())) as db:
                    validate(db, expected_version=version)
                    self.assertEqual(
                        db.execute(
                            "SELECT display_name FROM assets WHERE asset_id='new'"
                        ).fetchone()[0],
                        "after backup",
                    )
                with closing(open_readonly(self.fixture.path.resolve())) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM assets WHERE asset_id='new'"
                        ).fetchone()[0],
                        1,
                    )
                self._preserved()

    def test_backup_migration_and_health_hold_real_app_and_writer_locks(self):
        def backup(root, db, timeout):
            self._assert_locks()
            return _backup_from_guard(root, db, timeout)

        def migrate(db, version):
            self._assert_locks()
            _migrate_locked(db, version)

        with (
            patch(
                "creator_loop.update_recovery._backup_from_guard", side_effect=backup
            ),
            patch("creator_loop.update_recovery._migrate_locked", side_effect=migrate),
        ):
            self._resume()
        self._preserved()

    def test_actual_process_crash_before_and_after_commit_recovers_from_sqlite(self):
        for phase, expected_version in (
            ("MIGRATION_VALIDATED", 1),
            ("PREPARED", SCHEMA_VERSION),
        ):
            with self.subTest(crash_at=phase):
                self.fixture._legacy(1)
                prior = set((self.root / "manifests").glob("update-*.json"))
                code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from creator_loop.update_preparation import prepare_update, _journal
def write(path, record):
    if record['phase'] == sys.argv[5]:
        os._exit(17)
    _journal(path, record)
with patch('creator_loop.update_preparation._journal', side_effect=write):
    prepare_update(*map(Path, sys.argv[1:5]))
"""
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(self.fixture.archive),
                        str(self.fixture.manifest_path),
                        str(self.installation),
                        phase,
                    ],
                    env=env,
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(child.returncode, 17, child.stderr)
                created = set((self.root / "manifests").glob("update-*.json")) - prior
                self.assertEqual(len(created), 1)
                self.journal = created.pop()
                self.record = json.loads(self.journal.read_text())
                self.candidate = Path(self.record["candidate_directory"])
                self.assertFalse(self.record["migration_committed"])
                self.assertEqual(self._inspect()["actual_schema"], expected_version)
                self._resume()
                self._preserved()

    def test_changed_candidate_manifest_backup_and_missing_binding_refuse_without_mutation(
        self,
    ):
        manifest_path = self.candidate / "release-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        backup_path = self.original_backup / "creator_loop.sqlite3"
        for change in ("manifest", "backup", "binding", "candidate"):
            with self.subTest(change=change):
                saved = self.record.copy()
                executable = self.candidate / "CreatorLoop/CreatorLoop.exe"
                executable_bytes = executable.read_bytes()
                if change == "manifest":
                    manifest = json.loads(manifest_bytes)
                    manifest["release_notes"] = "altered"
                    manifest_path.write_text(json.dumps(manifest))
                elif change == "backup":
                    backup_path.write_bytes(self.original_backup_bytes + b"altered")
                elif change == "binding":
                    self.record.pop("candidate_manifest_identity")
                    self._save()
                else:
                    executable.write_bytes(b"altered")
                journal_before = self.journal.read_bytes()
                db_before = self.fixture.path.read_bytes()
                backups_before = set((self.root / "backups").iterdir())
                self.assertFalse(self._inspect()["recovery_ready"])
                with patch("creator_loop.update_activation.run_health_check") as health:
                    with self.assertRaises(ValueError):
                        resume_update(self.root, self.journal, self.installation)
                    health.assert_not_called()
                self.assertEqual(self.journal.read_bytes(), journal_before)
                self.assertEqual(self.fixture.path.read_bytes(), db_before)
                self.assertEqual(set((self.root / "backups").iterdir()), backups_before)
                self.record = saved
                self._save()
                manifest_path.write_bytes(manifest_bytes)
                backup_path.write_bytes(self.original_backup_bytes)
                executable.write_bytes(executable_bytes)

    def test_recovery_process_crash_after_its_commit_keeps_actual_schema_and_retries(
        self,
    ):
        self.fixture._legacy(1)
        code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from creator_loop.update_preparation import _journal
from creator_loop.update_recovery import resume_update
def write(path, record):
    if record['phase'] == 'PREPARED':
        os._exit(19)
    _journal(path, record)
with patch('creator_loop.update_recovery._journal', side_effect=write):
    resume_update(*map(Path, sys.argv[1:4]))
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.root),
                str(self.journal),
                str(self.installation),
            ],
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(child.returncode, 19, child.stderr)
        interrupted = json.loads(self.journal.read_text())
        self.assertEqual(interrupted["phase"], "RECOVERY_MIGRATION_VALIDATED")
        self.assertFalse(interrupted["migration_committed"])
        self.assertEqual(self._inspect()["actual_schema"], SCHEMA_VERSION)
        retained = self.root / "backups" / interrupted["backup_id"]
        self._resume()
        self.assertTrue(retained.is_dir())
        self._preserved()

    def test_activation_rechecks_lock_gap_instead_of_using_stale_connection(self):
        from creator_loop.update_activation import activate_prepared_update

        def competing_app(root, journal, installation):
            with AppDataLock(root):
                return activate_prepared_update(root, journal, installation)

        with patch(
            "creator_loop.update_recovery.activate_prepared_update",
            side_effect=competing_app,
        ):
            with self.assertRaises(DataRootBusy):
                self._resume()
        self.assertEqual(json.loads(self.journal.read_text())["phase"], "PREPARED")
        self.assertFalse(self.pointer.exists())
        self._resume()
        self._preserved()

    def test_invalid_history_or_future_schema_refuses_without_backup_or_pointer(self):
        for change in ("history", "future"):
            with self.subTest(change=change):
                self.fixture._legacy(SCHEMA_VERSION)
                with closing(sqlite3.connect(self.fixture.path)) as db:
                    if change == "history":
                        db.execute("UPDATE schema_migrations SET checksum='bad'")
                    else:
                        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
                    db.commit()
                before = (
                    self.journal.read_bytes(),
                    set((self.root / "backups").iterdir()),
                )
                with self.assertRaises(RuntimeError):
                    self._resume()
                self.assertEqual(
                    (self.journal.read_bytes(), set((self.root / "backups").iterdir())),
                    before,
                )
                self.assertFalse(self.pointer.exists())

    def test_missing_storage_runtime_records_and_queued_run_block_resume(self):
        stale = self.root / "runtime/unknown-worker.json"
        stale.write_text("{}")
        self.assertEqual(self._inspect()["runtime_records_pending"], 1)
        with self.assertRaisesRegex(RuntimeError, "workers require recovery"):
            self._resume()
        stale.unlink()
        self.fixture.media.unlink()
        self.assertFalse(self._inspect()["storage_valid"])
        with self.assertRaisesRegex(RuntimeError, "Storage reference"):
            self._resume()
        self.fixture.media.write_bytes(b"original bytes")
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute(
                "INSERT INTO processing_runs(run_id,asset_id,task_type,status,tool_name,tool_version,created_at) VALUES (?,?,?,?,?,?,?)",
                ("r", "a", "fixture", "QUEUED", "fake", "1", preparation_fixture.T),
            )
            db.commit()
        self.assertEqual(self._inspect()["active_processing_runs"], 1)
        with self.assertRaisesRegex(RuntimeError, "workers require recovery"):
            self._resume()
        self.assertFalse(self.pointer.exists())

    def test_uncommitted_failure_rolls_back_current_db_and_keeps_both_backups(self):
        self.fixture._legacy(1)
        before = self.fixture.path.read_bytes()

        def migrate(db, version):
            _migrate_locked(db, version)
            raise RuntimeError("private fixture detail")

        with patch("creator_loop.update_recovery._migrate_locked", side_effect=migrate):
            with self.assertRaises(RuntimeError):
                self._resume()
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["phase"], "RECOVERY_FAILED_ROLLED_BACK")
        self.assertFalse(record["migration_committed"])
        self.assertNotIn("private fixture", json.dumps(record))
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertTrue((self.root / "backups" / record["backup_id"]).is_dir())
        self.assertTrue(self.original_backup.is_dir())
        self.assertFalse(self.pointer.exists())
        self._resume()
        self._preserved()

    def test_post_commit_journal_failure_remains_recoverable_without_restore(self):
        def write(path, record):
            if record["phase"] == "PREPARED":
                raise OSError("private failed journal")
            _journal(path, record)

        with patch("creator_loop.update_recovery._journal", side_effect=write):
            with self.assertRaises(OSError):
                self._resume()
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["phase"], "RECOVERY_FAILED_POST_COMMIT")
        self.assertTrue(record["migration_committed"])
        self.assertTrue(self._inspect()["recovery_ready"])
        self._resume()
        self._preserved()

    def test_pending_pointer_recovers_previous_compatible_pointer_on_health_failure(
        self,
    ):
        self.fixture._legacy(SCHEMA_VERSION)
        second_journal = self.fixture._prepare()
        previous = Path(json.loads(second_journal.read_text())["candidate_directory"])
        saved = {
            "pointer_format": 1,
            "status": "ACTIVE",
            "candidate_name": previous.name,
            "manifest_sha256": _digest(previous / "release-manifest.json"),
            "update_id": "previous",
        }
        self.record.update(
            phase="ACTIVATED_PENDING_HEALTH", previous_active_pointer=saved
        )
        self._save()
        self.pointer.write_text(
            json.dumps(
                {
                    "pointer_format": 1,
                    "status": "PENDING_HEALTH",
                    "candidate_name": self.candidate.name,
                    "update_id": self.record["update_id"],
                }
            )
        )
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=TimeoutError("fixture"),
        ):
            with self.assertRaises(TimeoutError):
                resume_update(self.root, self.journal, self.installation)
        self.assertEqual(json.loads(self.pointer.read_text()), saved)
        self.assertEqual(
            active_candidate(self.installation, SCHEMA_VERSION)[0], previous
        )
        self._preserved()

    def test_no_recorded_candidate_never_guesses_staged_version(self):
        self.record.update(phase="FAILED_ROLLED_BACK", candidate_directory=None)
        self._save()
        with self.assertRaisesRegex(ValueError, "explicit package"):
            self._resume()
        self.assertFalse(self._inspect()["recovery_ready"])
        self.assertTrue(self.candidate.is_dir())
        self.assertFalse(self.pointer.exists())

    def test_completed_update_requires_metadata_recheck_instead_of_reactivation(self):
        self._resume()
        before = (
            self.pointer.read_bytes(),
            self.journal.read_bytes(),
            self.fixture.path.read_bytes(),
        )
        self.assertFalse(self._inspect()["recovery_ready"])
        with self.assertRaises(ValueError):
            self._resume()
        self.assertEqual(
            (
                self.pointer.read_bytes(),
                self.journal.read_bytes(),
                self.fixture.path.read_bytes(),
            ),
            before,
        )

    def test_cli_inspection_and_contention_are_bounded_and_do_not_initialize_missing_db(
        self,
    ):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--inspect-update",
            str(self.journal),
            "--installation-root",
            str(self.installation),
        ]
        result = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["actual_schema"], SCHEMA_VERSION)
        with AppDataLock(self.root):
            blocked = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        incompatible = subprocess.run(
            command + ["--smoke"], env=env, capture_output=True, timeout=10
        )
        self.assertEqual(incompatible.returncode, 2)
        self.fixture.path.unlink()
        missing = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(missing.returncode, 4)
        self.assertFalse(self.fixture.path.exists())


if __name__ == "__main__":
    unittest.main()
