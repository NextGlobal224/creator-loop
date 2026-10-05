"""Long native lock/read lease paths preserve canonical identities and sharing."""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.component_command import ComponentCommand
from creator_loop.component_selection import (
    load_component_selection,
    save_component_selection,
)
from creator_loop.database import SCHEMA_VERSION, initialize, open_readonly, validate
from creator_loop.local_components import ComponentSpec
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.storage_roots import StorageRootError
from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "native Windows long paths")
class NativeComponentPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        self.root = self.base / ("r" * (239 - len(str(self.base)) - 1))
        self.root.mkdir()
        (self.root / "runtime").mkdir()
        self.addCleanup(self._clean_fixture)

    def io(self, path):
        # Independent observer spelling; does not call the implementation helper.
        return Path("\\\\?\\" + str(path))

    def _clean_fixture(self):
        base = self.io(self.base).resolve(strict=True)
        target = self.io(self.root).resolve(strict=True)
        self.assertTrue(target.is_relative_to(base))
        self.assertEqual(target.relative_to(base), self.root.relative_to(self.base))
        shutil.rmtree(target)

    def test_deep_persistent_lock_excludes_second_owner_and_reopens_after_exit(self):
        lock = AppDataLock(self.root)
        self.assertEqual(len(str(lock.path)), 261)
        self.assertEqual(lock.root, self.root)
        with lock:
            self.assertTrue(lock.held)
            with self.assertRaises(DataRootBusy):
                with AppDataLock(self.root):
                    self.fail("Second owner acquired the live lock")
        self.assertFalse(lock.held)
        self.assertTrue(self.io(lock.path).is_file())
        with AppDataLock(self.root) as reopened:
            self.assertTrue(reopened.held)
            self.assertEqual(reopened.path, lock.path)

    def test_deep_readonly_lease_keeps_identity_bytes_and_write_delete_denial(self):
        regular = self.root / ("file-" + "x" * 25)
        self.assertEqual(len(str(regular)), 270)
        payload = json.dumps({"fixture": True}).encode()
        self.io(regular).write_bytes(payload)
        with RuntimeHandle(regular, read_only=True) as held:
            self.assertEqual(held.path, regular)
            self.assertEqual(held.read_json(), {"fixture": True})
            with self.assertRaises(OSError):
                self.io(regular).open("r+b")
            with self.assertRaises(OSError):
                self.io(regular).unlink()
        with self.io(regular).open("r+b") as stream:
            self.assertEqual(stream.read(), payload)
        self.assertEqual(str(self.io(regular).resolve(strict=True))[4:], str(regular))

    def test_component_runner_reads_bounded_deep_metadata_after_owned_tree_exit(self):
        app = QApplication.instance() or QApplication([])
        (self.root / "logs").mkdir()
        command = ComponentCommand(self.root)
        self.addCleanup(command.shutdown)
        finished = []
        failed = []
        command.finished.connect(lambda code, raw: finished.append((code, raw)))
        command.failed.connect(failed.append)
        command._launch(
            Path(sys.executable),
            ["-c", "print('safe metadata')"],
            {},
            timeout_seconds=10,
        )
        self.assertGreater(len(str(command.process.stdout_path)), 260)
        deadline = time.monotonic() + 15
        while command.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        self.assertFalse(command.busy)
        self.assertEqual(failed, [])
        self.assertEqual(finished, [(0, "safe metadata\r\n")])

    def test_deep_unicode_sqlite_readonly_uri_preserves_bytes_and_never_initializes_missing(
        self,
    ):
        seed = self.base / "seed.sqlite3"
        initialize(seed)
        payload = seed.read_bytes()
        prefix = "dữ liệu-"
        database = self.root / (prefix + "x" * (30 - len(prefix)))
        self.assertEqual(len(str(database)), 270)
        self.io(database).write_bytes(payload)
        db = open_readonly(database)
        try:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            validate(db)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM projects")
        finally:
            db.close()
        self.assertEqual(self.io(database).read_bytes(), payload)
        missing = self.root / ("missing-" + "x" * 25)
        with self.assertRaises(sqlite3.OperationalError):
            open_readonly(missing)
        self.assertFalse(self.io(missing).exists())

    def test_deep_present_history_is_read_and_malformed_history_is_not_missing(self):
        seed = self.base / "selection-seed"
        ensure_data_root(seed)
        initialize(seed / "creator_loop.sqlite3")
        model = self.base / "fake-model.fixture"
        payload = b"SYNTHETIC UNOWNED MODEL, NEVER EXECUTED"
        model.write_bytes(payload)
        component = ComponentSpec(
            "fake-model",
            "MODEL",
            model,
            "fake-version",
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "https://example.invalid/fake-model",
            "TEST ONLY",
            "https://example.invalid/fake-license",
            64 * 1024**2,
            True,
            False,
        )
        with AppDataLock(seed) as lock:
            saved = save_component_selection(seed, (component,), lock=lock)
        manifest = self.root / "manifests/storage-roots.json"
        self.io(manifest.parent).mkdir()
        original = (seed / "manifests/storage-roots.json").read_bytes()
        self.io(manifest).write_bytes(original)
        self.assertGreater(len(str(manifest)), 260)
        self.assertEqual(load_component_selection(self.root), saved)
        self.assertEqual(self.io(manifest).read_bytes(), original)
        self.assertEqual(model.read_bytes(), payload)
        self.io(manifest).write_bytes(b"{")
        with self.assertRaises(StorageRootError):
            load_component_selection(self.root)
        self.assertEqual(self.io(manifest).read_bytes(), b"{")

    def test_sqlite_path_budget_counts_utf16_units_for_non_bmp_names(self):
        # Parent remains below 248 Python characters, while the full filename
        # crosses MAX_PATH in Win32 UTF-16 code units.
        parent = self.base / ("e" * (230 - len(str(self.base)) - 1))
        parent.mkdir()

        def clean_parent():
            base = self.io(self.base).resolve(strict=True)
            target = self.io(parent).resolve(strict=True)
            self.assertTrue(target.is_relative_to(base))
            self.assertEqual(target.relative_to(base), parent.relative_to(self.base))
            shutil.rmtree(target)

        self.addCleanup(clean_parent)
        seed = self.base / "emoji-seed.sqlite3"
        initialize(seed)
        payload = seed.read_bytes()
        path = parent / ("\U0001f642" * 15)
        self.assertEqual(len(str(path)), 246)
        self.assertEqual(len(str(path).encode("utf-16-le")) // 2, 261)
        self.io(path).write_bytes(payload)
        db = open_readonly(path)
        try:
            validate(db)
        finally:
            db.close()
        self.assertEqual(self.io(path).read_bytes(), payload)
        self.io(path).unlink()
