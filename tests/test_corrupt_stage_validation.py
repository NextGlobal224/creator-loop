"""Held closed stage/raw archive validation never initializes or changes live DB."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import closing

import test_corrupt_restore_preparation as preparation_fixture
from creator_loop.corrupt_stage_validation import hold_corrupt_preparation
from creator_loop.restore_assessment import _database_identity


class CorruptStageValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.CorruptRestorePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.manifest = self.fixture.prepare()
        self.stage = self.manifest.parent / "restored.sqlite3"
        self.original = self.manifest.read_bytes()
        self.record = json.loads(self.original)
        self.token = hashlib.sha256(self.original).hexdigest()
        self.before = self.fixture.protected_bytes()

    def hold(self, **kwargs):
        return hold_corrupt_preparation(self.root, self.manifest, self.token, **kwargs)

    def write_record(self, record):
        body = json.dumps(record).encode("utf-8")
        self.manifest.write_bytes(body)
        self.token = hashlib.sha256(body).hexdigest()

    def assert_unchanged(self):
        self.assertEqual(self.fixture.protected_bytes(), self.before)
        self.assertEqual(self.manifest.read_bytes(), self.original)
        self.assertEqual(
            {path.name for path in self.manifest.parent.iterdir()},
            {"preparation.json", "restored.sqlite3"},
        )

    def test_valid_closed_stage_archive_hash_counts_refs_without_live_assessment(self):
        with self.hold() as record:
            self.assertTrue(record["stage_revalidated"])
            self.assertFalse(record["apply_authorized"])
            self.assertFalse(record["activated"])
            self.assertFalse(record["restored"])
            self.assertFalse(record["current_source_assessed"])
            self.assertEqual(record["preparation_manifest_sha256"], self.token)
            self.assertEqual(record["staged_counts"]["assets"], 1)
        self.assert_unchanged()

    def test_actual_schema_derived_media_roles_validate_without_reading_live_media(
        self,
    ):
        roles = ("DERIVED_AUDIO", "DERIVED_FRAME", "THUMBNAIL", "OTHER")
        digest = hashlib.sha256(b"DERIVED FIXTURE").hexdigest()
        with closing(sqlite3.connect(self.stage)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for index, role in enumerate(roles):
                db.execute(
                    "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"z{index}",
                        "a",
                        role,
                        f"storage/derived/{index}.dat",
                        digest,
                        15,
                        "application/octet-stream",
                        "f",
                        "2026-10-04T00:00:00Z",
                    ),
                )
                self.record["media_assessment"].append(
                    {
                        "file_id": f"z{index}",
                        "role": role,
                        "expected_size": 15,
                        "expected_sha256": digest,
                        "actual_size": 15,
                        "actual_sha256": digest,
                        "status": "valid",
                    }
                )
            db.commit()
            identity, counts = _database_identity(db, time.monotonic() + 30)
        self.record.update(
            staged_database_identity=identity,
            staged_counts=counts,
            staged_database_sha256=hashlib.sha256(self.stage.read_bytes()).hexdigest(),
            staged_database_byte_size=self.stage.stat().st_size,
        )
        self.write_record(self.record)
        with self.hold() as result:
            self.assertEqual(result["staged_counts"]["asset_files"], 5)
            self.assertFalse(result["current_source_assessed"])
        self.assertEqual(self.fixture.protected_bytes(), self.before)

    def test_actual_cli_only_verifies_explicit_stage_and_does_not_init_missing_live_db(
        self,
    ):
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--verify-corrupt-preparation",
            str(self.manifest),
            "--reviewed-preparation",
            self.token,
        ]
        environment = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        self.fixture.fixture.path.unlink()  # own fixture only
        success = subprocess.run(
            command, env=environment, capture_output=True, timeout=30
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        receipt = json.loads(success.stdout)
        self.assertTrue(receipt["stage_revalidated"])
        self.assertFalse(receipt["current_source_assessed"])
        self.assertFalse(receipt["restored"])
        for args in (command + ["--smoke"], command[:-2]):
            result = subprocess.run(
                args, env=environment, capture_output=True, timeout=20
            )
            self.assertEqual(result.returncode, 2, result.stderr)
        stale = subprocess.run(
            command[:-1] + ["0" * 64], env=environment, capture_output=True, timeout=30
        )
        self.assertEqual(stale.returncode, 4)
        self.assertFalse(self.fixture.fixture.path.exists())
        self.assertNotIn(b"PRIVATE", success.stdout + success.stderr + stale.stderr)

    def test_missing_or_changed_live_source_is_not_opened_or_initialized(self):
        self.fixture.fixture.path.unlink()  # own fixture only
        with self.hold() as record:
            self.assertFalse(record["current_source_assessed"])
        self.assertFalse(self.fixture.fixture.path.exists())
        self.fixture.fixture.path.write_bytes(b"NEW LIVE STATE")
        with self.hold() as record:
            self.assertFalse(record["restored"])
        self.assertEqual(self.fixture.fixture.path.read_bytes(), b"NEW LIVE STATE")

    def test_stale_manifest_digest_and_bad_budget_refuse_read_only(self):
        self.manifest.write_bytes(self.original + b" ")
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(self.manifest.read_bytes(), self.original + b" ")
        self.manifest.write_bytes(self.original)
        for budget in (0, float("nan"), 601):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                with self.hold(timeout_seconds=budget):
                    pass
        self.assert_unchanged()

    def test_tampered_stage_bytes_are_retained_and_refused(self):
        original = self.stage.read_bytes()
        self.stage.write_bytes(original + b"TAMPERED")
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(self.stage.read_bytes(), original + b"TAMPERED")
        self.assertEqual(self.fixture.protected_bytes(), self.before)

    def test_changed_raw_archive_refuses_without_repairing_anything(self):
        raw = self.fixture.raw_manifest.parent / "creator_loop.sqlite3"
        raw.write_bytes(b"RAW TAMPER")
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(raw.read_bytes(), b"RAW TAMPER")
        self.assertEqual(self.manifest.read_bytes(), self.original)

    def test_partial_unknown_entries_and_hardlinks_refuse_without_cleanup(self):
        unknown = self.manifest.parent / "restored.sqlite3-wal"
        unknown.write_bytes(b"KEEP UNKNOWN")
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(unknown.read_bytes(), b"KEEP UNKNOWN")
        unknown.unlink()  # own fixture only
        alias = self.root / "stage alias"
        os.link(self.stage, alias)
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(alias.read_bytes(), self.stage.read_bytes())

    def test_metadata_claims_types_root_namespace_and_unknown_fields_refuse(self):
        bad_values = {
            "apply_authorized": True,
            "activated": True,
            "restored": True,
            "current_changes_assessable": True,
            "media_included": True,
            "raw_source_preserved": False,
            "confirmed_lost_changes": 1,
            "confirmed_media_issues": 1,
            "schema_from": True,
            "schema_to": True,
            "staged_database_byte_size": True,
            "data_root_identity": "0" * 64,
            "restore_id": "0" * 32,
            "phase": "RESTORED",
            "staged_database": "../creator_loop.sqlite3",
            "raw_manifest": "../raw-source-manifest.json",
            "candidate_name": "../escaped",
            "prepared_at": "2026-10-04T00:00:00",
        }
        for key, value in bad_values.items():
            with self.subTest(key=key):
                self.write_record({**self.record, key: value})
                with self.assertRaises(ValueError):
                    with self.hold():
                        pass
        self.write_record({**self.record, "unknown": "KEEP"})
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(self.fixture.protected_bytes(), self.before)

    def test_duplicate_fields_and_oversized_json_refuse(self):
        body = self.original[:-1] + b',"restored":false}'
        self.manifest.write_bytes(body)
        self.token = hashlib.sha256(body).hexdigest()
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.manifest.write_bytes(b" " * (16 * 1024**2 + 1))
        with self.assertRaises(ValueError):
            with self.hold():
                pass
        self.assertEqual(self.manifest.stat().st_size, 16 * 1024**2 + 1)

    def test_matching_physical_hash_does_not_bypass_logical_counts_and_media_checks(
        self,
    ):
        for key, value in (
            ("staged_database_identity", "0" * 64),
            ("staged_counts", {**self.record["staged_counts"], "assets": 3}),
            ("media_assessment", []),
        ):
            with self.subTest(key=key):
                self.write_record({**self.record, key: value})
                with self.assertRaises(ValueError):
                    with self.hold():
                        pass
        self.assertEqual(self.fixture.protected_bytes(), self.before)

    def test_cancel_and_caller_failure_preserve_stage_and_original_inputs(self):
        with self.assertRaises(InterruptedError):
            with self.hold(cancelled=lambda: True):
                pass
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with self.hold():
                raise RuntimeError("caller failed")
        self.assert_unchanged()

    @unittest.skipUnless(sys.platform == "win32", "native retained handles")
    def test_native_stage_raw_files_and_folders_cannot_change_during_caller(self):
        with self.hold():
            for path in (self.stage, self.manifest, self.fixture.raw_manifest):
                with self.assertRaises(OSError):
                    path.write_bytes(b"NEVER WRITE")
                with self.assertRaises(OSError):
                    path.unlink()
            for path in (
                self.root,
                self.manifest.parent,
                self.fixture.raw_manifest.parent,
            ):
                with self.assertRaises(OSError):
                    path.rename(path.with_name("never rename"))
        self.assert_unchanged()

    @unittest.skipIf(sys.platform == "win32", "native stage handle denies changes")
    def test_portable_change_after_yield_refuses_without_deleting_evidence(self):
        with self.assertRaises(RuntimeError):
            with self.hold():
                self.stage.write_bytes(b"PORTABLE CHANGE")
        self.assertEqual(self.stage.read_bytes(), b"PORTABLE CHANGE")
        self.assertEqual(self.fixture.protected_bytes(), self.before)
