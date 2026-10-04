"""Read-only archive evidence validation never grants damaged-source restore."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.corrupt_database import hold_corrupt_database
from creator_loop.corrupt_source_preservation import preserve_corrupt_source
from creator_loop.paths import ensure_data_root
from creator_loop.preserved_source_validation import hold_preserved_source


class PreservedSourceValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "dữ liệu archive space"
        ensure_data_root(self.root)
        self.source = self.root / "creator_loop.sqlite3"
        self.original = b"PRIVATE RAW DAMAGE\x00\xff" * 4096
        self.source.write_bytes(self.original)
        for suffix in ("-wal", "-shm", "-journal"):
            self.source.with_name(self.source.name + suffix).write_bytes(
                b"PRIVATE SIDECAR " + suffix.encode()
            )
        with hold_corrupt_database(self.root) as damage:
            self.token = damage["damage_identity"]
        self.manifest = preserve_corrupt_source(self.root, self.token)
        self.folder = self.manifest.parent
        self.record = json.loads(self.manifest.read_text(encoding="utf-8"))

    def snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in self.folder.iterdir()
            if path.is_file()
        }

    def verify(self):
        return hold_preserved_source(self.root, self.manifest, self.token)

    def test_fresh_held_bytes_revalidated_without_opening_live_db_or_restore_claim(
        self,
    ):
        before = self.snapshot()
        with (
            patch(
                "sqlite3.connect", side_effect=AssertionError("must not open SQLite")
            ),
            self.verify() as result,
        ):
            self.assertTrue(result["archive_revalidated"])
            self.assertEqual(result["damage_identity"], self.token)
            self.assertEqual(
                result["archive_manifest_sha256"],
                hashlib.sha256(before[self.manifest.name]).hexdigest(),
            )
            self.assertFalse(result["consistent_backup"])
            self.assertFalse(result["restore_authorized"])
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_missing_live_db_is_not_initialized_by_archive_recovery_read(self):
        self.source.unlink()  # own synthetic fixture, not production data
        with self.verify() as result:
            self.assertTrue(result["archive_revalidated"])
        self.assertFalse(self.source.exists())

    def test_live_source_changes_not_mistaken_for_fresh_restore_review(self):
        self.source.write_bytes(b"changed live source after retention")
        with self.verify() as result:
            self.assertFalse(result["restore_authorized"])
            self.assertEqual(
                (self.folder / self.source.name).read_bytes(), self.original
            )
        self.assertEqual(
            self.source.read_bytes(), b"changed live source after retention"
        )

    def test_stale_or_other_root_identity_refuses_and_preserves_archive(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            with hold_preserved_source(self.root, self.manifest, "0" * 64):
                pass
        other = self.root.parent / "other root"
        ensure_data_root(other)
        folder = other / "backups" / self.folder.name
        folder.mkdir()
        for name, body in before.items():
            (folder / name).write_bytes(body)
        with self.assertRaises(ValueError):
            with hold_preserved_source(other, folder / self.manifest.name, self.token):
                pass
        self.assertEqual(self.snapshot(), before)

    def test_tampered_raw_bytes_refuse_without_repair_delete_or_success(self):
        wal = self.folder / (self.source.name + "-wal")
        wal.write_bytes(b"tampered retained WAL")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            with self.verify():
                pass
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_partial_unknown_and_missing_inventory_refuse_preserving_evidence(self):
        extra = self.folder / "raw-source-manifest.json.pending"
        extra.write_bytes(b"KEEP PARTIAL")
        with self.assertRaises(ValueError):
            with self.verify():
                pass
        self.assertEqual(extra.read_bytes(), b"KEEP PARTIAL")
        extra.unlink()  # own synthetic extra; verify missing-file refusal separately
        wal = self.folder / (self.source.name + "-wal")
        wal.unlink()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            with self.verify():
                pass
        self.assertEqual(self.snapshot(), before)

    def test_hardlinked_manifest_or_raw_file_refuses_both_paths_untouched(self):
        for selected in (self.manifest, self.folder / self.source.name):
            with self.subTest(selected=selected):
                alias = self.root / "owned test alias"
                os.link(selected, alias)
                try:
                    before = selected.read_bytes()
                    with self.assertRaises(ValueError):
                        with self.verify():
                            pass
                    self.assertEqual(selected.read_bytes(), before)
                    self.assertEqual(alias.read_bytes(), before)
                finally:
                    alias.unlink()

    def test_directory_in_place_of_raw_file_is_not_traversed(self):
        raw = self.folder / (self.source.name + "-shm")
        raw.unlink()
        raw.mkdir()
        (raw / "keep.txt").write_bytes(b"KEEP UNKNOWN")
        with self.assertRaises(ValueError):
            with self.verify():
                pass
        self.assertEqual((raw / "keep.txt").read_bytes(), b"KEEP UNKNOWN")

    def test_duplicate_json_oversized_and_unsupported_claims_refuse(self):
        bodies = [
            '{"raw_source_format":1,"raw_source_format":1}',
            " " * 16385,
        ]
        for key, value in (
            ("raw_source_format", True),
            ("consistent_backup", True),
            ("restore_authorized", True),
            ("raw_bytes_verified", False),
            ("created_at", "2026-10-04T00:00:00"),
            ("app_version", "PRIVATE invalid version"),
            ("unrecognized", True),
        ):
            record = {**self.record, key: value}
            bodies.append(json.dumps(record))
        for body in bodies:
            with self.subTest(body_size=len(body)):
                self.manifest.write_text(body, encoding="utf-8")
                before = self.snapshot()
                with self.assertRaises(ValueError) as raised:
                    with self.verify():
                        pass
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertEqual(self.snapshot(), before)

    def test_damage_inventory_identity_cannot_be_changed_without_review(self):
        record = json.loads(json.dumps(self.record))
        record["damage"]["raw_files"][0]["sha256"] = "a" * 64
        self.manifest.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            with self.verify():
                pass

    def test_cancel_deadline_and_invalid_budget_close_archive_leases(self):
        before = self.snapshot()
        for options, error in (
            ({"cancelled": lambda: True}, InterruptedError),
            ({"timeout_seconds": float("inf")}, ValueError),
            ({"timeout_seconds": 0}, ValueError),
        ):
            with self.subTest(options=options), self.assertRaises(error):
                with hold_preserved_source(
                    self.root, self.manifest, self.token, **options
                ):
                    pass
        with (
            patch(
                "creator_loop.preserved_source_validation.time.monotonic",
                side_effect=range(100),
            ),
            self.assertRaises(TimeoutError),
        ):
            with hold_preserved_source(
                self.root, self.manifest, self.token, timeout_seconds=0.1
            ):
                pass
        with self.verify():
            pass
        self.assertEqual(self.snapshot(), before)
        self.manifest.write_bytes(before[self.manifest.name])

    def test_cancel_during_held_hash_and_caller_exception_release_all_files(self):
        before = self.snapshot()
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 12

        with self.assertRaises(InterruptedError):
            with hold_preserved_source(
                self.root, self.manifest, self.token, cancelled=cancelled
            ):
                pass
        self.assertGreaterEqual(calls, 12)
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with self.verify():
                raise RuntimeError("caller failed")
        self.assertEqual(self.snapshot(), before)
        for name, body in before.items():
            (self.folder / name).write_bytes(body)

    def test_noncanonical_selection_outside_archive_namespace_refuses(self):
        before = self.snapshot()
        for selected in (
            self.root / self.manifest.name,
            self.manifest.parent / "not-the-manifest.json",
            self.manifest.parent / ".." / self.folder.name / self.manifest.name,
        ):
            with self.subTest(path=selected), self.assertRaises((ValueError, OSError)):
                with hold_preserved_source(self.root, selected, self.token):
                    pass
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(sys.platform == "win32", "native archive sharing lease")
    def test_native_archive_source_and_manifest_remain_locked_through_caller(self):
        before = self.snapshot()
        with self.verify():
            for path in (self.manifest, self.folder / self.source.name):
                with self.assertRaises(OSError):
                    path.write_bytes(b"bad mutation")
                with self.assertRaises(OSError):
                    path.unlink()
            with self.assertRaises(OSError):
                self.folder.rename(self.folder.with_name("MOVED"))
            with self.assertRaises(OSError):
                self.root.rename(self.root.with_name("MOVED ROOT"))
        self.assertEqual(self.snapshot(), before)

    def test_cli_archive_read_with_missing_live_db_and_mixed_arguments(self):
        self.source.unlink()  # own fixture only
        before = self.snapshot()
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--verify-preserved-source",
            str(self.manifest),
            "--reviewed-damage",
            self.token,
        ]
        mixed = subprocess.run(
            command + ["--smoke"], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(mixed.returncode, 2, mixed.stderr)
        verified = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        record = json.loads(verified.stdout)
        self.assertTrue(record["archive_revalidated"])
        self.assertFalse(record["current_source_assessed"])
        self.assertFalse(record["restore_authorized"])
        self.assertFalse(record["restored"])
        self.assertNotIn(b"PRIVATE", verified.stdout + verified.stderr)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.snapshot(), before)
        stale = subprocess.run(
            command[:-1] + ["0" * 64], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(stale.returncode, 4, stale.stderr)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.snapshot(), before)

    @unittest.skipIf(sys.platform == "win32", "native lease denies replacement")
    def test_portable_post_yield_path_change_is_detected_without_cleanup(self):
        raw = self.folder / self.source.name
        with self.assertRaises(RuntimeError):
            with self.verify():
                raw.write_bytes(b"external archive change")
        self.assertEqual(raw.read_bytes(), b"external archive change")
