"""Real source/sidecar bytes retained; damage inspection cannot authorize restore."""

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
from creator_loop.corrupt_database import hold_corrupt_database
from creator_loop.paths import ensure_data_root


class CorruptDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True) / "data with space"
        ensure_data_root(self.root)
        self.source = self.root / "creator_loop.sqlite3"
        self.content = b"PRIVATE DAMAGED SQLITE BYTES\x00\xff" * 4096
        self.source.write_bytes(self.content)

    def snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.is_file()
        }

    def test_real_notadb_and_sidecars_kept_without_private_bytes_or_restore_grant(self):
        for suffix in ("-wal", "-shm", "-journal"):
            self.source.with_name(self.source.name + suffix).write_bytes(
                b"PRIVATE SIDECAR " + suffix.encode()
            )
        before = self.snapshot()
        with hold_corrupt_database(self.root) as result:
            self.assertEqual(result["sqlite_primary_error"], sqlite3.SQLITE_NOTADB)
            self.assertEqual(result["sqlite_view"], "immutable_readonly_inspection")
            self.assertFalse(result["wal_recoverability_assessed"])
            self.assertFalse(result["consistent_backup"])
            self.assertFalse(result["current_changes_assessable"])
            self.assertFalse(result["restore_authorized"])
            for entry in result["raw_files"]:
                self.assertTrue(entry["present"])
                self.assertEqual(entry["byte_size"], len(before[entry["name"]]))
                self.assertEqual(
                    entry["sha256"], hashlib.sha256(before[entry["name"]]).hexdigest()
                )
            self.assertNotIn("PRIVATE", json.dumps(result))
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    pass
        self.assertEqual(self.snapshot(), before)
        with AppDataLock(self.root):
            pass  # error/success release app lock; leave its coordination file

    def test_readable_or_future_schema_is_not_damage_and_creates_no_sidecars(self):
        self.source.unlink()  # own synthetic fixture only
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE private_data(value)")
            db.execute("PRAGMA user_version=999")
            db.execute("INSERT INTO private_data VALUES ('KEEP ME')")
            db.commit()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Readable database"):
            with hold_corrupt_database(self.root):
                self.fail("Future readable schema must not authorize corrupt restore")
        self.assertEqual(self.snapshot(), before)

    def test_real_corrupt_btree_is_reported_without_repair_or_content_output(self):
        self.source.unlink()  # own synthetic fixture only
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE private_data(value)")
            db.execute("INSERT INTO private_data VALUES ('PRIVATE CORRUPT PAGE')")
            db.commit()
        damaged = bytearray(self.source.read_bytes())
        damaged[100:108] = b"\xff" * 8  # damaged page-one B-tree header
        self.source.write_bytes(damaged)
        before = self.snapshot()
        with hold_corrupt_database(self.root) as result:
            self.assertEqual(result["sqlite_primary_error"], sqlite3.SQLITE_CORRUPT)
            self.assertFalse(result["restore_authorized"])
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.snapshot(), before)

    def test_missing_source_runtime_guard_and_directory_sidecar_refuse(self):
        self.source.unlink()
        with self.assertRaises(ValueError):
            with hold_corrupt_database(self.root):
                pass
        self.assertFalse(self.source.exists())
        self.source.write_bytes(self.content)
        marker = self.root / "runtime/restore-in-progress.json"
        marker.write_text("KEEP UNKNOWN")
        with self.assertRaises(RuntimeError):
            with hold_corrupt_database(self.root):
                pass
        self.assertEqual(marker.read_text(), "KEEP UNKNOWN")
        marker.unlink()  # own synthetic fixture only
        self.source.with_name(self.source.name + "-wal").mkdir()
        with self.assertRaises(ValueError):
            with hold_corrupt_database(self.root):
                pass
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_real_crash_left_wal_is_not_replayed_or_changed_by_inspection(self):
        self.source.unlink()  # own synthetic fixture only
        script = (
            "import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1]); "
            "d.execute('PRAGMA journal_mode=WAL'); "
            "d.execute('CREATE TABLE private_data(value)'); "
            "d.execute(\"INSERT INTO private_data VALUES ('PRIVATE WAL DATA')\"); "
            "d.commit(); os._exit(0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.source)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.source.with_name(self.source.name + "-wal").exists())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Readable database"):
            with hold_corrupt_database(self.root):
                pass
        self.assertEqual(self.snapshot(), before)

    def test_cancellation_during_hash_releases_native_source_lease(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 8

        with self.assertRaises(InterruptedError):
            with hold_corrupt_database(self.root, cancelled=cancelled):
                pass
        self.assertGreaterEqual(calls, 8)
        self.assertEqual(self.source.read_bytes(), self.content)
        self.source.write_bytes(self.content + b"after cancelled lease")

    def test_hardlinked_source_refuses_and_retains_both_paths(self):
        alias = self.root / "alias"
        os.link(self.source, alias)
        with self.assertRaises(ValueError):
            with hold_corrupt_database(self.root):
                pass
        self.assertEqual(alias.read_bytes(), self.content)
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_cancellation_and_deadline_release_handles_preserve_bytes(self):
        for options, error in (
            ({"cancelled": lambda: True}, InterruptedError),
            ({"timeout_seconds": 1e-9}, TimeoutError),
            ({"timeout_seconds": float("inf")}, ValueError),
        ):
            with self.subTest(options=options), self.assertRaises(error):
                with hold_corrupt_database(self.root, **options):
                    pass
        with hold_corrupt_database(self.root):
            pass
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_io_error_is_not_sqlite_corruption_and_releases_app_lock(self):
        error = sqlite3.OperationalError("PRIVATE DIAGNOSTIC")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        with patch("creator_loop.corrupt_database.sqlite3.connect", side_effect=error):
            with self.assertRaises(sqlite3.OperationalError):
                with hold_corrupt_database(self.root):
                    pass
        with AppDataLock(self.root):
            pass
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_sub_tick_deadline_expires_even_when_coarse_clock_is_unchanged(self):
        before = self.snapshot()
        with (
            patch("creator_loop.corrupt_database.time.monotonic", return_value=100.0),
            patch(
                "creator_loop.corrupt_database.time.perf_counter",
                side_effect=[100.0, 100.001],
            ),
        ):
            with self.assertRaises(TimeoutError):
                with hold_corrupt_database(self.root, timeout_seconds=1e-9):
                    pass
        self.assertEqual(self.snapshot(), before)
        with AppDataLock(self.root):
            pass

    @unittest.skipUnless(sys.platform == "win32", "native sharing lease")
    def test_windows_all_source_handles_deny_write_delete_replace_until_release(self):
        wal = self.source.with_name(self.source.name + "-wal")
        wal.write_bytes(b"KEEP WAL")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"NEVER PUBLISH")
        with hold_corrupt_database(self.root):
            for path in (self.source, wal):
                with self.subTest(path=path):
                    with self.assertRaises(OSError):
                        path.write_bytes(b"bad mutation")
                    with self.assertRaises(OSError):
                        path.unlink()
                    with self.assertRaises(OSError):
                        replacement.replace(path)
        self.assertEqual(self.source.read_bytes(), self.content)
        self.assertEqual(wal.read_bytes(), b"KEEP WAL")
        self.source.write_bytes(self.content + b"after lease")

    @unittest.skipIf(sys.platform == "win32", "Windows lease prevents this mutation")
    def test_portable_change_after_damage_detection_refuses_stale_result(self):
        from creator_loop.corrupt_database import _damage_code

        def mutated(*args):
            code = _damage_code(*args)
            self.source.write_bytes(b"changed by external writer")
            return code

        with patch("creator_loop.corrupt_database._damage_code", side_effect=mutated):
            with self.assertRaises(RuntimeError):
                with hold_corrupt_database(self.root):
                    pass
        self.assertEqual(self.source.read_bytes(), b"changed by external writer")
