"""Real Windows bootstrap and SQLite backup at a deep Unicode user data root."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from creator_loop.__main__ import main
from creator_loop.app_lock import AppDataLock
from creator_loop.component_selection import (
    load_component_selection,
    save_component_selection,
)
from creator_loop.database import (
    SCHEMA_VERSION,
    _connect_write,
    backup,
    initialize,
    migration_path,
    open_readonly,
    validate,
)
from creator_loop.local_components import ComponentSpec
from creator_loop.paths import ensure_data_root
from creator_loop.storage_roots import StorageRootError


@unittest.skipUnless(sys.platform == "win32", "native Windows deep user data root")
class LongDataRootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        prefix = "dữ liệu mới-"
        self.root = self.base / (
            prefix + "x" * (239 - len(str(self.base)) - 1 - len(prefix))
        )
        self.database = self.root / "creator_loop.sqlite3"
        self.addCleanup(self._clean_fixture)

    def io_path(self, path):
        # Independent observer; no implementation path helper or seeded DB.
        return Path("\\\\?\\" + str(path))

    def _clean_fixture(self):
        if self.io_path(self.root).exists():
            target = self.io_path(self.root).resolve(strict=True)
            base = self.io_path(self.base).resolve(strict=True)
            self.assertTrue(target.is_relative_to(base))
            self.assertEqual(target.relative_to(base), self.root.relative_to(self.base))
            shutil.rmtree(target)

    def test_new_root_initializes_then_reopens_schema_without_replacing_data(self):
        self.assertFalse(self.io_path(self.root).exists())
        ensure_data_root(self.root)
        with AppDataLock(self.root):
            initialize(self.database)
            with closing(_connect_write(self.database)) as db:
                db.execute(
                    "INSERT INTO assets VALUES (?,?,?,?,?)",
                    ("keep", "TEXT", "retained", "2026-10-06T00:00:00Z", None),
                )
                db.commit()
            initialize(self.database)
        for name in (
            "storage/originals",
            "storage/derived",
            "models",
            "components",
            "manifests",
            "backups",
            "logs",
            "runtime",
        ):
            self.assertTrue(self.io_path(self.root / name).is_dir(), name)
        with closing(open_readonly(self.database)) as db:
            validate(db)
            self.assertEqual(
                db.execute(
                    "SELECT display_name FROM assets WHERE asset_id='keep'"
                ).fetchone(),
                ("retained",),
            )
        self.assertEqual(len(str(self.database)), 260)

    def test_real_main_smoke_bootstraps_new_unicode_root_with_canonical_path(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CREATOR_LOOP_DATA_ROOT": str(self.root)}),
            patch.object(sys, "argv", ["creator-loop", "--smoke"]),
            redirect_stdout(output),
        ):
            self.assertEqual(main(), 0)
        self.assertIn(str(self.database), output.getvalue())
        self.assertNotIn("\\\\?\\", output.getvalue())
        with closing(open_readonly(self.database)) as db:
            validate(db)

    def test_backup_api_captures_committed_wal_and_preserves_source(self):
        ensure_data_root(self.root)
        initialize(self.database)
        destination = self.root / "backups/snapshot.sqlite3"
        with closing(_connect_write(self.database)) as writer:
            writer.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("wal", "TEXT", "live WAL", "2026-10-06T00:00:00Z", None),
            )
            writer.commit()
            self.assertTrue(self.io_path(Path(str(self.database) + "-wal")).is_file())
            backup(self.database, destination)
            self.assertEqual(
                writer.execute("SELECT count(*) FROM assets").fetchone()[0], 1
            )
            with closing(open_readonly(destination)) as snapshot:
                validate(snapshot)
                self.assertEqual(
                    snapshot.execute(
                        "SELECT display_name FROM assets WHERE asset_id='wal'"
                    ).fetchone(),
                    ("live WAL",),
                )
            writer.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("later", "TEXT", "later", "2026-10-06T00:00:01Z", None),
            )
            writer.commit()
            with closing(open_readonly(destination)) as snapshot:
                self.assertEqual(
                    snapshot.execute("SELECT count(*) FROM assets").fetchone()[0], 1
                )

    def test_new_deep_root_does_not_modify_unknown_future_schema(self):
        ensure_data_root(self.root)
        # Create the incompatible fixture through an independent SQLite spelling.
        with closing(sqlite3.connect(self.io_path(self.database))) as db:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            db.execute("CREATE TABLE retained(value TEXT)")
            db.execute("INSERT INTO retained VALUES ('keep unknown bytes')")
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "Unsupported schema version"):
            initialize(self.database)
        with closing(sqlite3.connect(self.io_path(self.database))) as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1
            )
            self.assertEqual(
                db.execute("SELECT value FROM retained").fetchone()[0],
                "keep unknown bytes",
            )

    def test_deep_restore_guard_blocks_bootstrap_without_creating_database(self):
        # Presence alone must fail closed, including malformed guard bytes.
        marker = self.root / "runtime/restore-in-progress.json"
        self.io_path(marker.parent).mkdir(parents=True)
        self.io_path(marker).write_bytes(b"PRESERVE MALFORMED RESTORE GUARD")
        self.assertGreater(len(str(marker)), 260)
        with (
            patch.dict(os.environ, {"CREATOR_LOOP_DATA_ROOT": str(self.root)}),
            patch.object(sys, "argv", ["creator-loop", "--smoke"]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 4)
        self.assertFalse(self.io_path(self.database).exists())
        self.assertEqual(
            self.io_path(marker).read_bytes(), b"PRESERVE MALFORMED RESTORE GUARD"
        )

    def test_deep_legacy_migration_keeps_verified_preupgrade_backup_and_asset(self):
        ensure_data_root(self.root)
        sql = migration_path().read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.io_path(self.database))) as db:
            db.executescript(sql)
            db.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                (
                    "0001_initial",
                    hashlib.sha256(sql.encode()).hexdigest(),
                    "2026-10-06T00:00:00Z",
                    "0.1.0",
                ),
            )
            db.execute("PRAGMA user_version=1")
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("legacy", "TEXT", "retained legacy", "2026-10-06T00:00:00Z", None),
            )
            db.commit()
        with AppDataLock(self.root):
            initialize(self.database)
        with closing(open_readonly(self.database)) as db:
            validate(db)
            self.assertEqual(
                db.execute(
                    "SELECT display_name FROM assets WHERE asset_id='legacy'"
                ).fetchone(),
                ("retained legacy",),
            )
        snapshots = list(
            self.io_path(self.root).glob(
                f"creator_loop.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3"
            )
        )
        self.assertEqual(len(snapshots), 1)
        with closing(open_readonly(snapshots[0])) as before:
            validate(before, expected_version=1)
            self.assertEqual(
                before.execute("SELECT count(*) FROM assets").fetchone()[0], 1
            )

    def test_new_deep_root_saves_selection_without_overwriting_malformed_history(self):
        ensure_data_root(self.root)
        initialize(self.database)
        model = self.base / "unowned-fake-model.fixture"
        content = b"FAKE MODEL TEST ONLY, NEVER EXECUTED"
        model.write_bytes(content)
        spec = ComponentSpec(
            "fake-model",
            "MODEL",
            model,
            "test-v1",
            hashlib.sha256(content).hexdigest(),
            len(content),
            "https://example.invalid/model",
            "TEST ONLY",
            "https://example.invalid/license",
            64 * 1024**2,
            True,
            False,
        )
        manifest = self.root / "manifests/storage-roots.json"
        before = self.io_path(self.database).read_bytes()
        with AppDataLock(self.root) as lock:
            selection = save_component_selection(self.root, (spec,), lock=lock)
        self.assertEqual(load_component_selection(self.root), selection)
        metadata = json.loads(self.io_path(manifest).read_bytes())
        self.assertFalse(
            metadata["component_selection"]["runtime_compatibility_verified"]
        )
        self.assertEqual(self.io_path(self.database).read_bytes(), before)
        self.assertEqual(model.read_bytes(), content)
        self.io_path(manifest).write_bytes(b"{ malformed prior metadata")
        with AppDataLock(self.root) as lock, self.assertRaises(StorageRootError):
            save_component_selection(self.root, (spec,), lock=lock)
        self.assertEqual(
            self.io_path(manifest).read_bytes(), b"{ malformed prior metadata"
        )
        self.assertEqual(self.io_path(self.database).read_bytes(), before)
        self.assertEqual(
            [p.name for p in self.io_path(manifest.parent).iterdir()], [manifest.name]
        )
