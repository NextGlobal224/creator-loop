"""Success metadata preserves data-root/registry identities and healthy activation."""

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
from creator_loop.update_health import readonly_health
from creator_loop.update_metadata import repair_update_metadata


class UpdateMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        self.journal = self.fixture._prepare()
        self.record = json.loads(self.journal.read_text())
        self.candidate = Path(self.record["candidate_directory"])
        self.manifest = self.root / "manifests/storage-roots.json"
        self.before = self.fixture.path.read_bytes()

    def _health(self, executable, root, log_directory, **kwargs):
        with self.assertRaises(DataRootBusy):
            with AppDataLock(root):
                self.fail("Metadata health released app lock")
        with closing(sqlite3.connect(self.fixture.path, timeout=0.02)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("BEGIN IMMEDIATE")
        log_directory.mkdir()
        return readonly_health(root)

    def _activate(self):
        with patch(
            "creator_loop.update_activation.run_health_check", side_effect=self._health
        ):
            return activate_prepared_update(self.root, self.journal, self.installation)

    def _repair(self):
        with patch(
            "creator_loop.update_metadata.run_health_check", side_effect=self._health
        ):
            return repair_update_metadata(self.root, self.journal, self.installation)

    def _assert_preserved(self):
        self.assertEqual(self.fixture.path.read_bytes(), self.before)
        self.fixture._assert_source(SCHEMA_VERSION)

    def test_success_creates_stable_data_root_id_and_update_backup_metadata(self):
        self._activate()
        payload = json.loads(self.manifest.read_text())
        self.assertRegex(payload["data_root_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["last_backup_id"], self.record["backup_id"])
        self.assertEqual(
            payload["last_successful_update"]["update_id"], self.record["update_id"]
        )
        self.assertEqual(
            payload["last_successful_update"]["git_commit"],
            self.record["candidate_commit"],
        )
        self.assertEqual(payload["component_installations"], [])
        self._repair()
        self.assertEqual(json.loads(self.manifest.read_text()), payload)
        self._assert_preserved()

    def test_existing_root_default_offline_registration_and_external_components_preserved(
        self,
    ):
        payload = {
            "manifest_version": 1,
            "data_root_id": "a" * 32,
            "schema_version": SCHEMA_VERSION,
            "default_storage_root_id": "b" * 32,
            "storage_roots": [
                {
                    "root_id": "b" * 32,
                    "path": str(self.fixture.base / "offline fixture root"),
                    "volume_id": "fixture unavailable volume",
                }
            ],
            "component_installations": [{"name": "external fixture", "owned": False}],
            "foreign_coordination_field": "keep",
        }
        self.manifest.write_text(json.dumps(payload))
        self._activate()
        updated = json.loads(self.manifest.read_text())
        for field, value in payload.items():
            self.assertEqual(updated[field], value)
        self._assert_preserved()

    def test_metadata_write_failure_keeps_healthy_active_pointer_and_is_repairable(
        self,
    ):
        with patch(
            "creator_loop.update_metadata._write_manifest",
            side_effect=OSError("fixture private failure"),
        ):
            with self.assertRaises(OSError):
                self._activate()
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["phase"], "COMPLETED_METADATA_PENDING")
        self.assertFalse(record["activation_pending"])
        self.assertNotIn("fixture private failure", json.dumps(record))
        self.assertEqual(
            active_candidate(self.installation, SCHEMA_VERSION)[0], self.candidate
        )
        self.assertFalse(self.manifest.exists())
        self._repair()
        repaired = json.loads(self.journal.read_text())
        self.assertEqual(repaired["phase"], "COMPLETED")
        self.assertNotIn("error_type", repaired)
        self.assertEqual(
            json.loads(self.manifest.read_text())["last_backup_id"],
            self.record["backup_id"],
        )
        self._assert_preserved()

    def test_failed_fresh_health_keeps_success_metadata_and_records_error_type_only(
        self,
    ):
        self._activate()
        before = self.manifest.read_bytes()
        with patch(
            "creator_loop.update_metadata.run_health_check",
            side_effect=TimeoutError("private fixture text"),
        ):
            with self.assertRaises(TimeoutError):
                repair_update_metadata(self.root, self.journal, self.installation)
        self.assertEqual(self.manifest.read_bytes(), before)
        record = json.loads(self.journal.read_text())
        self.assertEqual(record["metadata_repair_error_type"], "TimeoutError")
        self.assertNotIn("private fixture text", json.dumps(record))
        self.assertEqual(
            active_candidate(self.installation, SCHEMA_VERSION)[0], self.candidate
        )
        self._assert_preserved()

    def test_repair_rejects_a_different_active_update_before_health_or_manifest_write(
        self,
    ):
        self._activate()
        before = self.manifest.read_bytes()
        record = json.loads(self.journal.read_text())
        record["update_id"] = "c" * 32
        alternate = self.root / "manifests" / f"update-{record['update_id']}.json"
        alternate.write_text(json.dumps(record))
        with patch("creator_loop.update_metadata.run_health_check") as health:
            with self.assertRaisesRegex(ValueError, "current active update"):
                repair_update_metadata(self.root, alternate, self.installation)
            health.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)
        self._assert_preserved()

    def test_missing_or_changed_backup_prevents_repair_without_touching_success_metadata(
        self,
    ):
        self._activate()
        before = self.manifest.read_bytes()
        backup = (
            self.root / "backups" / self.record["backup_id"] / "creator_loop.sqlite3"
        )
        backup.write_bytes(backup.read_bytes() + b"changed")
        with patch("creator_loop.update_metadata.run_health_check") as health:
            with self.assertRaisesRegex(ValueError, "backup digest"):
                repair_update_metadata(self.root, self.journal, self.installation)
            health.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)
        self._assert_preserved()
