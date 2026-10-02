"""Real Windows media follows the selected root without silent fallback."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.database import initialize, open_readonly
    from creator_loop.image_thumbnail import create_image_thumbnail
    from creator_loop.media_intake import intake_image_original, intake_video_original
    from creator_loop.originals import verify_original_file
    from creator_loop.storage_paths import StoragePathError, resolve_storage_path
    from creator_loop.storage_roots import (
        register_storage_root,
        set_default_storage_root,
    )
    from creator_loop.text_intake import intake_text_original
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Requires Windows owned files and Qt")
class DefaultIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "Dữ liệu Huế"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.media = self.base / "Kho Đà Nẵng"
        self.media.mkdir()
        self.registered = register_storage_root(self.root, self.media)
        set_default_storage_root(self.root, self.registered.root_id)
        self.text = self.base / "Nguyên bản.txt"
        self.text.write_bytes("Tiếng Việt\r\nChính xác".encode())

    def test_three_original_types_and_thumbnail_follow_default_root(self) -> None:
        image_path = self.base / "Ảnh.png"
        image = QImage(10, 6, QImage.Format.Format_RGB32)
        image.fill(QColor("magenta"))
        self.assertTrue(image.save(str(image_path)))
        video = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
        imported = []
        for source, intake in (
            (self.text, intake_text_original),
            (image_path, intake_image_original),
            (video, intake_video_original),
        ):
            result = intake(source, root=self.root)
            imported.append(result)
            stored = resolve_storage_path(self.root, "ORIGINAL", result.storage_key)
            self.assertTrue(stored.is_relative_to(self.media.resolve()))
            self.assertEqual(stored.read_bytes(), source.read_bytes())
            with closing(open_readonly(self.db_path)) as db:
                verify_original_file(db, result.file_id, self.root)
        thumbnail = create_image_thumbnail(
            imported[1].file_id, data_root=self.root, max_edge=5
        )
        stored = resolve_storage_path(self.root, "THUMBNAIL", thumbnail.storage_key)
        self.assertTrue(stored.is_relative_to(self.media.resolve()))
        self.assertEqual(
            (QImage(str(stored)).width(), QImage(str(stored)).height()), (5, 3)
        )
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT parent_file_id FROM asset_files WHERE file_id=?",
                    (thumbnail.file_id,),
                ).fetchone()[0],
                imported[1].file_id,
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertFalse((self.root / "storage").exists())
        set_default_storage_root(self.root, None)
        local = intake_text_original(self.text, root=self.root)
        self.assertTrue(local.storage_key.startswith("storage/originals/"))
        self.assertEqual(
            resolve_storage_path(
                self.root, "ORIGINAL", imported[0].storage_key
            ).read_bytes(),
            self.text.read_bytes(),
        )

    def test_offline_default_fails_without_creating_fallback_file_or_asset(
        self,
    ) -> None:
        with patch(
            "creator_loop.storage_roots.volume_identity", return_value="wrong-volume"
        ):
            with self.assertRaises(StoragePathError):
                intake_text_original(self.text, root=self.root)
        self.assertFalse((self.root / "storage").exists())
        self.assertEqual(list(self.media.iterdir()), [])
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 0)

    def test_volume_failure_before_commit_discards_only_owned_copy(self) -> None:
        existing = self.media / "unowned.txt"
        existing.write_bytes(b"keep")
        with patch(
            "creator_loop.text_intake.resolve_storage_path",
            side_effect=StoragePathError("unavailable_file"),
        ):
            with self.assertRaises(StoragePathError):
                intake_text_original(self.text, root=self.root)
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertEqual(list((self.media / "originals").iterdir()), [])
        with closing(open_readonly(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 0)
