"""Real BackupAPI staging, all supported migrations, consent and failure retention."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_update_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_restore_assessment import assess_corrupt_restore
from creator_loop.corrupt_restore_preparation import prepare_corrupt_restore
from creator_loop.corrupt_source_preservation import preserve_corrupt_source
from creator_loop.database import SCHEMA_VERSION, open_readonly, validate


class CorruptRestorePreparationTests(unittest.TestCase):
    def setUp(self):
        self.setup_fixture(1)

    def setup_fixture(self, version):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._legacy(version)
        self.root = self.fixture.root.resolve()
        journal = self.fixture._prepare()
        record = json.loads(journal.read_text())
        self.candidate = Path(record["candidate_directory"])
        self.backup_id = record["backup_id"]
        self.snapshot = self.root / "backups" / self.backup_id / "creator_loop.sqlite3"
        self.fixture.path.write_bytes(b"PRIVATE DAMAGED DB\x00\xff" * 1024)
        (self.root / "creator_loop.sqlite3-wal").write_bytes(b"KEEP WAL BYTES")
        self.assessment = self.assess()
        self.raw_manifest = preserve_corrupt_source(
            self.root, self.assessment["damage"]["damage_identity"]
        )
        self.before = self.protected_bytes()

    def assess(self):
        return assess_corrupt_restore(
            self.root, self.backup_id, self.fixture.installation, self.candidate
        )

    def protected_bytes(self):
        paths = [
            self.fixture.path,
            self.root / "creator_loop.sqlite3-wal",
            self.snapshot,
            self.fixture.media,
            self.fixture.old / "keep.txt",
            self.candidate / "CreatorLoop/CreatorLoop.exe",
        ]
        paths.extend(self.raw_manifest.parent.iterdir())
        return {str(path): path.read_bytes() for path in paths if path.is_file()}

    def prepare(self, **kwargs):
        options = {
            "reviewed_identity": self.assessment["assessment_identity"],
            "confirm_lost_changes": True,
        }
        options.update(kwargs)
        return prepare_corrupt_restore(
            self.root,
            self.backup_id,
            self.fixture.installation,
            self.candidate,
            self.raw_manifest,
            **options,
        )

    def assert_unchanged(self):
        self.assertEqual(self.protected_bytes(), self.before)
        self.assertEqual(
            {path.name for path in (self.root / "runtime").iterdir()},
            {"app-data.lock"},
        )

    def test_every_supported_schema_has_consistent_migrated_separate_db(self):
        for version in range(1, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                if version != 1:
                    self.setup_fixture(version)
                manifest = self.prepare()
                record = json.loads(manifest.read_text())
                staged = manifest.parent / record["staged_database"]
                self.assertEqual(record["schema_from"], version)
                self.assertEqual(record["schema_to"], SCHEMA_VERSION)
                self.assertEqual(record["phase"], "CORRUPT_RESTORE_STAGED")
                self.assertFalse(record["apply_authorized"])
                self.assertFalse(record["restored"])
                self.assertFalse(record["activated"])
                self.assertFalse(record["current_changes_assessable"])
                self.assertTrue(record["raw_source_preserved"])
                self.assertEqual(
                    hashlib.sha256(staged.read_bytes()).hexdigest(),
                    record["staged_database_sha256"],
                )
                with closing(open_readonly(staged.resolve())) as db:
                    validate(db, expected_version=SCHEMA_VERSION)
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM assets").fetchone(), (1,)
                    )
                    self.assertEqual(
                        db.execute("SELECT storage_key FROM asset_files").fetchone(),
                        ("storage/originals/text.txt",),
                    )
                self.assertEqual(
                    {p.name for p in manifest.parent.iterdir()},
                    {"restored.sqlite3", "preparation.json"},
                )
                self.assertNotIn("PRIVATE", manifest.read_text())
                self.assert_unchanged()

    def test_missing_consent_stale_review_and_invalid_budget_do_not_create_stage(self):
        for options in (
            {"confirm_lost_changes": False},
            {"confirm_lost_changes": 1},
            {"reviewed_identity": "0" * 64},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("inf")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(**options)
        self.assertFalse(list((self.root / "backups").glob("corrupt-restore-*")))
        self.assert_unchanged()

    def test_changed_source_or_raw_archive_refuses_before_stage_creation(self):
        self.fixture.path.write_bytes(b"CHANGED DAMAGE")
        with self.assertRaises(ValueError):
            self.prepare()
        self.fixture.path.write_bytes(self.before[str(self.fixture.path)])
        raw = self.raw_manifest.parent / "creator_loop.sqlite3"
        raw.write_bytes(b"TAMPERED ARCHIVE")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(raw.read_bytes(), b"TAMPERED ARCHIVE")
        self.assertFalse(list((self.root / "backups").glob("corrupt-restore-*")))

    def test_media_issue_requires_fresh_acknowledgement_and_never_repairs_media(self):
        self.fixture.media.write_bytes(b"CHANGED MEDIA")
        with self.assertRaises(ValueError):
            self.prepare(confirm_media_issues=True)
        self.assessment = self.assess()
        self.assertGreater(self.assessment["media_issue_count"], 0)
        with self.assertRaises(ValueError):
            self.prepare()
        self.before = self.protected_bytes()
        record = json.loads(self.prepare(confirm_media_issues=True).read_text())
        self.assertTrue(record["confirmed_media_issues"])
        self.assertFalse(record["restored"])
        self.assert_unchanged()

    def test_app_contention_cancel_disk_and_pending_runtime_refuse_safely(self):
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            self.prepare()
        with self.assertRaises(InterruptedError):
            self.prepare(cancelled=lambda: True)
        with (
            patch(
                "creator_loop.corrupt_restore_preparation.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            self.assertRaises(OSError),
        ):
            self.prepare()
        pending = self.root / "runtime/restore-in-progress.json"
        pending.write_bytes(b"KEEP UNKNOWN RUNTIME")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(pending.read_bytes(), b"KEEP UNKNOWN RUNTIME")
        pending.unlink()  # own fixture only
        self.assertFalse(list((self.root / "backups").glob("corrupt-restore-*")))
        self.assert_unchanged()

    def test_migration_failure_keeps_partial_db_and_original_inputs(self):
        with (
            patch(
                "creator_loop.corrupt_restore_preparation._migrate_locked",
                side_effect=RuntimeError("fixture migration fails"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.prepare()
        stages = list((self.root / "backups").glob("corrupt-restore-*"))
        self.assertEqual(len(stages), 1)
        self.assertFalse((stages[0] / "preparation.json").exists())
        with closing(open_readonly((stages[0] / "restored.sqlite3").resolve())) as db:
            validate(db, expected_version=1)
        self.assert_unchanged()

    def test_manifest_publication_failure_retains_complete_stage_and_pending_evidence(
        self,
    ):
        with (
            patch(
                "creator_loop.corrupt_restore_preparation._publish_new_manifest",
                side_effect=OSError("fixture publish fails"),
            ),
            self.assertRaises(OSError),
        ):
            self.prepare()
        folder = next((self.root / "backups").glob("corrupt-restore-*"))
        self.assertTrue((folder / "restored.sqlite3").is_file())
        self.assertTrue((folder / "preparation.json.pending").is_file())
        self.assertFalse((folder / "preparation.json").exists())
        self.assert_unchanged()

    def test_migration_media_rewrite_rolls_back_and_keeps_source_and_stage(self):
        from creator_loop.database import _migrate_locked

        def migrate(db, version):
            _migrate_locked(db, version)
            db.execute(
                "UPDATE asset_files SET storage_key='storage/originals/unwanted.txt'"
            )

        with patch("creator_loop.corrupt_restore_preparation._migrate_locked", migrate):
            with self.assertRaisesRegex(RuntimeError, "changed media references"):
                self.prepare()
        folder = next((self.root / "backups").glob("corrupt-restore-*"))
        with closing(open_readonly((folder / "restored.sqlite3").resolve())) as db:
            validate(db, expected_version=1)
            self.assertEqual(
                db.execute("SELECT storage_key FROM asset_files").fetchone(),
                ("storage/originals/text.txt",),
            )
        self.assertFalse((folder / "preparation.json").exists())
        self.assert_unchanged()

    def test_cancel_after_database_copy_keeps_partial_stage_and_source(self):
        from creator_loop.database import validate as real_validate

        cancelled = False

        def validate_and_cancel(*args, **kwargs):
            nonlocal cancelled
            real_validate(*args, **kwargs)
            cancelled = True

        with patch(
            "creator_loop.corrupt_restore_preparation.validate", validate_and_cancel
        ):
            with self.assertRaises((InterruptedError, sqlite3.OperationalError)):
                self.prepare(cancelled=lambda: cancelled)
        folder = next((self.root / "backups").glob("corrupt-restore-*"))
        self.assertTrue((folder / "restored.sqlite3").is_file())
        self.assertFalse((folder / "preparation.json").exists())
        self.assert_unchanged()

    def test_stage_mutation_during_publish_is_not_reported_as_success(self):
        from creator_loop.corrupt_source_preservation import _publish_new_manifest

        def publish(temporary, destination, stream):
            _publish_new_manifest(temporary, destination, stream)
            with (temporary.parent / "restored.sqlite3").open("ab") as staged:
                staged.write(b"EXTERNAL STAGE MUTATION")

        with patch(
            "creator_loop.corrupt_restore_preparation._publish_new_manifest", publish
        ):
            with self.assertRaisesRegex(
                RuntimeError, "changed during manifest publication"
            ):
                self.prepare()
        folder = next((self.root / "backups").glob("corrupt-restore-*"))
        self.assertTrue((folder / "preparation.json").is_file())
        self.assertTrue(
            (folder / "restored.sqlite3")
            .read_bytes()
            .endswith(b"EXTERNAL STAGE MUTATION")
        )
        self.assert_unchanged()

    @unittest.skipUnless(sys.platform == "win32", "native retained handles")
    def test_native_source_archive_media_and_stage_pins_remain_through_publish(self):
        from creator_loop.corrupt_source_preservation import _publish_new_manifest

        def publish(temporary, destination, stream):
            for path in (
                self.fixture.path,
                self.root / "creator_loop.sqlite3-wal",
                self.snapshot,
                self.fixture.media,
                self.raw_manifest,
            ):
                with self.assertRaises(OSError):
                    path.write_bytes(b"NEVER WRITE")
                with self.assertRaises(OSError):
                    path.unlink()
            staged = temporary.parent / "restored.sqlite3"
            with self.assertRaises(OSError):
                staged.unlink()
            with self.assertRaises(OSError):
                temporary.parent.rename(temporary.parent.with_name("never rename"))
            _publish_new_manifest(temporary, destination, stream)

        with patch(
            "creator_loop.corrupt_restore_preparation._publish_new_manifest", publish
        ):
            self.prepare()
        self.assert_unchanged()

    def test_actual_process_crash_keeps_partial_and_published_evidence(self):
        for publish_first in (False, True):
            code = """
import os, sys
from pathlib import Path
from creator_loop.corrupt_restore_preparation import prepare_corrupt_restore
import creator_loop.corrupt_restore_preparation as module
original = module._publish_new_manifest
def crash(*args):
    if sys.argv[7] == 'True': original(*args)
    os._exit(27)
module._publish_new_manifest = crash
prepare_corrupt_restore(
    Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]),
    Path(sys.argv[5]), reviewed_identity=sys.argv[6], confirm_lost_changes=True,
)
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(self.root),
                    self.backup_id,
                    str(self.fixture.installation),
                    str(self.candidate),
                    str(self.raw_manifest),
                    self.assessment["assessment_identity"],
                    str(publish_first),
                ],
                env={**os.environ, "PYTHONPATH": "app"},
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 27, result.stderr)
            stages = list((self.root / "backups").glob("corrupt-restore-*"))
            self.assertTrue(
                any(
                    (
                        folder
                        / (
                            "preparation.json"
                            if publish_first
                            else "preparation.json.pending"
                        )
                    ).is_file()
                    for folder in stages
                )
            )
            self.assert_unchanged()

    def test_actual_cli_requires_explicit_inputs_and_never_initializes_missing_source(
        self,
    ):
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--prepare-corrupt-restore",
            self.backup_id,
            "--restore-candidate",
            str(self.candidate),
            "--installation-root",
            str(self.fixture.installation),
            "--raw-source-manifest",
            str(self.raw_manifest),
            "--reviewed-restore",
            self.assessment["assessment_identity"],
            "--confirm-lost-changes",
        ]
        environment = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }

        def run(arguments):
            return subprocess.run(
                arguments, env=environment, capture_output=True, timeout=30
            )

        success = run(command)
        self.assertEqual(success.returncode, 0, success.stderr)
        record = json.loads(success.stdout)
        self.assertFalse(record["restored"])
        self.assertFalse(record["apply_authorized"])
        self.assertTrue((self.root / record["corrupt_restore_preparation"]).is_file())
        for arguments in (
            command + ["--smoke"],
            command[:-1],
            command + ["--apply-restore", self.backup_id],
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(run(arguments).returncode, 2)
        self.assert_unchanged()
        self.fixture.path.unlink()  # own synthetic fixture only
        missing = run(command)
        self.assertEqual(missing.returncode, 4)
        self.assertFalse(self.fixture.path.exists())
        self.assertNotIn(b"PRIVATE", missing.stderr)

    @unittest.skipIf(sys.platform == "win32", "native source lease prevents mutation")
    def test_portable_source_change_during_migration_rejects_without_discarding_bytes(
        self,
    ):
        from creator_loop.database import _migrate_locked

        def migrate(db, version):
            _migrate_locked(db, version)
            self.fixture.path.write_bytes(b"CONCURRENT PORTABLE CHANGE")

        with patch("creator_loop.corrupt_restore_preparation._migrate_locked", migrate):
            with self.assertRaisesRegex(RuntimeError, "source bytes changed"):
                self.prepare()
        self.assertEqual(self.fixture.path.read_bytes(), b"CONCURRENT PORTABLE CHANGE")
        folder = next((self.root / "backups").glob("corrupt-restore-*"))
        self.assertTrue((folder / "restored.sqlite3").is_file())
        self.assertFalse((folder / "preparation.json").exists())
