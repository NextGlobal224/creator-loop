"""Windows relocation preserves source bytes, domain history and rollback."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import _connect_write, initialize, open_readonly
from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.storage_relocation import relocate_asset_files
from creator_loop.storage_roots import StorageRootError, register_storage_root
from creator_loop.text_intake import intake_text_original
from creator_loop.windows_owned_file import OwnedWindowsFile


@unittest.skipUnless(sys.platform == "win32", "Requires Windows owned file handles")
class RelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "Dữ liệu app"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        incoming = self.base / "Nguồn Huế.txt"
        incoming.write_bytes("Nguyên bản\r\nTiếng Việt".encode())
        self.imported = intake_text_original(incoming, root=self.root)
        self.source = resolve_storage_path(
            self.root, "ORIGINAL", self.imported.storage_key
        )
        self.target = self.base / "Kho Đà Nẵng"
        self.target.mkdir()
        self.registered = register_storage_root(self.root, self.target)
        derived = self.root / "storage" / "derived"
        derived.mkdir()
        self.derived = derived / "anchor.txt"
        self.derived.write_bytes(b"Derived anchor")
        with closing(_connect_write(self.db_path)) as db:
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at) VALUES('derived',?,'THUMBNAIL','storage/derived/anchor.txt',?,14,'text/plain',?,'now')",
                (
                    self.imported.asset_id,
                    hashlib.sha256(self.derived.read_bytes()).hexdigest(),
                    self.imported.file_id,
                ),
            )
            db.commit()
            EvidenceRepository(db).create_with_version(
                Evidence("e", self.imported.asset_id, "OTHER", "now", None),
                EvidenceVersion(
                    "v",
                    "e",
                    self.imported.asset_id,
                    1,
                    "derived",
                    "Snapshot",
                    "WHOLE_ASSET",
                    "{}",
                    "HUMAN",
                    None,
                    "editor",
                    "now",
                ),
            )

    def keys(self) -> dict[str, str]:
        with closing(open_readonly(self.db_path)) as db:
            return dict(db.execute("SELECT file_id,storage_key FROM asset_files"))

    def test_original_and_derived_move_without_changing_identity_or_history(
        self,
    ) -> None:
        before = self.source.read_bytes()
        with closing(open_readonly(self.db_path)) as db:
            history = db.execute("SELECT * FROM evidence_versions").fetchall()
            files = db.execute(
                "SELECT file_id,asset_id,sha256,parent_file_id FROM asset_files ORDER BY file_id"
            ).fetchall()
        moved = relocate_asset_files(
            self.root, [self.imported.file_id, "derived"], self.registered.root_id
        )
        self.assertEqual(self.keys(), moved)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.derived.read_bytes(), b"Derived anchor")
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM evidence_versions").fetchall(), history
            )
            self.assertEqual(
                db.execute(
                    "SELECT file_id,asset_id,sha256,parent_file_id FROM asset_files ORDER BY file_id"
                ).fetchall(),
                files,
            )
            reopened = reopen_evidence_version(db, "v", self.root)
            self.assertEqual(reopened.anchor_path.read_bytes(), b"Derived anchor")
            self.assertTrue(reopened.anchor_path.is_relative_to(self.target))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            resolve_storage_path(
                self.root, "ORIGINAL", moved[self.imported.file_id]
            ).read_bytes(),
            before,
        )

    def test_failed_second_file_rolls_back_keys_and_discards_only_new_copies(
        self,
    ) -> None:
        before = self.keys()
        with self.assertRaisesRegex(ValueError, "missing"):
            relocate_asset_files(
                self.root, [self.imported.file_id, "missing"], self.registered.root_id
            )
        self.assertEqual(self.keys(), before)
        self.assertEqual(list((self.target / "originals").iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_tampered_source_is_rejected_without_publishing_copy(self) -> None:
        before = self.keys()
        self.source.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "Source bytes"):
            relocate_asset_files(
                self.root, [self.imported.file_id], self.registered.root_id
            )
        self.assertEqual(self.keys(), before)
        self.assertEqual(self.source.read_bytes(), b"tampered")
        self.assertEqual(list((self.target / "originals").iterdir()), [])

    def test_target_collision_preserves_unowned_bytes_and_database(self) -> None:
        before = self.keys()
        folder = self.target / "originals"
        folder.mkdir()
        collision = folder / f"{'c' * 32}-{self.source.name}"
        collision.write_bytes(b"unowned")
        with patch("creator_loop.storage_relocation.uuid4") as identifier:
            identifier.return_value.hex = "c" * 32
            with self.assertRaises(FileExistsError):
                relocate_asset_files(
                    self.root, [self.imported.file_id], self.registered.root_id
                )
        self.assertEqual(collision.read_bytes(), b"unowned")
        self.assertEqual(self.keys(), before)

    def test_volume_loss_after_copy_rolls_back_before_publish(self) -> None:
        before = self.keys()
        with patch(
            "creator_loop.storage_relocation.resolve_registered_root",
            side_effect=[self.target.resolve(), StorageRootError("volume lost")],
        ):
            with self.assertRaises(StorageRootError):
                relocate_asset_files(
                    self.root, [self.imported.file_id], self.registered.root_id
                )
        self.assertEqual(self.keys(), before)
        self.assertEqual(list((self.target / "originals").iterdir()), [])

    def test_commit_error_after_success_retains_published_files(self) -> None:
        class AmbiguousCommit:
            def __init__(self, db: sqlite3.Connection) -> None:
                self.db = db

            def execute(self, *args: object) -> sqlite3.Cursor:
                return self.db.execute(*args)

            def commit(self) -> None:
                self.db.commit()
                raise sqlite3.OperationalError("commit response lost")

            def rollback(self) -> None:
                self.db.rollback()

            def close(self) -> None:
                self.db.close()

        with patch(
            "creator_loop.storage_relocation._connect_write",
            side_effect=lambda path: AmbiguousCommit(_connect_write(path)),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "response lost"):
                relocate_asset_files(
                    self.root, [self.imported.file_id], self.registered.root_id
                )
        key = self.keys()[self.imported.file_id]
        self.assertTrue(key.startswith("registered/"))
        self.assertEqual(
            resolve_storage_path(self.root, "ORIGINAL", key).read_bytes(),
            self.source.read_bytes(),
        )

    def test_write_lock_is_held_during_copy(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        create = OwnedWindowsFile.create_new

        def held(path: Path) -> OwnedWindowsFile:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release copy")
            return create(path)

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch(
                "creator_loop.storage_relocation.OwnedWindowsFile.create_new",
                side_effect=held,
            ),
        ):
            future = pool.submit(
                relocate_asset_files,
                self.root,
                [self.imported.file_id],
                self.registered.root_id,
            )
            try:
                self.assertTrue(entered.wait(5))
                with closing(sqlite3.connect(self.db_path, timeout=0.1)) as competing:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competing.execute("BEGIN IMMEDIATE")
            finally:
                release.set()
            self.assertEqual(set(future.result(timeout=10)), {self.imported.file_id})
