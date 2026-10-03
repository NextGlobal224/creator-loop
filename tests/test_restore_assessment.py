"""Restore review checks actual backup/media/current state without applying it."""

import hashlib
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
from creator_loop.database import SCHEMA_VERSION, _connect_write
from creator_loop.restore_assessment import assess_restore
from creator_loop.storage_roots import register_storage_root
from creator_loop.update_backup import create_update_backup


class RestoreAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        self.journal = self.fixture._prepare()
        self.record = json.loads(self.journal.read_text())
        self.candidate = Path(self.record["candidate_directory"])
        self.backup_id = self.record["backup_id"]
        self.backup = self.root / "backups" / self.backup_id

    def _assess(self, **kwargs):
        return assess_restore(
            self.root, self.backup_id, self.installation, self.candidate, **kwargs
        )

    def test_assessment_reads_backup_and_preserves_all_source_media_and_coordination(
        self,
    ):
        before = (
            self.fixture.path.read_bytes(),
            self.fixture.media.read_bytes(),
            self.journal.read_bytes(),
            set((self.root / "backups").iterdir()),
        )
        result = self._assess()
        self.assertEqual(result["backup_schema"], 1)
        self.assertEqual(result["current_schema"], SCHEMA_VERSION)
        self.assertEqual(result["target_schema"], SCHEMA_VERSION)
        self.assertEqual(result["media_issue_count"], 0)
        self.assertFalse(result["restored"])
        self.assertTrue(result["requires_lost_changes_confirmation"])
        self.assertIn("every database change", result["loss_warning"])
        self.assertEqual(result["backup_counts"]["assets"], 1)
        self.assertEqual(result["media_assessment"][0]["status"], "valid")
        self.assertEqual(
            (
                self.fixture.path.read_bytes(),
                self.fixture.media.read_bytes(),
                self.journal.read_bytes(),
                set((self.root / "backups").iterdir()),
            ),
            before,
        )
        self.assertEqual(
            self._assess()["assessment_identity"], result["assessment_identity"]
        )
        self.assertNotIn("original bytes", json.dumps(result))
        self.assertNotIn("storage/originals/text.txt", json.dumps(result))

    def test_same_size_changed_digest_missing_and_size_mismatch_report_separately(self):
        original = self.fixture.media.read_bytes()
        initial = self._assess()["assessment_identity"]
        for payload, status in (
            (b"x" * len(original), "digest_mismatch"),
            (b"x", "size_mismatch"),
            (None, "unavailable_file"),
        ):
            with self.subTest(status=status):
                if payload is None:
                    self.fixture.media.unlink()
                else:
                    self.fixture.media.write_bytes(payload)
                result = self._assess()
                self.assertEqual(result["media_issue_count"], 1)
                self.assertEqual(result["media_assessment"][0]["status"], status)
                self.assertTrue(result["requires_media_issues_confirmation"])
                self.assertNotEqual(result["assessment_identity"], initial)
                self.assertEqual(self.fixture.media.exists(), payload is not None)
                self.fixture.media.write_bytes(original)

    def test_wal_visible_new_rows_change_proof_even_before_main_db_checkpoint(self):
        with closing(_connect_write(self.fixture.path)) as db:
            before = self._assess()
            db_bytes = self.fixture.path.read_bytes()
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (
                    "new",
                    "TEXT",
                    "private unpublished content",
                    preparation_fixture.T,
                    None,
                ),
            )
            db.commit()
            self.assertEqual(self.fixture.path.read_bytes(), db_bytes)
            after = self._assess()
            self.assertEqual(after["current_counts"]["assets"], 2)
            self.assertEqual(after["backup_counts"]["assets"], 1)
            self.assertNotEqual(
                after["current_database_identity"], before["current_database_identity"]
            )
            self.assertNotEqual(
                after["assessment_identity"], before["assessment_identity"]
            )
            self.assertNotIn("private unpublished content", json.dumps(after))

    def test_newer_media_and_reference_are_reported_kept_and_not_in_backup(self):
        newer = self.root / "storage/originals/newer.txt"
        newer.write_bytes(b"newer file to keep")
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("new", "TEXT", "newer", preparation_fixture.T, None),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "new-file",
                    "new",
                    "ORIGINAL",
                    "storage/originals/newer.txt",
                    hashlib.sha256(newer.read_bytes()).hexdigest(),
                    newer.stat().st_size,
                    "text/plain",
                    preparation_fixture.T,
                ),
            )
            db.commit()
        result = self._assess()
        self.assertEqual(result["current_file_references_not_in_backup"], 1)
        self.assertEqual(len(result["media_assessment"]), 1)
        self.assertEqual(newer.read_bytes(), b"newer file to keep")
        self.assertEqual(result["current_counts"]["asset_files"], 2)

    def test_current_registry_change_invalidates_proof_without_repointing_old_keys(
        self,
    ):
        registry = self.root / "manifests/storage-roots.json"
        payload = {
            "manifest_version": 1,
            "data_root_id": "c" * 32,
            "schema_version": SCHEMA_VERSION,
            "storage_roots": [],
            "component_installations": [{"owned": False}],
            "foreign": "old",
        }
        registry.write_text(json.dumps(payload))
        before = self._assess()
        payload["foreign"] = "new"
        registry.write_text(json.dumps(payload))
        after = self._assess()
        self.assertEqual(after["data_root_id"], before["data_root_id"])
        self.assertNotEqual(after["registry_sha256"], before["registry_sha256"])
        self.assertNotEqual(after["assessment_identity"], before["assessment_identity"])
        self.assertEqual(json.loads(registry.read_text()), payload)
        self.assertEqual(self.fixture.media.read_bytes(), b"original bytes")

    def test_registered_backup_reference_offline_is_reported_without_fallback(self):
        external = self.fixture.base / "registered fixture"
        external.mkdir()
        (external / "originals").mkdir()
        media = external / "originals/registered.txt"
        media.write_bytes(b"original bytes")
        with patch(
            "creator_loop.storage_roots.volume_identity", return_value="fixture-volume"
        ):
            registered = register_storage_root(self.root, external)
            with closing(sqlite3.connect(self.fixture.path)) as db:
                db.execute(
                    "UPDATE asset_files SET storage_key=? WHERE file_id='f'",
                    (f"registered/{registered.root_id}/originals/registered.txt",),
                )
                db.commit()
            self.backup_id = create_update_backup(self.root).name
        with patch(
            "creator_loop.storage_roots.volume_identity",
            side_effect=OSError("offline fixture"),
        ):
            result = self._assess()
        self.assertEqual(result["media_assessment"][0]["status"], "unavailable_file")
        self.assertEqual(result["media_issue_count"], 1)
        self.assertEqual(self.fixture.media.read_bytes(), b"original bytes")
        self.assertEqual(media.read_bytes(), b"original bytes")

    def test_incompatible_candidate_and_changed_backup_refuse_without_mutation(self):
        manifest_path = self.candidate / "release-manifest.json"
        original_manifest = manifest_path.read_bytes()
        manifest = json.loads(original_manifest)
        manifest["schema_from"] = 2
        manifest_path.write_text(json.dumps(manifest))
        before = self.fixture.path.read_bytes(), self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "cannot migrate"):
            self._assess()
        manifest_path.write_bytes(original_manifest)
        snapshot = self.backup / "creator_loop.sqlite3"
        snapshot.write_bytes(snapshot.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "digest"):
            self._assess()
        self.assertEqual(
            (self.fixture.path.read_bytes(), self.journal.read_bytes()), before
        )
        self.assertFalse((self.installation / "active-installation.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows read-sharing semantics")
    def test_windows_media_is_held_against_writes_while_it_is_hashed(self):
        from contextlib import contextmanager

        from creator_loop.publication_media import _open_read_lock

        @contextmanager
        def held(path):
            with _open_read_lock(path) as stream:
                with self.assertRaises(PermissionError):
                    path.write_bytes(b"outside writer")
                yield stream

        with patch("creator_loop.restore_assessment._open_read_lock", side_effect=held):
            self.assertEqual(self._assess()["media_issue_count"], 0)
        self.assertEqual(self.fixture.media.read_bytes(), b"original bytes")

    def test_locks_and_unresolved_runtime_prevent_assessment_without_touching_db(self):
        with AppDataLock(self.root):
            with self.assertRaises(DataRootBusy):
                self._assess()
        with closing(sqlite3.connect(self.fixture.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self._assess(timeout_seconds=0.02)
            db.rollback()
        stale = self.root / "runtime/unknown.json"
        stale.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "workers require recovery"):
            self._assess()
        self.assertTrue(stale.exists())

    def test_expired_work_budget_and_unsafe_backup_identity_refuse(self):
        with self.assertRaises(TimeoutError):
            self._assess(timeout_seconds=1e-12)
        with self.assertRaises(ValueError):
            self._assess(timeout_seconds=float("inf"))
        with self.assertRaisesRegex(ValueError, "backup ID"):
            assess_restore(self.root, "../outside", self.installation, self.candidate)
        self.fixture._assert_source(SCHEMA_VERSION)

    def test_cli_requires_explicit_backup_candidate_and_does_not_initialize_missing_db(
        self,
    ):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--inspect-restore",
            self.backup_id,
            "--restore-candidate",
            str(self.candidate),
            "--installation-root",
            str(self.installation),
        ]
        result = subprocess.run(command, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["restored"])
        incompatible = subprocess.run(
            command + ["--backup"], env=env, capture_output=True, timeout=10
        )
        self.assertEqual(incompatible.returncode, 2)
        self.fixture.path.unlink()
        missing = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(missing.returncode, 4)
        self.assertFalse(self.fixture.path.exists())


if __name__ == "__main__":
    unittest.main()
