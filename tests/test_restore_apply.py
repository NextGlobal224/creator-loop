"""Confirmed restore preservation, stale reviews and real copy/crash recovery."""

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
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
from creator_loop.restore_apply import _copy_database, apply_restore, recover_restore
from creator_loop.restore_assessment import assess_restore
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.update_health import readonly_health


class RestoreApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        prepared = self.fixture._prepare()
        record = json.loads(prepared.read_text())
        self.backup_id = record["backup_id"]
        self.candidate = Path(record["candidate_directory"])
        self.marker = self.root / "runtime/restore-in-progress.json"
        self.backup_bytes = (
            self.root / "backups" / self.backup_id / "creator_loop.sqlite3"
        ).read_bytes()
        self._add_new_row()
        self.assessment = self._assess()

    def _add_new_row(self):
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (
                    "new",
                    "TEXT",
                    "post-backup private data",
                    preparation_fixture.T,
                    None,
                ),
            )
            db.commit()

    def _assess(self):
        return assess_restore(
            self.root, self.backup_id, self.installation, self.candidate
        )

    def _health(self, executable, root, log_directory, **kwargs):
        self.assertEqual(executable, self.candidate / "CreatorLoop/CreatorLoop.exe")
        self.assertTrue(self.marker.exists())
        with self.assertRaises(DataRootBusy):
            with AppDataLock(root):
                self.fail("Restore health released app lock")
        with closing(sqlite3.connect(self.fixture.path, timeout=0.02)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("BEGIN IMMEDIATE")
        log_directory.mkdir()
        return readonly_health(root)

    def _apply(self, **kwargs):
        return apply_restore(
            self.root,
            self.backup_id,
            self.installation,
            self.candidate,
            reviewed_identity=kwargs.pop(
                "reviewed_identity", self.assessment["assessment_identity"]
            ),
            confirm_lost_changes=kwargs.pop("confirm_lost_changes", True),
            **kwargs,
        )

    def _healthy_apply(self, **kwargs):
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=self._health
        ):
            return self._apply(**kwargs)

    def _journal(self):
        journals = [
            path
            for path in (self.root / "manifests").glob("update-*.json")
            if json.loads(path.read_text()).get("restore_format") == 1
        ]
        self.assertEqual(len(journals), 1)
        return journals[0], json.loads(journals[0].read_text())

    def _assert_original_media_and_backup(self):
        self.assertEqual(self.fixture.media.read_bytes(), b"original bytes")
        self.assertEqual(
            (
                self.root / "backups" / self.backup_id / "creator_loop.sqlite3"
            ).read_bytes(),
            self.backup_bytes,
        )
        self.assertEqual(
            (self.fixture.old / "keep.txt").read_bytes(), b"previous installation"
        )

    def _assert_restored(self):
        self.fixture._assert_source(SCHEMA_VERSION)
        with closing(open_readonly(self.fixture.path.resolve())) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT asset_id FROM assets WHERE asset_id='new'"
                ).fetchone()
            )

    def test_explicit_confirmed_restore_keeps_current_backup_and_newer_media(self):
        newer = self.root / "storage/originals/new-unreferenced.txt"
        newer.write_bytes(b"newer bytes retained")
        self.assertEqual(self._healthy_apply(), self.candidate)
        journal, record = self._journal()
        self.assertEqual(record["phase"], "COMPLETED")
        self.assertTrue(record["confirmed_lost_changes"])
        self.assertFalse(self.marker.exists())
        self.assertIn("restore_completed_at", record)
        current_backup = self.root / "backups" / record["pre_restore_backup_id"]
        metadata = json.loads((current_backup / "backup-manifest.json").read_text())
        self.assertEqual(metadata["storage_reference_check"], "not_checked")
        with closing(
            open_readonly((current_backup / "creator_loop.sqlite3").resolve())
        ) as db:
            validate(db)
            self.assertEqual(
                db.execute(
                    "SELECT display_name FROM assets WHERE asset_id='new'"
                ).fetchone()[0],
                "post-backup private data",
            )
        self.assertNotIn("post-backup private data", journal.read_text())
        self.assertEqual(newer.read_bytes(), b"newer bytes retained")
        self._assert_restored()
        self._assert_original_media_and_backup()

    def test_explicit_consent_required_before_any_backup_or_db_change(self):
        before = self.fixture.path.read_bytes(), set((self.root / "backups").iterdir())
        for consent in (False, None, 1):
            with self.subTest(consent=consent):
                with self.assertRaisesRegex(ValueError, "Explicit lost-change"):
                    self._apply(confirm_lost_changes=consent)
        self.assertEqual(
            (self.fixture.path.read_bytes(), set((self.root / "backups").iterdir())),
            before,
        )
        self.assertFalse(self.marker.exists())

    def test_stale_db_edit_candidate_registry_or_media_review_never_applies(self):
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute(
                "UPDATE assets SET display_name='edited after review' WHERE asset_id='new'"
            )
            db.commit()
        before = self.fixture.path.read_bytes(), set((self.root / "backups").iterdir())
        with self.assertRaisesRegex(ValueError, "stale"):
            self._apply()
        self.assertEqual(
            (self.fixture.path.read_bytes(), set((self.root / "backups").iterdir())),
            before,
        )
        self.assessment = self._assess()
        self.fixture.media.write_bytes(b"x" * len(b"original bytes"))
        with self.assertRaisesRegex(ValueError, "stale"):
            self._apply(confirm_media_issues=True)
        self.assertFalse(self.marker.exists())

    def test_acknowledged_missing_media_keeps_db_backup_and_blocks_unhealthy_activation(
        self,
    ):
        self.fixture.media.unlink()
        self.assessment = self._assess()
        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            self._apply()
        # Acknowledgement does not fabricate successful media health. The DB
        # can be restored, then remains guarded until reference/health recovery.
        with self.assertRaisesRegex(RuntimeError, "Storage reference"):
            self._apply(confirm_media_issues=True)
        journal, record = self._journal()
        self.assertTrue(self.marker.exists())
        self.assertTrue(record["confirmed_media_issues"])
        self.assertTrue(
            (self.root / "backups" / record["pre_restore_backup_id"]).exists()
        )
        with self.assertRaises(RuntimeError):
            require_no_pending_restore(self.root)
        self.fixture.media.write_bytes(b"original bytes")
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=self._health
        ):
            self.assertEqual(
                recover_restore(self.root, journal, self.installation), self.candidate
            )
        self.assertFalse(self.marker.exists())
        self._assert_restored()

    def test_copy_failure_rolls_back_actual_sqlite_and_recovery_clears_original_state_guard(
        self,
    ):
        before = self.fixture.path.read_bytes()
        count = 0

        def copy(reader, target, deadline):
            nonlocal count
            count += 1
            if count == 1:
                return _copy_database(reader, target, deadline)

            def fail(status, remaining, total):
                if status != sqlite3.SQLITE_DONE:
                    raise RuntimeError("injected private copy failure")

            reader.backup(target, pages=1, progress=fail)
            self.fail("Expected interrupted SQLite copy")

        with patch("creator_loop.restore_apply._copy_database", side_effect=copy):
            with self.assertRaises(RuntimeError):
                self._apply()
        journal, record = self._journal()
        self.assertEqual(record["phase"], "RESTORE_OUTCOME_UNCONFIRMED")
        self.assertNotIn("private copy", journal.read_text())
        self.assertTrue(self.marker.exists())
        with closing(open_readonly(self.fixture.path.resolve())) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM assets WHERE asset_id='new'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(self.fixture.path.read_bytes(), before)
        with patch("creator_loop.update_activation.run_health_check") as health:
            self.assertEqual(
                recover_restore(self.root, journal, self.installation), journal
            )
            health.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.assertEqual(
            json.loads(journal.read_text())["phase"], "RESTORE_NOT_APPLIED"
        )
        self._assert_original_media_and_backup()

    def test_live_copy_holds_exclusive_lock_across_its_own_sqlite_transaction(self):
        count = 0

        def copy(reader, target, deadline):
            nonlocal count
            count += 1
            if count == 2:
                for sql in ("SELECT count(*) FROM assets", "BEGIN IMMEDIATE"):
                    with closing(
                        sqlite3.connect(self.fixture.path, timeout=0.02)
                    ) as other:
                        with self.assertRaises(sqlite3.OperationalError):
                            other.execute(sql)
                self.assertFalse(target.in_transaction)
            _copy_database(reader, target, deadline)
            if count == 2:
                with closing(sqlite3.connect(self.fixture.path, timeout=0.02)) as other:
                    with self.assertRaises(sqlite3.OperationalError):
                        other.execute("SELECT count(*) FROM assets")

        with patch("creator_loop.restore_apply._copy_database", side_effect=copy):
            self._healthy_apply()
        self._assert_restored()

    def test_actual_process_exit_before_and_after_copy_resolves_actual_db_without_recopy(
        self,
    ):
        for phase, restored in (
            ("RESTORE_COPY_STARTED", False),
            ("RESTORE_DB_COMMITTED", True),
        ):
            with self.subTest(crash_at=phase):
                self.fixture._legacy(SCHEMA_VERSION)
                self._add_new_row()
                self.assessment = self._assess()
                prior = set((self.root / "manifests").glob("update-*.json"))
                code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from creator_loop.restore_apply import apply_restore
from creator_loop.update_preparation import _journal
def journal(path, record):
    if record.get('phase') == sys.argv[6]:
        os._exit(21)
    _journal(path, record)
with patch('creator_loop.restore_apply._journal', side_effect=journal):
    apply_restore(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), reviewed_identity=sys.argv[5], confirm_lost_changes=True)
"""
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        self.backup_id,
                        str(self.installation),
                        str(self.candidate),
                        self.assessment["assessment_identity"],
                        phase,
                    ],
                    env=env,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(child.returncode, 21, child.stderr)
                journals = set((self.root / "manifests").glob("update-*.json")) - prior
                self.assertEqual(len(journals), 1)
                journal = journals.pop()
                self.assertTrue(self.marker.exists())
                with self.assertRaises(RuntimeError):
                    require_no_pending_restore(self.root)
                with (
                    patch("creator_loop.restore_apply._copy_database") as recopy,
                    patch(
                        "creator_loop.update_activation.run_health_check",
                        side_effect=self._health,
                    ),
                ):
                    outcome = recover_restore(self.root, journal, self.installation)
                    recopy.assert_not_called()
                self.assertFalse(self.marker.exists())
                self.assertEqual(outcome, self.candidate if restored else journal)
                if restored:
                    self._assert_restored()
                else:
                    with closing(open_readonly(self.fixture.path.resolve())) as db:
                        self.assertIsNotNone(
                            db.execute(
                                "SELECT asset_id FROM assets WHERE asset_id='new'"
                            ).fetchone()
                        )
                self._assert_original_media_and_backup()

    def test_post_commit_health_failure_keeps_guard_and_unknown_changed_db_is_not_guessed(
        self,
    ):
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=TimeoutError("private fixture"),
        ):
            with self.assertRaises(TimeoutError):
                self._apply()
        journal, record = self._journal()
        self.assertEqual(record["phase"], "HEALTH_FAILED")
        self.assertTrue(self.marker.exists())
        self.assertNotIn("private fixture", journal.read_text())
        self._assert_restored()
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute(
                "UPDATE assets SET display_name='outside edit' WHERE asset_id='a'"
            )
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "Unknown actual"):
            recover_restore(self.root, journal, self.installation)
        self.assertTrue(self.marker.exists())
        self._assert_original_media_and_backup()

    def test_normal_and_compatible_launch_refuse_guard_before_initialize_or_ui(self):
        self.marker.write_text("{}")
        before = self.fixture.path.read_bytes()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        for args in (("--smoke",), ("--compatible-only", "--smoke")):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, "-m", "creator_loop", *args],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 4)
                self.assertIn(b"explicit recovery", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertTrue(self.marker.exists())

    def test_malformed_recovery_journal_fails_closed_before_activation(self):
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=TimeoutError
        ):
            with self.assertRaises(TimeoutError):
                self._apply()
        journal, record = self._journal()
        before = self.fixture.path.read_bytes()
        for field, value in (
            ("schema_to", True),
            ("candidate_directory", None),
            ("phase", "UNKNOWN"),
            ("pre_restore_backup_id", None),
        ):
            with self.subTest(field=field):
                altered = dict(record)
                altered[field] = value
                journal.write_text(json.dumps(altered))
                with patch("creator_loop.update_activation.run_health_check") as health:
                    with self.assertRaises(ValueError):
                        recover_restore(self.root, journal, self.installation)
                    health.assert_not_called()
                self.assertTrue(self.marker.exists())
                self.assertEqual(self.fixture.path.read_bytes(), before)
        journal.write_text(json.dumps(record))

    def test_activation_rechecks_db_and_media_after_recovery_releases_locks(self):
        from creator_loop.update_activation import activate_prepared_update

        def changed_activation(*args, **kwargs):
            self.fixture.media.write_bytes(b"x" * len(b"original bytes"))
            return activate_prepared_update(*args, **kwargs)

        with (
            patch(
                "creator_loop.restore_apply.activate_prepared_update",
                side_effect=changed_activation,
            ),
            patch("creator_loop.update_activation.run_health_check") as health,
        ):
            with self.assertRaisesRegex(RuntimeError, "Storage reference bytes"):
                self._apply()
            health.assert_not_called()
        journal, _ = self._journal()
        self.assertTrue(self.marker.exists())
        self.fixture.media.write_bytes(b"original bytes")
        self._assert_restored()

        def changed_db_activation(*args, **kwargs):
            with closing(sqlite3.connect(self.fixture.path)) as db:
                db.execute(
                    "UPDATE assets SET display_name='gap edit' WHERE asset_id='a'"
                )
                db.commit()
            return activate_prepared_update(*args, **kwargs)

        with (
            patch(
                "creator_loop.restore_apply.activate_prepared_update",
                side_effect=changed_db_activation,
            ),
            patch("creator_loop.update_activation.run_health_check") as health,
        ):
            with self.assertRaisesRegex(RuntimeError, "Restored DB changed"):
                recover_restore(self.root, journal, self.installation)
            health.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_health_holds_media_read_handles_and_guard_clear_rechecks_gap(self):
        from creator_loop.update_activation import activate_prepared_update

        def health(executable, root, log_directory, **kwargs):
            if os.name == "nt":
                with self.assertRaises(OSError):
                    self.fixture.media.write_bytes(b"changed during health")
            return self._health(executable, root, log_directory, **kwargs)

        def changed_after_health(*args, **kwargs):
            activated = activate_prepared_update(*args, **kwargs)
            self.fixture.media.write_bytes(b"x" * len(b"original bytes"))
            return activated

        with (
            patch(
                "creator_loop.restore_apply.activate_prepared_update",
                side_effect=changed_after_health,
            ),
            patch(
                "creator_loop.update_activation.run_health_check", side_effect=health
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Storage reference bytes"):
                self._apply()
        self.assertTrue(self.marker.exists())
        journal, _ = self._journal()
        self.fixture.media.write_bytes(b"original bytes")
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=health
        ):
            self.assertEqual(
                recover_restore(self.root, journal, self.installation), self.candidate
            )
        self.assertFalse(self.marker.exists())

    def test_invalid_budget_and_app_contention_refuse_without_restore(self):
        with self.assertRaises(ValueError):
            self._apply(timeout_seconds=float("inf"))
        with AppDataLock(self.root):
            with self.assertRaises(DataRootBusy):
                self._apply()
        self.assertFalse(self.marker.exists())

    def test_every_supported_current_schema_and_wal_restore_reopens_at_target(self):
        for version in range(1, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                self.fixture._legacy(version)
                self._add_new_row()
                with closing(sqlite3.connect(self.fixture.path)) as db:
                    self.assertEqual(
                        db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
                    )
                self.assessment = self._assess()
                self._healthy_apply()
                self.assertFalse(self.marker.exists())
                self._assert_restored()
                self._assert_original_media_and_backup()

    def test_staged_snapshot_tamper_blocks_recovery_instead_of_trusting_journal(self):
        with patch(
            "creator_loop.update_activation.run_health_check",
            side_effect=TimeoutError("fixture"),
        ):
            with self.assertRaises(TimeoutError):
                self._apply()
        journal, record = self._journal()
        staged = (
            self.root
            / "backups"
            / record["staged_database_directory"]
            / "restored.sqlite3"
        )
        staged.write_bytes(staged.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            recover_restore(self.root, journal, self.installation)
        self.assertTrue(self.marker.exists())
        self._assert_original_media_and_backup()

    def test_cli_needs_loss_confirmation_and_review_identity_before_any_restore(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--apply-restore",
            self.backup_id,
            "--restore-candidate",
            str(self.candidate),
            "--installation-root",
            str(self.installation),
            "--reviewed-restore",
            self.assessment["assessment_identity"],
        ]
        before = self.fixture.path.read_bytes(), set((self.root / "backups").iterdir())
        refused = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        mixed = subprocess.run(
            command + ["--confirm-lost-changes", "--smoke"],
            env=env,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(mixed.returncode, 2)
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute("UPDATE assets SET display_name='new edit' WHERE asset_id='new'")
            db.commit()
        stale = subprocess.run(
            command + ["--confirm-lost-changes"],
            env=env,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(stale.returncode, 4, stale.stderr)
        self.assertEqual(set((self.root / "backups").iterdir()), before[1])
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
