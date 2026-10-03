"""Package sealing against decoded PNG originals and held Windows handles."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.database import initialize
from creator_loop.drafts import create_draft
from creator_loop.media_intake import intake_image_original
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.projects import create_project
from creator_loop.source_association import SourceDetails, create_source_for_asset
from creator_loop.text_intake import intake_text_original
from test_media_intake import _NonWindowsOwnedFile

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None


@unittest.skipUnless(QImage is not None, "decoded PNG tests require PySide6")
class PackageMediaTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtGui import QImage

        if os.name == "nt":
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication

            self.app = QApplication.instance() or QApplication([])
        else:
            owned = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned.start()
            self.addCleanup(owned.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Dữ liệu Package có dấu"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        source = Path(temp.name) / "Ảnh.png"
        image = QImage(10, 6, QImage.Format.Format_RGB32)
        image.fill(0xFFFF00FF)
        self.assertTrue(image.save(str(source)))
        self.media = intake_image_original(source, root=self.root)
        self.stored = self.root.joinpath(*self.media.storage_key.split("/"))
        self.digest = hashlib.sha256(self.stored.read_bytes()).hexdigest()
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.project = create_project(self.db, title="Package")
        self.draft = create_draft(
            self.db,
            project_id=self.project.project_id,
            body_text="Bản nháp",
            format="POST",
            actor="editor",
        )
        self.repo = PublicationRepository(self.path)
        self.source = None

    def _rights(self, status="OWNED"):
        self.source = create_source_for_asset(
            self.db,
            asset_id=self.media.asset_id,
            details=SourceDetails(platform="LOCAL", rights_status=status),
            relationship_type="ORIGIN",
        )

    def _items(self):
        return [
            PackageItem("CAPTION", 0, text_payload="Café ở Huế"),
            PackageItem(
                "MEDIA", 0, file_id=self.media.file_id, content_digest=self.digest
            ),
        ]

    def _create(self, *, items=None, root=True):
        package_id = uuid4().hex
        fingerprint = self.repo.create_package(
            package_id=package_id,
            project_id=self.project.project_id,
            draft_version_id=self.draft.draft_version_id,
            platform="FB",
            format="POST",
            created_at="2026-10-03T02:40:00Z",
            items=self._items() if items is None else items,
            data_root=self.root if root else None,
        )
        return package_id, fingerprint

    def _count(self):
        return self.db.execute("SELECT count(*) FROM publication_packages").fetchone()[
            0
        ]

    def test_real_media_rights_seal_and_canonical_fingerprint(self):
        self._rights()
        first, fingerprint = self._create()
        second, reordered = self._create(items=list(reversed(self._items())))
        self.assertNotEqual(first, second)
        self.assertEqual(fingerprint, reordered)
        changed = self._items()
        changed[0] = PackageItem("CAPTION", 0, text_payload="Café ở Huế!")
        self.assertNotEqual(fingerprint, self._create(items=changed)[1])
        self.assertEqual(
            self.db.execute(
                "SELECT content_digest FROM publication_package_items WHERE package_id=? AND item_type='MEDIA'",
                (first,),
            ).fetchone()[0],
            self.digest,
        )
        self.assertIsNotNone(
            self.db.execute(
                "SELECT sealed_at FROM publication_packages WHERE package_id=?",
                (first,),
            ).fetchone()[0]
        )

    def test_unknown_reference_restricted_and_conflicting_rights_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "reuse rights unresolved.*UNKNOWN"):
            self._create()
        self._rights()
        other = create_source_for_asset(
            self.db,
            asset_id=self.media.asset_id,
            details=SourceDetails(platform="OTHER"),
            relationship_type="REFERENCE",
        )
        for status in ("UNKNOWN", "REFERENCE_ONLY", "RESTRICTED"):
            self.db.execute(
                "UPDATE sources SET rights_status=? WHERE source_id=?", (status, other)
            )
            self.db.commit()
            with self.assertRaisesRegex(ValueError, "reuse rights unresolved"):
                self._create()
            self.assertEqual(self._count(), 0)
        self.db.execute(
            "UPDATE sources SET rights_status='LICENSED' WHERE source_id=?", (other,)
        )
        self.db.commit()
        self._create()

    def test_tampered_missing_bytes_rejected_and_old_snapshot_preserved(self):
        self._rights()
        package, _fingerprint = self._create()
        original = self.stored.read_bytes()
        self.stored.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "physical bytes"):
            self._create()
        self.stored.unlink()
        with self.assertRaisesRegex(ValueError, "file unavailable"):
            self._create()
        self.assertEqual(self._count(), 1)
        self.assertEqual(
            self.db.execute("SELECT package_id FROM publication_packages").fetchone()[
                0
            ],
            package,
        )

    def test_changed_real_media_creates_new_digest_and_package(self):
        from PySide6.QtGui import QImage

        self._rights()
        old = self._create()
        image = QImage(10, 6, QImage.Format.Format_RGB32)
        image.fill(0xFF0000FF)
        source = self.root.parent / "Ảnh mới.png"
        self.assertTrue(image.save(str(source)))
        imported = intake_image_original(source, root=self.root)
        create_source_for_asset(
            self.db,
            asset_id=imported.asset_id,
            details=SourceDetails(platform="LOCAL", rights_status="OWNED"),
            relationship_type="ORIGIN",
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertNotEqual(digest, self.digest)
        items = self._items()
        items[1] = PackageItem(
            "MEDIA", 0, file_id=imported.file_id, content_digest=digest
        )
        self.assertNotEqual(old[1], self._create(items=items)[1])

    def test_missing_root_digest_or_invalid_items_leave_no_package(self):
        self._rights()
        with self.assertRaisesRegex(ValueError, "recorded data root"):
            self._create(root=False)
        for items in (
            [
                PackageItem(
                    "MEDIA", 0, file_id=self.media.file_id, content_digest="0" * 64
                )
            ],
            [PackageItem("CAPTION", 0, text_payload="x", content_digest="bad")],
            [PackageItem("CAPTION", True, text_payload="x")],
            [PackageItem("CAPTION", 0, text_payload="x")] * 2,
        ):
            with self.assertRaises(ValueError):
                self._create(items=items)
        self.assertEqual(self._count(), 0)

    def test_failed_item_insert_rolls_back_and_releases_media_handle(self):
        self._rights()
        with self.assertRaises(sqlite3.IntegrityError):
            self._create(
                items=self._items() + [PackageItem("INVALID", 0, text_payload="x")]
            )
        self.assertEqual(self._count(), 0)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM publication_package_items"
            ).fetchone()[0],
            0,
        )
        with self.stored.open("r+b") as stream:
            self.assertTrue(stream.read())

    def test_archived_project_or_draft_cannot_create_package(self):
        self._rights()
        self.db.execute("UPDATE projects SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "active live Project"):
            self._create()
        self.db.execute("UPDATE projects SET status='ACTIVE'")
        self.db.execute("UPDATE drafts SET status='ARCHIVED'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "active live Project"):
            self._create()
        self.assertEqual(self._count(), 0)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_windows_handle_blocks_write_and_delete_through_seal_commit(self):
        self._rights()
        outcomes = []

        def probe():
            for operation in (lambda: self.stored.open("r+b"), self.stored.unlink):
                try:
                    result = operation()
                except PermissionError:
                    outcomes.append("blocked")
                else:
                    if result is not None:
                        result.close()
                    outcomes.append("allowed")
            return 1

        from creator_loop.database import _connect_write

        def connection(path):
            db = _connect_write(path)
            db.create_function("probe_media", 0, probe)
            db.execute(
                "CREATE TEMP TRIGGER probe_seal BEFORE UPDATE OF sealed_at ON publication_packages BEGIN SELECT probe_media(); END"
            )
            return db

        with patch("creator_loop.packages._connect_write", side_effect=connection):
            self._create()
        self.assertEqual(outcomes, ["blocked", "blocked"])
        with self.stored.open("r+b") as stream:
            self.assertTrue(stream.read())


class PortablePackageMediaTests(unittest.TestCase):
    def test_real_file_rights_and_byte_gate_without_qt(self):
        """Portable file-payload test; does not certify image/video codecs."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Publication Unicode"
            root.mkdir()
            path = root / "creator_loop.sqlite3"
            initialize(path)
            source = Path(temp) / "source.txt"
            source.write_text("Café ở Huế", encoding="utf-8")
            if os.name == "nt":
                imported = intake_text_original(source, root=root)
            else:
                with patch(
                    "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
                ):
                    imported = intake_text_original(source, root=root)
            stored = root.joinpath(*imported.storage_key.split("/"))
            digest = hashlib.sha256(stored.read_bytes()).hexdigest()
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                project = create_project(db, title="Portable")
                draft = create_draft(
                    db,
                    project_id=project.project_id,
                    body_text="Café",
                    format="POST",
                    actor="editor",
                )
                repo = PublicationRepository(path)
                payload = dict(
                    project_id=project.project_id,
                    draft_version_id=draft.draft_version_id,
                    platform="MANUAL",
                    format="POST",
                    created_at="now",
                    data_root=root,
                    items=[
                        PackageItem(
                            "OTHER", 0, file_id=imported.file_id, content_digest=digest
                        )
                    ],
                )
                with self.assertRaisesRegex(ValueError, "reuse rights unresolved"):
                    repo.create_package(package_id="unknown", **payload)
                create_source_for_asset(
                    db,
                    asset_id=imported.asset_id,
                    details=SourceDetails(platform="LOCAL", rights_status="OWNED"),
                    relationship_type="ORIGIN",
                )
                repo.create_package(package_id="accepted", **payload)
                original = stored.read_bytes()
                stored.write_bytes(b"X" * len(original))
                with self.assertRaisesRegex(ValueError, "physical bytes"):
                    repo.create_package(package_id="tampered", **payload)
                self.assertEqual(
                    db.execute(
                        "SELECT package_id FROM publication_packages"
                    ).fetchall(),
                    [("accepted",)],
                )
