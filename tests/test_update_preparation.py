"""Backup/stage/migration lock continuity and rollback without rewriting media."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    _connect_write,
    _execute_migration,
    _migrate_locked,
    _migration_sql,
    open_readonly,
    validate,
)
from creator_loop.paths import ensure_data_root
from creator_loop.update_preparation import prepare_update

T = "2026-10-03T00:00:00Z"


class UpdatePreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "Dữ liệu cập nhật"
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        self.media = self.root / "storage/originals/text.txt"
        self.media.write_bytes(b"original bytes")
        self.installation = self.base / "Bản cài"
        self.installation.mkdir()
        self.old = self.installation / "previous"
        self.old.mkdir()
        (self.old / "keep.txt").write_bytes(b"previous installation")
        self.archive = self.base / "update.zip"
        self.manifest_path = self.base / "release-manifest.json"
        executable = b"fixture only; never run"
        self.members = {"CreatorLoop/CreatorLoop.exe": executable}
        self.members.update(
            {
                f"CreatorLoop/_internal/migrations/{name}.sql": _migration_sql(name)[
                    0
                ].encode("utf-8")
                for name in MIGRATIONS
            }
        )
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, content in self.members.items():
                archive.writestr(name, content)
        self.manifest = {
            "manifest_version": 1,
            "app_version": "0.1.0",
            "git_commit": "a" * 40,
            "artifact_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "target": "win-x64",
            "tested_artifact": True,
            "schema_from": 0,
            "schema_to": SCHEMA_VERSION,
            "schema_read_min": SCHEMA_VERSION,
            "schema_read_max": SCHEMA_VERSION,
            "build_time": T,
            "minimum_os": "Windows x64",
            "runtime": "fixture",
            "dependencies": {"fixture": "1"},
            "component_compatibility": {"fixture": "no workers"},
            "provenance": {"fixture": "test"},
            "release_notes": "fixture",
            "recovery_notes": "keep backup",
            "migration_ids": list(MIGRATIONS),
            "migration_checksums": {
                name: _migration_sql(name)[1] for name in MIGRATIONS
            },
            "files": {
                name: {
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in self.members.items()
            },
        }
        self._save()
        self._legacy(1)

    def _save(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _legacy(self, version):
        self.path.unlink(missing_ok=True)
        with closing(_connect_write(self.path)) as db:
            for name in MIGRATIONS[:version]:
                sql, digest = _migration_sql(name)
                _execute_migration(db, sql)
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                    (name, digest, T, "0.1.0"),
                )
            db.execute(f"PRAGMA user_version={version}")
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("a", "TEXT", "fixture", T, None),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "f",
                    "a",
                    "ORIGINAL",
                    "storage/originals/text.txt",
                    hashlib.sha256(self.media.read_bytes()).hexdigest(),
                    14,
                    "text/plain",
                    T,
                ),
            )
            db.commit()

    def _prepare(self):
        return prepare_update(
            self.root, self.archive, self.manifest_path, self.installation
        )

    def _assert_source(self, version):
        with closing(open_readonly(self.path.resolve())) as db:
            validate(db, expected_version=version)
            self.assertEqual(
                db.execute("SELECT storage_key FROM asset_files").fetchone()[0],
                "storage/originals/text.txt",
            )
        self.assertEqual(self.media.read_bytes(), b"original bytes")
        self.assertEqual((self.old / "keep.txt").read_bytes(), b"previous installation")

    def _failure_record(self):
        files = list((self.root / "manifests").glob("update-*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_every_supported_schema_backed_up_staged_and_migrated_with_media_identity(
        self,
    ):
        for version in range(1, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                self._legacy(version)
                journal = self._prepare()
                record = json.loads(journal.read_text())
                self.assertEqual(record["phase"], "PREPARED")
                self.assertTrue(record["migration_committed"])
                self.assertTrue(record["activation_pending"])
                self.assertTrue(Path(record["candidate_directory"]).is_dir())
                snapshot = (
                    self.root / "backups" / record["backup_id"] / "creator_loop.sqlite3"
                )
                with closing(open_readonly(snapshot.resolve())) as db:
                    validate(db, expected_version=version)
                self._assert_source(SCHEMA_VERSION)

    def test_app_and_writer_locks_remain_held_during_staging_and_migration(self):
        from creator_loop.update_preparation import stage_installation

        def assert_locked():
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    self.fail("App lock released early")
            with closing(sqlite3.connect(self.path, timeout=0.02)) as other:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute("BEGIN IMMEDIATE")

        def stage(*args, **kwargs):
            assert_locked()
            result = stage_installation(*args, **kwargs)
            assert_locked()
            return result

        def migrate(db, version):
            assert_locked()
            _migrate_locked(db, version)
            assert_locked()

        with (
            patch(
                "creator_loop.update_preparation.stage_installation", side_effect=stage
            ),
            patch(
                "creator_loop.update_preparation._migrate_locked", side_effect=migrate
            ),
        ):
            self._prepare()
        self._assert_source(SCHEMA_VERSION)

    def test_migration_and_media_inventory_failures_roll_back_keep_backup_and_candidate(
        self,
    ):
        def fail_migration(db, version):
            _migrate_locked(db, version)
            raise RuntimeError("private fixture content should not enter journal")

        with patch(
            "creator_loop.update_preparation._migrate_locked",
            side_effect=fail_migration,
        ):
            with self.assertRaises(RuntimeError):
                self._prepare()
        record = self._failure_record()
        self.assertEqual(record["phase"], "FAILED_ROLLED_BACK")
        self.assertNotIn("private fixture", json.dumps(record))
        self.assertIsNotNone(record["backup_id"])
        self.assertTrue(Path(record["candidate_directory"]).is_dir())
        self._assert_source(1)

    def test_staging_failure_keeps_valid_backup_without_committing_schema(self):
        with patch(
            "creator_loop.update_preparation.stage_installation",
            side_effect=OSError("fixture stage failure"),
        ):
            with self.assertRaises(OSError):
                self._prepare()
        record = self._failure_record()
        self.assertEqual(record["phase"], "FAILED_ROLLED_BACK")
        snapshot = self.root / "backups" / record["backup_id"] / "creator_loop.sqlite3"
        with closing(open_readonly(snapshot.resolve())) as db:
            validate(db, expected_version=1)
        self._assert_source(1)

    def test_storage_inventory_change_rolls_back_before_commit(self):
        from creator_loop.update_preparation import _inventory

        count = 0

        def inventory(db, root):
            nonlocal count
            count += 1
            result = _inventory(db, root)
            return result if count == 1 else []

        with patch("creator_loop.update_preparation._inventory", side_effect=inventory):
            with self.assertRaisesRegex(RuntimeError, "storage reference inventory"):
                self._prepare()
        self.assertEqual(self._failure_record()["phase"], "FAILED_ROLLED_BACK")
        self._assert_source(1)

    def test_post_commit_journal_failure_is_not_reported_as_rollback(self):
        from creator_loop.update_preparation import _journal

        def journal(path, record):
            if record["phase"] == "PREPARED":
                raise OSError("fixture metadata failure after commit")
            _journal(path, record)

        with patch("creator_loop.update_preparation._journal", side_effect=journal):
            with self.assertRaises(OSError):
                self._prepare()
        record = self._failure_record()
        self.assertEqual(record["phase"], "FAILED_POST_COMMIT")
        self.assertTrue(record["migration_committed"])
        self.assertTrue(record["activation_pending"])
        self._assert_source(SCHEMA_VERSION)

    def test_candidate_checksum_mismatch_refuses_before_backup_or_db_changes(self):
        self.manifest["migration_checksums"][MIGRATIONS[0]] = "0" * 64
        self._save()
        with self.assertRaisesRegex(ValueError, "Candidate migrations"):
            self._prepare()
        self.assertEqual(list((self.root / "backups").iterdir()), [])
        self.assertEqual(list((self.root / "manifests").iterdir()), [])
        self._assert_source(1)

    def test_candidate_packaged_sql_must_match_running_updater(self):
        name = f"CreatorLoop/_internal/migrations/{MIGRATIONS[0]}.sql"
        self.members[name] += b"\n-- a changed candidate baseline\n"
        with zipfile.ZipFile(self.archive, "w") as archive:
            for member, content in self.members.items():
                archive.writestr(member, content)
        self.manifest["artifact_sha256"] = hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        self.manifest["files"][name] = {
            "size": len(self.members[name]),
            "sha256": hashlib.sha256(self.members[name]).hexdigest(),
        }
        self._save()
        with self.assertRaisesRegex(RuntimeError, "packaged migration checksum"):
            self._prepare()
        self.assertEqual(self._failure_record()["phase"], "FAILED_ROLLED_BACK")
        self._assert_source(1)

    def test_cli_preparation_and_app_contention_exit_codes(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--prepare-update",
            str(self.archive),
            "--release-manifest",
            str(self.manifest_path),
            "--installation-root",
            str(self.installation),
        ]
        with AppDataLock(self.root):
            blocked = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        result = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"activation pending", result.stdout)
        self._assert_source(SCHEMA_VERSION)
