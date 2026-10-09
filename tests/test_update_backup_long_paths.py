"""Native long backup paths keep canonical IDs and cleanup only own staging."""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import SCHEMA_VERSION, _sqlite_uri, initialize
from creator_loop.paths import ensure_data_root
from creator_loop.update_activation import verify_prepared_backup
from creator_loop.update_backup import create_update_backup
from creator_loop.windows_paths import file_io_path


@unittest.skipUnless(sys.platform == "win32", "Windows extended backup I/O")
class LongBackupPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.root = Path(self.temp.name).resolve(strict=True)
        while len(str(self.root)) < 310:
            self.root /= "owned backup unicode segment"
        ensure_data_root(self.root)
        initialize(self.root / "creator_loop.sqlite3")
        self.media = self.root / "storage/originals/retained.txt"
        file_io_path(self.media).write_bytes(b"original bytes")
        self.digest = hashlib.sha256(b"original bytes").hexdigest()
        with closing(
            sqlite3.connect(_sqlite_uri(self.root / "creator_loop.sqlite3"), uri=True)
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("a", "TEXT", "retained", "2026-10-08T00:00:00Z", None),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "f",
                    "a",
                    "ORIGINAL",
                    "storage/originals/retained.txt",
                    self.digest,
                    14,
                    "text/plain",
                    "2026-10-08T00:00:00Z",
                ),
            )
            db.commit()

    def _cleanup(self):
        # This test owns the freshly allocated temporary root, never user data.
        owned = Path(self.temp.name).resolve(strict=True)
        self.assertTrue(self.root.is_relative_to(owned))
        shutil.rmtree(file_io_path(owned))
        self.temp.cleanup()

    def test_deep_backup_and_readback_need_no_registry_or_short_alias(self):
        target = create_update_backup(self.root)
        manifest = json.loads(
            file_io_path(target / "backup-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(target.parent, self.root / "backups")
        self.assertFalse(str(target).startswith("\\\\?\\"))
        self.assertEqual(manifest["backup_id"], target.name)
        self.assertEqual(manifest["storage_references"][0]["sha256"], self.digest)
        self.assertEqual(file_io_path(self.media).read_bytes(), b"original bytes")

        db = sqlite3.connect(
            _sqlite_uri(target / "creator_loop.sqlite3") + "?mode=ro", uri=True
        )
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            db.close()

    def test_deep_failure_preserves_prior_backup_and_original(self):
        prior = create_update_backup(self.root)
        old = file_io_path(prior / "creator_loop.sqlite3").read_bytes()
        original = file_io_path(self.root / "creator_loop.sqlite3").read_bytes()
        with patch(
            "creator_loop.update_backup.os.fsync",
            side_effect=OSError("simulated write failure"),
        ):
            with self.assertRaises(OSError):
                create_update_backup(self.root)
        self.assertEqual(file_io_path(prior / "creator_loop.sqlite3").read_bytes(), old)
        self.assertEqual(
            file_io_path(self.root / "creator_loop.sqlite3").read_bytes(), original
        )
        self.assertEqual(
            [p.name for p in file_io_path(self.root / "backups").iterdir()],
            [prior.name],
        )

    def test_activation_reads_same_deep_backup_and_rejects_changed_snapshot(self):
        target = create_update_backup(self.root)
        record = {"backup_id": target.name, "schema_from": SCHEMA_VERSION}
        snapshot = target / "creator_loop.sqlite3"
        before = file_io_path(self.root / "creator_loop.sqlite3").read_bytes()
        original = file_io_path(snapshot).read_bytes()
        self.assertEqual(verify_prepared_backup(self.root, record), snapshot)
        self.assertFalse(str(snapshot).startswith("\\\\?\\"))
        metadata_path = target / "backup-manifest.json"
        metadata = json.loads(file_io_path(metadata_path).read_text(encoding="utf-8"))
        metadata["backup_id"] = "b" * 32
        file_io_path(metadata_path).write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest or identity"):
            verify_prepared_backup(self.root, record)
        metadata["backup_id"] = target.name
        file_io_path(metadata_path).write_text(json.dumps(metadata), encoding="utf-8")
        file_io_path(snapshot).write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "digest or identity"):
            verify_prepared_backup(self.root, record)
        self.assertEqual(
            file_io_path(self.root / "creator_loop.sqlite3").read_bytes(), before
        )
        self.assertEqual(file_io_path(self.media).read_bytes(), b"original bytes")
