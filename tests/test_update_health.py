"""Read-only health refuses incompatible/missing data and owns its launcher tree."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import SCHEMA_VERSION, initialize
from creator_loop.paths import ensure_data_root
from creator_loop.update_health import readonly_health, run_health_check


class ReadonlyHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Health dữ liệu"
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)

    def test_empty_database_health_does_not_change_bytes_or_version(self):
        before = self.path.read_bytes()
        self.assertEqual(
            readonly_health(self.root),
            {
                "health_format": 1,
                "schema_version": SCHEMA_VERSION,
                "query_ok": True,
                "storage_reference": "not_applicable",
            },
        )
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(sys.platform == "win32", "Windows frozen runtime preflight")
    def test_broken_frozen_qt_cannot_report_database_only_health_success(self):
        before = self.path.read_bytes()
        with (
            patch("sys.frozen", True, create=True),
            patch(
                "importlib.import_module",
                side_effect=ImportError("fixture DLL unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Bundled Qt runtime"):
                readonly_health(self.root)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(sys.platform == "win32", "Windows frozen plugin preflight")
    def test_importable_qt_with_invalid_owned_plugin_refuses_health(self):
        before = self.path.read_bytes()
        bundle = self.root / "private bundle"
        platforms = bundle / "PySide6/plugins/platforms"
        platforms.mkdir(parents=True)
        (platforms / "qwindows.dll").write_bytes(b"invalid bundled plugin")
        with (
            patch("sys.frozen", True, create=True),
            patch("sys._MEIPASS", str(bundle), create=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "Bundled Qt platform plugin"):
                readonly_health(self.root)
        self.assertEqual(self.path.read_bytes(), before)

    def test_health_resolves_actual_sample_and_refuses_missing_media(self):
        media = self.root / "storage/originals/sample.txt"
        media.write_bytes(b"sample")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("a", "TEXT", "fixture", "2026-10-03T00:00:00Z", None),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "f",
                    "a",
                    "ORIGINAL",
                    "storage/originals/sample.txt",
                    hashlib.sha256(b"sample").hexdigest(),
                    6,
                    "text/plain",
                    "2026-10-03T00:00:00Z",
                ),
            )
            db.commit()
        before = self.path.read_bytes()
        self.assertEqual(readonly_health(self.root)["storage_reference"], "resolved")
        media.unlink()
        with self.assertRaisesRegex(RuntimeError, "storage reference"):
            readonly_health(self.root)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_or_old_schema_is_not_initialized_or_migrated(self):
        self.path.unlink()
        with self.assertRaises(OSError):
            readonly_health(self.root)
        self.assertFalse(self.path.exists())
        initialize(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Schema version"):
            readonly_health(self.root)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(
        sys.platform == "win32", "requires Windows owned health process"
    )
    def test_actual_source_launcher_health_under_parent_writer_reservation(self):
        old_path = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("BEGIN IMMEDIATE")
                result = run_health_check(
                    Path(sys.executable),
                    self.root,
                    self.root / "logs/source-health",
                    component_version="0.1.0",
                    arguments_prefix=["-m", "creator_loop"],
                )
                db.rollback()
            self.assertTrue(result["query_ok"])
            record = json.loads(
                (self.root / "logs/source-health/result.json").read_text()
            )
            self.assertEqual(record["exit_code"], 0)
            self.assertFalse(record["timed_out"])
        finally:
            if old_path is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = old_path

    @unittest.skipUnless(
        sys.platform == "win32", "requires Windows owned health process"
    )
    def test_timeout_and_nonzero_health_keep_database_unchanged_with_logs(self):
        before = self.path.read_bytes()
        with self.assertRaises(TimeoutError):
            run_health_check(
                Path(sys.executable),
                self.root,
                self.root / "logs/timeout-health",
                component_version="fake-1",
                timeout_seconds=0.1,
                arguments_prefix=["-c", "import time; time.sleep(60)"],
            )
        with self.assertRaisesRegex(RuntimeError, "health failed"):
            run_health_check(
                Path(sys.executable),
                self.root,
                self.root / "logs/failed-health",
                component_version="fake-1",
                arguments_prefix=["-c", "import sys; sys.exit(9)"],
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.root / "logs/timeout-health/result.json").is_file())

    @unittest.skipUnless(
        sys.platform == "win32", "requires Windows owned health process"
    )
    def test_success_exit_does_not_accept_invalid_health_or_unbounded_metadata(self):
        before = self.path.read_bytes()
        cases = (
            ("print('{}')", "result is invalid"),
            ("print('x'*1048577)", "metadata budget"),
            ("print('not JSON')", None),
        )
        for index, (script, message) in enumerate(cases):
            with self.subTest(script=script):
                error = RuntimeError if message else json.JSONDecodeError
                with self.assertRaises(error):
                    run_health_check(
                        Path(sys.executable),
                        self.root,
                        self.root / f"logs/invalid-health-{index}",
                        component_version="fake-1",
                        arguments_prefix=["-c", script],
                    )
        self.assertEqual(self.path.read_bytes(), before)
