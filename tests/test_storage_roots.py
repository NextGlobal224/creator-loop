"""Registered volume resolution never falls back to another media store."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import (
    SCHEMA_VERSION,
    _connect_write,
    initialize,
    open_readonly,
)
from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import EvidenceReopenError, reopen_evidence_version
from creator_loop.originals import verify_original_file
from creator_loop.storage_paths import StoragePathError, resolve_storage_path
from creator_loop.storage_roots import (
    StorageRootError,
    default_storage_root_id,
    list_storage_roots,
    register_storage_root,
    resolve_registered_root,
    set_default_storage_root,
)


class RegisteredStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "User data Huế"
        self.root.mkdir()
        initialize(self.root / "creator_loop.sqlite3")
        self.media = self.base / "Kho media Đà Nẵng"
        self.media.mkdir()
        self.manifest = self.root / "manifests" / "storage-roots.json"

    def test_default_choice_preserves_roots_and_legacy_manifest(self) -> None:
        self.assertIsNone(default_storage_root_id(self.root))
        registered = register_storage_root(self.root, self.media)
        before = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("default_storage_root_id", before)
        set_default_storage_root(self.root, registered.root_id)
        self.assertEqual(default_storage_root_id(self.root), registered.root_id)
        set_default_storage_root(self.root, None)
        self.assertIsNone(default_storage_root_id(self.root))
        after = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(before["data_root_id"], after["data_root_id"])
        self.assertEqual(list_storage_roots(self.root), [registered])

    def test_default_choice_unknown_or_offline_root_does_not_overwrite_manifest(
        self,
    ) -> None:
        registered = register_storage_root(self.root, self.media)
        before = self.manifest.read_bytes()
        for root_id in ("0" * 32, "../invalid"):
            with self.assertRaises(StorageRootError):
                set_default_storage_root(self.root, root_id)
        with patch(
            "creator_loop.storage_roots.volume_identity", return_value="wrong-volume"
        ):
            with self.assertRaises(StorageRootError):
                set_default_storage_root(self.root, registered.root_id)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_malformed_default_is_not_treated_as_local_storage(self) -> None:
        register_storage_root(self.root, self.media)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["default_storage_root_id"] = "missing"
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(StorageRootError):
            default_storage_root_id(self.root)

    def test_registration_persists_identity_and_does_not_touch_domain_or_media(
        self,
    ) -> None:
        source = self.media / "Tư liệu riêng.txt"
        source.write_bytes(b"existing unrelated bytes")
        before = source.read_bytes()
        registered = register_storage_root(self.root, self.media)
        self.assertEqual(
            resolve_registered_root(self.root, registered.root_id), self.media.resolve()
        )
        self.assertEqual(list_storage_roots(self.root), [registered])
        self.assertEqual(register_storage_root(self.root, self.media), registered)
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(source.read_bytes(), before)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            for table in ("assets", "asset_files", "evidence_versions"):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )

    def test_registered_original_and_derived_evidence_reopen_exact_anchor(self) -> None:
        registered = register_storage_root(self.root, self.media)
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            db.execute(
                "INSERT INTO assets(asset_id,media_type,display_name,created_at) VALUES('a','TEXT','Tư liệu','now')"
            )
            db.commit()
            for role, folder in (("ORIGINAL", "originals"), ("THUMBNAIL", "derived")):
                directory = self.media / folder
                directory.mkdir()
                content = f"Exact source {role}".encode()
                file = directory / "Tư liệu.txt"
                file.write_bytes(content)
                key = f"registered/{registered.root_id}/{folder}/{file.name}"
                db.execute(
                    "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at,parent_file_id) VALUES(?,'a',?,?,?,?,?,'now',?)",
                    (
                        role,
                        role,
                        key,
                        hashlib.sha256(content).hexdigest(),
                        len(content),
                        "text/plain",
                        None if role == "ORIGINAL" else "ORIGINAL",
                    ),
                )
                db.commit()
                version = EvidenceVersion(
                    role,
                    role,
                    "a",
                    1,
                    role,
                    "Observation",
                    "WHOLE_ASSET",
                    "{}",
                    "HUMAN",
                    None,
                    "editor",
                    "now",
                )
                EvidenceRepository(db).create_with_version(
                    Evidence(role, "a", "OTHER", "now", None), version
                )
                reopened = reopen_evidence_version(db, role, self.root)
                self.assertEqual(reopened.anchor_path, file.resolve())
                self.assertEqual(reopened.anchor_file_id, role)
                if role == "ORIGINAL":
                    verify_original_file(db, role, self.root)
            directory = self.media / "originals"
            (directory / "Tư liệu.txt").write_bytes(b"Changed source")
            with self.assertRaises(EvidenceReopenError):
                reopen_evidence_version(db, "ORIGINAL", self.root)
            self.assertEqual(
                reopen_evidence_version(db, "THUMBNAIL", self.root).anchor_file_id,
                "THUMBNAIL",
            )

    def test_unavailable_or_changed_volume_keeps_registration_and_never_falls_back(
        self,
    ) -> None:
        registered = register_storage_root(self.root, self.media)
        key = f"registered/{registered.root_id}/originals/file.txt"
        fallback = self.root / "storage" / "originals" / "file.txt"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"wrong source")
        with patch(
            "creator_loop.storage_roots.volume_identity", return_value="another-volume"
        ):
            with self.assertRaisesRegex(StorageRootError, "volume has changed"):
                resolve_registered_root(self.root, registered.root_id)
            with self.assertRaises(StoragePathError) as failure:
                resolve_storage_path(self.root, "ORIGINAL", key)
            self.assertEqual(failure.exception.reason, "unavailable_file")
        shutil.rmtree(self.media)
        self.assertEqual(list_storage_roots(self.root), [registered])
        with self.assertRaisesRegex(StorageRootError, "unavailable"):
            resolve_registered_root(self.root, registered.root_id)
        self.assertEqual(fallback.read_bytes(), b"wrong source")

    def test_invalid_roots_and_key_escape_are_rejected(self) -> None:
        with self.assertRaises(OSError):
            register_storage_root(self.root, self.base / "missing")
        for bad in (self.root, self.base):
            with self.assertRaises(StorageRootError):
                register_storage_root(self.root, bad)
        registered = register_storage_root(self.root, self.media)
        nested = self.media / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(StorageRootError, "overlap"):
            register_storage_root(self.root, nested)
        for key in (
            f"registered/{registered.root_id}/originals/../outside",
            f"registered/{registered.root_id}/derived/file",
            f"registered/{registered.root_id}/originals/C:file",
            "registered/no-root/originals/file",
        ):
            with self.subTest(key=key), self.assertRaises(StoragePathError):
                resolve_storage_path(self.root, "ORIGINAL", key)

    def test_oversized_manifest_is_rejected_without_replacing_it(self) -> None:
        register_storage_root(self.root, self.media)
        raw = b" " * (1024 * 1024 + 1)
        self.manifest.write_bytes(raw)
        with self.assertRaisesRegex(StorageRootError, "budget"):
            set_default_storage_root(self.root, None)
        self.assertEqual(self.manifest.stat().st_size, len(raw))

    def test_manifest_corruption_is_not_silently_replaced(self) -> None:
        register_storage_root(self.root, self.media)
        self.manifest.write_text('{"broken":true}', encoding="utf-8")
        before = self.manifest.read_bytes()
        with self.assertRaises(StorageRootError):
            register_storage_root(self.root, self.media)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_failed_atomic_replace_keeps_previous_manifest(self) -> None:
        first = register_storage_root(self.root, self.media)
        before = self.manifest.read_bytes()
        second = self.base / "Second root"
        second.mkdir()
        with patch(
            "creator_loop.storage_roots.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                register_storage_root(self.root, second)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(list_storage_roots(self.root), [first])
        self.assertEqual(list(self.manifest.parent.glob("*.tmp")), [])

    def test_concurrent_registration_preserves_both_roots(self) -> None:
        second = self.base / "Second root"
        second.mkdir()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(register_storage_root, self.root, path)
                for path in (self.media, second)
            ]
            expected = [future.result() for future in futures]
        self.assertEqual(set(list_storage_roots(self.root)), set(expected))

    def test_temporary_name_collision_does_not_delete_unowned_file(self) -> None:
        first = register_storage_root(self.root, self.media)
        before = self.manifest.read_bytes()
        second = self.base / "Second root"
        second.mkdir()
        collision = self.manifest.with_name(f"{self.manifest.name}.{'c' * 32}.tmp")
        collision.write_bytes(b"unowned temporary file")
        with patch("creator_loop.storage_roots.uuid4") as identifier:
            identifier.return_value.hex = "c" * 32
            with self.assertRaises(FileExistsError):
                register_storage_root(self.root, second)
        self.assertEqual(collision.read_bytes(), b"unowned temporary file")
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(list_storage_roots(self.root), [first])
