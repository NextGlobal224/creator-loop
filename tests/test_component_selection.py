"""Selections preserve coordination/domain history and never acquire files."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.component_selection import (
    load_component_selection,
    save_component_selection,
)
from creator_loop.database import SCHEMA_VERSION, initialize
from creator_loop.local_components import (
    ComponentPreflightError,
    parse_component_manifest,
)
from creator_loop.paths import ensure_data_root


class ComponentSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve(strict=True)
        self.root = self.folder / "user data"
        ensure_data_root(self.root)
        self.database = self.root / "creator_loop.sqlite3"
        initialize(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (
                    "keep-asset",
                    "TEXT",
                    "Keep domain history",
                    "2026-10-04T00:00:00Z",
                    None,
                ),
            )
            db.commit()
        self.manifest = self.root / "manifests/storage-roots.json"
        self.external = self.folder / "unowned fake model.fixture"
        self.external.write_bytes(b"FAKE PRIVATE MODEL BYTES\x00\xff")
        self.specs = parse_component_manifest(
            json.dumps(
                {
                    "manifest_version": 1,
                    "components": [
                        {
                            "component_id": "fake-model",
                            "kind": "MODEL",
                            "path": str(self.external),
                            "version": "fake-v1",
                            "sha256": hashlib.sha256(
                                self.external.read_bytes()
                            ).hexdigest(),
                            "byte_size": self.external.stat().st_size,
                            "source_url": "https://example.invalid/model",
                            "license": "FAKE TEST ONLY",
                            "license_url": "https://example.invalid/license",
                            "local_use_allowed": True,
                            "owned": False,
                            "worker_memory_bytes": 512 * 1024**2,
                        }
                    ],
                }
            ).encode()
        )
        root_id = uuid4().hex
        self.baseline = {
            "manifest_version": 1,
            "data_root_id": uuid4().hex,
            "schema_version": SCHEMA_VERSION,
            "storage_roots": [
                {
                    "root_id": root_id,
                    "path": str(self.folder / "offline unowned storage"),
                    "volume_id": "fake-offline-volume",
                }
            ],
            "default_storage_root_id": root_id,
            "component_installations": [
                {"name": "KEEP UNOWNED REGISTRY", "owned": False}
            ],
            "last_backup_id": uuid4().hex,
            "last_successful_update": {"update_id": uuid4().hex},
            "unknown_future_metadata": {"retain": True},
        }
        self.write_payload(self.baseline)

    def write_payload(self, payload):
        self.manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def dump(self):
        with closing(sqlite3.connect(self.database)) as db:
            return list(db.iterdump())

    def test_save_reload_preserves_root_registry_backup_update_domain_and_external(
        self,
    ):
        before = self.dump()
        before_bytes = self.external.read_bytes()
        with AppDataLock(self.root) as lock:
            selection = save_component_selection(self.root, self.specs, lock=lock)
            self.assertTrue(lock.held)
        self.assertEqual(load_component_selection(self.root), selection)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            {k: v for k, v in payload.items() if k != "component_selection"},
            self.baseline,
        )
        self.assertFalse(
            payload["component_selection"]["runtime_compatibility_verified"]
        )
        self.assertFalse(selection.specs[0].owned)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.external.read_bytes(), before_bytes)
        self.assertEqual(
            sorted(p.name for p in self.manifest.parent.iterdir()),
            ["storage-roots.json"],
        )

    def test_first_selection_creates_root_identity_once_without_installations_claim(
        self,
    ):
        self.manifest.unlink()  # owned fixture only, explicit setup
        self.assertIsNone(load_component_selection(self.root))
        before = self.dump()
        with AppDataLock(self.root) as lock:
            first = save_component_selection(self.root, self.specs, lock=lock)
            payload = json.loads(self.manifest.read_text(encoding="utf-8"))
            next_spec = replace(self.specs[0], version="fake-declaration-v2")
            second = save_component_selection(self.root, (next_spec,), lock=lock)
        later = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["data_root_id"], later["data_root_id"])
        self.assertEqual(later["storage_roots"], [])
        self.assertNotIn("component_installations", later)
        self.assertNotEqual(first.specs[0].version, second.specs[0].version)
        self.assertEqual(self.dump(), before)

    def test_held_app_lock_for_exact_root_required_and_not_released_by_service(self):
        original = self.manifest.read_bytes()
        lock = AppDataLock(self.root)
        with self.assertRaises(RuntimeError):
            save_component_selection(self.root, self.specs, lock=lock)
        other = self.folder / "another root"
        ensure_data_root(other)
        with AppDataLock(other) as other_lock:
            with self.assertRaises(RuntimeError):
                save_component_selection(self.root, self.specs, lock=other_lock)
            self.assertTrue(other_lock.held)
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_sqlite_writer_reservation_covers_manifest_publication_then_releases(self):
        from creator_loop.storage_roots import _write_manifest

        def check_reserved(path, payload):
            competitor = sqlite3.connect(self.database, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    competitor.execute("BEGIN IMMEDIATE")
            finally:
                competitor.close()
            return _write_manifest(path, payload)

        before = self.dump()
        with AppDataLock(self.root) as lock:
            with patch(
                "creator_loop.component_selection._write_manifest",
                side_effect=check_reserved,
            ) as writer:
                save_component_selection(self.root, self.specs, lock=lock)
            writer.assert_called_once()
        with closing(sqlite3.connect(self.database)) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            competitor.rollback()
        self.assertEqual(self.dump(), before)

    def test_hash_failure_and_cancel_preserve_previous_selection_and_domain(self):
        before = self.dump()
        with AppDataLock(self.root) as lock:
            save_component_selection(self.root, self.specs, lock=lock)
            original = self.manifest.read_bytes()
            with self.assertRaises(ComponentPreflightError):
                save_component_selection(
                    self.root, (replace(self.specs[0], sha256="0" * 64),), lock=lock
                )
            with self.assertRaises(InterruptedError):
                save_component_selection(
                    self.root, self.specs, lock=lock, cancelled=lambda: True
                )
            self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(self.dump(), before)
        with self.external.open("r+b") as reader:
            self.assertTrue(reader.read(1))

    def test_publication_cancel_after_hash_or_replace_failure_keeps_old_metadata(self):
        original = self.manifest.read_bytes()
        before = self.dump()
        from creator_loop.storage_roots import _write_manifest

        with AppDataLock(self.root) as lock:
            with patch(
                "creator_loop.storage_roots.os.replace",
                side_effect=PermissionError("fake sharing conflict"),
            ):
                with self.assertRaises(PermissionError):
                    save_component_selection(self.root, self.specs, lock=lock)
            self.assertEqual(self.manifest.read_bytes(), original)
            self.assertEqual(
                sorted(p.name for p in self.manifest.parent.iterdir()),
                ["storage-roots.json"],
            )
            with self.external.open("r+b") as reader:
                self.assertTrue(reader.read(1))
            from creator_loop.database import validate

            cancelled = False

            def validate_then_cancel(db):
                nonlocal cancelled
                validate(db)
                cancelled = True

            # Deliver cancellation after real hashing and DB validation, before publish.
            with (
                patch(
                    "creator_loop.component_selection._write_manifest",
                    wraps=_write_manifest,
                ) as writer,
                patch(
                    "creator_loop.component_selection.validate",
                    side_effect=validate_then_cancel,
                ),
            ):
                with self.assertRaises(InterruptedError):
                    save_component_selection(
                        self.root,
                        self.specs,
                        lock=lock,
                        cancelled=lambda: cancelled,
                    )
                writer.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(self.dump(), before)

    def test_history_read_is_not_fresh_check_and_changed_external_file_not_restored(
        self,
    ):
        with AppDataLock(self.root) as lock:
            selection = save_component_selection(self.root, self.specs, lock=lock)
            original = self.manifest.read_bytes()
            changed = b"USER CHANGED COMPONENT AFTER LEASE RELEASE"
            self.external.write_bytes(changed)
            self.assertEqual(load_component_selection(self.root), selection)
            with self.assertRaises(ComponentPreflightError):
                save_component_selection(self.root, selection.specs, lock=lock)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(self.external.read_bytes(), changed)

    def test_invalid_existing_history_schema_or_registry_not_overwritten(self):
        changes = [
            {"component_installations": "corrupt registry"},
            {"component_selection": {"check": "INVALID"}},
            {"schema_version": SCHEMA_VERSION - 1},
        ]
        with AppDataLock(self.root) as lock:
            for change in changes:
                self.write_payload({**self.baseline, **change})
                original = self.manifest.read_bytes()
                with (
                    self.subTest(change=change),
                    self.assertRaises(ComponentPreflightError),
                ):
                    save_component_selection(self.root, self.specs, lock=lock)
                self.assertEqual(self.manifest.read_bytes(), original)

    def test_metadata_budget_failure_and_corrupt_db_keep_previous_file(self):
        payload = {**self.baseline, "future_padding": ""}
        payload["future_padding"] = "x" * (
            1024**2 - 128 - len(json.dumps(payload, indent=2).encode())
        )
        self.write_payload(payload)
        original = self.manifest.read_bytes()
        with AppDataLock(self.root) as lock:
            with self.assertRaises(ComponentPreflightError):
                save_component_selection(self.root, self.specs, lock=lock)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.database.write_bytes(b"KEEP CORRUPT DB UNTOUCHED")
        corrupt = self.database.read_bytes()
        with AppDataLock(self.root) as lock:
            with self.assertRaises(sqlite3.DatabaseError):
                save_component_selection(self.root, self.specs, lock=lock)
        self.assertEqual(self.database.read_bytes(), corrupt)
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_aliased_manifest_or_database_refused_without_modifying_either_name(self):
        original = self.manifest.read_bytes()
        database_bytes = self.database.read_bytes()
        manifest_alias = self.folder / "unowned manifest alias.json"
        database_alias = self.folder / "unowned database alias.sqlite3"
        with AppDataLock(self.root) as lock:
            os.link(self.manifest, manifest_alias)
            with self.assertRaises(ComponentPreflightError):
                save_component_selection(self.root, self.specs, lock=lock)
            self.assertEqual(manifest_alias.read_bytes(), original)
            manifest_alias.unlink()  # only this test's synthetic alias
            os.link(self.database, database_alias)
            with self.assertRaises(ComponentPreflightError):
                save_component_selection(self.root, self.specs, lock=lock)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(self.database.read_bytes(), database_bytes)
        self.assertEqual(database_alias.read_bytes(), database_bytes)

    def test_history_cannot_claim_runtime_acceptance_or_accept_ambiguous_timestamp(
        self,
    ):
        with AppDataLock(self.root) as lock:
            save_component_selection(self.root, self.specs, lock=lock)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        for change in (
            {"runtime_compatibility_verified": True},
            {"checked_at": "2026-10-04T01:00:00"},
            {"checked_at": "invalid"},
            {"worker_memory_limit": True},
            {"worker_memory_limit": 64 * 1024**2},
        ):
            self.write_payload(
                {
                    **payload,
                    "component_selection": {**payload["component_selection"], **change},
                }
            )
            original = self.manifest.read_bytes()
            with (
                self.subTest(change=change),
                self.assertRaises(ComponentPreflightError),
            ):
                load_component_selection(self.root)
            self.assertEqual(self.manifest.read_bytes(), original)

    def cli_command(self):
        from dataclasses import asdict

        request = self.folder / "explicit external selection.json"
        request.write_text(
            json.dumps(
                {
                    "manifest_version": 1,
                    "components": [
                        {**asdict(self.specs[0]), "path": str(self.external)}
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "app"),
        }
        return [
            sys.executable,
            "-m",
            "creator_loop",
            "--select-components",
            str(request),
        ], env

    def test_actual_cli_malformed_registry_is_structured_failure_and_preserves_bytes(
        self,
    ):
        command, env = self.cli_command()
        self.manifest.write_bytes(b"KEEP MALFORMED REGISTRY")
        before = self.dump()
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selection"], "FAILED")
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), b"KEEP MALFORMED REGISTRY")
        self.assertEqual(self.dump(), before)

    def test_actual_cli_saves_without_domain_change_and_refuses_held_app_lock(self):
        command, env = self.cli_command()
        before = self.dump()
        original = self.manifest.read_bytes()
        with AppDataLock(self.root):
            result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.manifest.read_bytes(), original)
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["selection"], "SAVED")
        self.assertFalse(report["runtime_compatibility_verified"])
        self.assertEqual(report["component_ids"], ["fake-model"])
        self.assertEqual(load_component_selection(self.root).specs, self.specs)
        self.assertEqual(self.dump(), before)
        self.assertNotIn(self.external.read_bytes(), result.stdout + result.stderr)

    def test_actual_cli_refuses_missing_or_corrupt_db_without_initialization_or_migration(
        self,
    ):
        command, env = self.cli_command()
        original = self.manifest.read_bytes()
        self.database.write_bytes(b"KEEP CORRUPT DB")
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.database.read_bytes(), b"KEEP CORRUPT DB")
        self.assertEqual(self.manifest.read_bytes(), original)
        missing_root = self.folder / "must-not-create"
        result = subprocess.run(
            command,
            env={**env, "CREATOR_LOOP_DATA_ROOT": str(missing_root)},
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(missing_root.exists())
        result = subprocess.run(
            command + ["--smoke"], env=env, capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.database.read_bytes(), b"KEEP CORRUPT DB")
        self.assertEqual(self.manifest.read_bytes(), original)
