"""Committed WAL, writer exclusion, metadata and failed-backup preservation."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.database import (
    MIGRATIONS,
    _connect_write,
    _execute_migration,
    _migration_sql,
    initialize,
    open_readonly,
    validate,
)
from creator_loop.paths import ensure_data_root
from creator_loop.storage_roots import register_storage_root, set_default_storage_root
from creator_loop.update_backup import create_update_backup

T = "2026-10-03T00:00:00Z"


class UpdateBackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Dá»¯ liá»‡u Huáº¿"
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = _connect_write(self.path)
        self.addCleanup(self.db.close)
        self.media = self.root / "storage/originals/text.txt"
        self.media.write_bytes(b"original bytes")
        self.digest = hashlib.sha256(self.media.read_bytes()).hexdigest()
        self.db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)", ("a", "TEXT", "title", T, None)
        )
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "f",
                "a",
                "ORIGINAL",
                "storage/originals/text.txt",
                self.digest,
                14,
                "text/plain",
                T,
            ),
        )
        self.db.commit()

    def _unchanged(self):
        self.assertEqual(self.media.read_bytes(), b"original bytes")
        self.assertEqual(
            self.db.execute("SELECT display_name FROM assets").fetchone()[0], "title"
        )
        validate(self.db)

    def test_committed_wal_snapshot_opens_with_metadata_and_media_inventory(self):
        self.assertGreater(
            self.path.with_name(self.path.name + "-wal").stat().st_size, 0
        )
        before = self.db.execute("PRAGMA data_version").fetchone()[0]
        target = create_update_backup(self.root)
        manifest = json.loads(
            (target / "backup-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["backup_id"], target.name)
        self.assertFalse(manifest["media_included"])
        snapshot = target / manifest["database_file"]
        self.assertEqual(
            manifest["database_sha256"],
            hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        )
        self.assertEqual(manifest["database_size"], snapshot.stat().st_size)
        self.assertEqual(manifest["storage_references"][0]["sha256"], self.digest)
        with closing(open_readonly(snapshot)) as db:
            validate(db)
            self.assertEqual(
                db.execute("SELECT display_name FROM assets").fetchone()[0], "title"
            )
        self.assertEqual(self.db.execute("PRAGMA data_version").fetchone()[0], before)
        self._unchanged()

    def test_app_or_external_writer_contention_creates_no_backup(self):
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            create_update_backup(self.root)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                create_update_backup(self.root, timeout_seconds=0.05)
        finally:
            self.db.rollback()
        self.assertEqual(list((self.root / "backups").iterdir()), [])
        self._unchanged()

    def test_other_writer_is_blocked_during_backup_and_released_afterward(self):
        from creator_loop.update_backup import _inventory

        def check_lock(db, root):
            with closing(sqlite3.connect(self.path, timeout=0.05)) as other:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute("BEGIN IMMEDIATE")
            return _inventory(db, root)

        with patch("creator_loop.update_backup._inventory", side_effect=check_lock):
            create_update_backup(self.root)
        self.db.execute("BEGIN IMMEDIATE")
        self.db.rollback()
        self._unchanged()

    def test_disk_full_write_failure_and_validation_failure_keep_prior_backup(self):
        prior = create_update_backup(self.root)
        prior_bytes = (prior / "creator_loop.sqlite3").read_bytes()
        with patch("creator_loop.update_backup.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(OSError, "Insufficient"):
                create_update_backup(self.root)
        with patch(
            "creator_loop.update_backup.os.fsync",
            side_effect=OSError("fixture disk failure"),
        ):
            with self.assertRaises(OSError):
                create_update_backup(self.root)
        with patch(
            "creator_loop.update_backup.os.fsync",
            side_effect=[None, None, OSError("fixture metadata failure")],
        ):
            with self.assertRaisesRegex(OSError, "metadata failure"):
                create_update_backup(self.root)
        from creator_loop.update_backup import validate as real_validate

        calls = 0

        def reject_copy(db, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture snapshot invalid")
            return real_validate(db, **kwargs)

        with patch("creator_loop.update_backup.validate", side_effect=reject_copy):
            with self.assertRaisesRegex(RuntimeError, "snapshot invalid"):
                create_update_backup(self.root)
        self.assertEqual(list((self.root / "backups").iterdir()), [prior])
        self.assertEqual((prior / "creator_loop.sqlite3").read_bytes(), prior_bytes)
        self._unchanged()

    def test_missing_media_and_unresolved_worker_records_fail_closed(self):
        self.media.unlink()
        with self.assertRaisesRegex(RuntimeError, "Storage reference"):
            create_update_backup(self.root)
        self.media.write_bytes(b"original bytes")
        record = self.root / "runtime/unknown-worker.json"
        record.write_text("unresolved", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "runtime records"):
            create_update_backup(self.root)
        self.assertEqual(record.read_text(), "unresolved")
        self.assertEqual(list((self.root / "backups").iterdir()), [])
        self._unchanged()

    def test_cli_backup_does_not_initialize_missing_db_or_start_ui(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        command = [sys.executable, "-m", "creator_loop", "--backup"]
        result = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"media is not included", result.stdout)
        self.db.close()
        self.path.unlink()
        result = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertFalse(self.path.exists())

    def test_bad_timeout_creates_nothing(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                create_update_backup(self.root, timeout_seconds=value)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_all_supported_old_schemas_back_up_without_migration(self):
        for version in range(1, len(MIGRATIONS)):
            with self.subTest(version=version):
                root = self.root.parent / f"schema-{version}"
                ensure_data_root(root)
                path = root / "creator_loop.sqlite3"
                with closing(_connect_write(path)) as db:
                    for migration in MIGRATIONS[:version]:
                        sql, digest = _migration_sql(migration)
                        _execute_migration(db, sql)
                        db.execute(
                            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                            (migration, digest, T, "0.1.0"),
                        )
                    db.execute(f"PRAGMA user_version={version}")
                    db.commit()
                target = create_update_backup(root)
                for candidate in (path, target / "creator_loop.sqlite3"):
                    with closing(open_readonly(candidate)) as db:
                        validate(db, expected_version=version)

    def test_timeout_discards_only_own_stage_and_releases_locks(self):
        with patch("creator_loop.update_backup.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                create_update_backup(self.root)
        self.assertEqual(list((self.root / "backups").iterdir()), [])
        create_update_backup(self.root)
        self._unchanged()

    def test_registered_root_inventory_and_coordination_manifest_are_preserved(self):
        other = self.root.parent / "External media"
        (other / "originals").mkdir(parents=True)
        (other / "originals/text.txt").write_bytes(self.media.read_bytes())
        with patch(
            "creator_loop.storage_roots.volume_identity", return_value="fixture-volume"
        ):
            registered = register_storage_root(self.root, other)
            set_default_storage_root(self.root, registered.root_id)
            key = f"registered/{registered.root_id}/originals/text.txt"
            self.db.execute(
                "UPDATE asset_files SET storage_key=? WHERE file_id='f'", (key,)
            )
            self.db.commit()
            target = create_update_backup(self.root)
        manifest = json.loads(
            (target / "backup-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["storage_manifest"]["default_storage_root_id"], registered.root_id
        )
        self.assertEqual(manifest["storage_roots"][0]["volume_id"], "fixture-volume")
        self.assertEqual(manifest["storage_references"][0]["storage_key"], key)
        self.assertEqual((other / "originals/text.txt").read_bytes(), b"original bytes")
        # Reconnection/physical media testing remains separate; no fallback here.
        with patch(
            "creator_loop.storage_roots.volume_identity",
            return_value="different-volume",
        ):
            with self.assertRaises(ValueError):
                create_update_backup(self.root)
        self.assertEqual(list((self.root / "backups").iterdir()), [target])

    def test_queued_run_and_future_schema_refuse_before_snapshot(self):
        self.db.execute(
            "INSERT INTO processing_runs(run_id,asset_id,task_type,status,tool_name,tool_version,created_at) VALUES (?,?,?,?,?,?,?)",
            ("r", "a", "fixture", "QUEUED", "fake", "1", T),
        )
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "Active processing"):
            create_update_backup(self.root)
        self.db.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            create_update_backup(self.root)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_delete_journal_snapshot_also_opens_without_changing_source(self):
        self.assertEqual(
            self.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete"
        )
        target = create_update_backup(self.root)
        with closing(open_readonly(target / "creator_loop.sqlite3")) as db:
            validate(db)
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self._unchanged()
