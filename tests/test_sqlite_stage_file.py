"""Native reserved file supports actual BackupAPI/migration and keeps failures."""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.database import SCHEMA_VERSION, initialize, validate
from creator_loop.sqlite_stage_file import hold_new_sqlite_target


class SqliteStageFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve() / "SQLite staged có space"
        self.folder.mkdir()
        self.target = self.folder / "restored.sqlite3"

    def test_real_backup_api_opens_reserved_file_and_validates_complete_schema(self):
        snapshot = self.folder / "validated-backup.sqlite3"
        initialize(snapshot)
        before = snapshot.read_bytes()
        with hold_new_sqlite_target(self.target) as held:
            with (
                closing(
                    sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
                ) as source,
                closing(
                    sqlite3.connect(self.target.as_uri() + "?mode=rw", uri=True)
                ) as target,
            ):
                target.execute("PRAGMA foreign_keys=ON")
                source.backup(target, pages=256)
                validate(target, expected_version=SCHEMA_VERSION)
            held.seek(0)
            self.assertGreater(len(held.read()), 0)
            if sys.platform == "win32":
                with self.assertRaises(OSError):
                    self.target.unlink()
                replacement = self.folder / "replacement"
                replacement.write_bytes(b"never publish")
                with self.assertRaises(OSError):
                    replacement.replace(self.target)
        with closing(sqlite3.connect(self.target)) as restored:
            restored.execute("PRAGMA foreign_keys=ON")
            validate(restored, expected_version=SCHEMA_VERSION)
        self.assertEqual(snapshot.read_bytes(), before)

    def test_existing_file_directory_or_hardlink_is_never_overwritten(self):
        self.target.write_bytes(b"KEEP PREEXISTING")
        with self.assertRaises(FileExistsError):
            with hold_new_sqlite_target(self.target):
                pass
        self.assertEqual(self.target.read_bytes(), b"KEEP PREEXISTING")
        alias = self.folder / "alias"
        os.link(self.target, alias)
        with self.assertRaises(FileExistsError):
            with hold_new_sqlite_target(alias):
                pass
        self.assertEqual(alias.read_bytes(), b"KEEP PREEXISTING")
        directory = self.folder / "existing directory"
        directory.mkdir()
        sentinel = directory / "keep.txt"
        sentinel.write_bytes(b"KEEP DIRECTORY")
        with self.assertRaises(OSError):
            with hold_new_sqlite_target(directory):
                pass
        self.assertEqual(sentinel.read_bytes(), b"KEEP DIRECTORY")

    def test_caller_failure_keeps_new_partial_file_and_closes_pin(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            with hold_new_sqlite_target(self.target) as held:
                held.write(b"KEEP PARTIAL STAGE")
                held.flush()
                raise RuntimeError("fixture failed")
        self.assertEqual(self.target.read_bytes(), b"KEEP PARTIAL STAGE")
        self.target.write_bytes(b"after pin released")

    @unittest.skipIf(sys.platform == "win32", "native pin prevents replacement")
    def test_portable_external_replacement_refuses_without_deleting_either_file(self):
        displaced = self.folder / "displaced"
        with self.assertRaises(RuntimeError):
            with hold_new_sqlite_target(self.target) as held:
                held.write(b"KEPT RESERVED")
                held.flush()
                self.target.rename(displaced)
                self.target.write_bytes(b"EXTERNAL REPLACEMENT")
        self.assertEqual(displaced.read_bytes(), b"KEPT RESERVED")
        self.assertEqual(self.target.read_bytes(), b"EXTERNAL REPLACEMENT")
