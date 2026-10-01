from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize
from creator_loop.media_intake import intake_image_original, intake_video_original
from creator_loop.originals import verify_original_file


class _NonWindowsOwnedFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = path.open("x+b")
        self.delete_on_close = False

    @classmethod
    def create_new(cls, path: Path) -> _NonWindowsOwnedFile:
        return cls(path)

    def discard(self) -> None:
        self.delete_on_close = True

    def close(self) -> None:
        self.stream.close()
        if self.delete_on_close:
            self.path.unlink()


class MediaOriginalIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "dữ liệu có dấu"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)

    def _check_import(
        self, source: Path, content: bytes, media_type: str, mime_type: str
    ) -> None:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        import_fn = (
            intake_video_original if media_type == "VIDEO" else intake_image_original
        )

        result = import_fn(source, root=self.root)

        stored = self.root.joinpath(*result.storage_key.split("/"))
        self.assertEqual(stored.read_bytes(), content)
        self.assertEqual(source.read_bytes(), content)
        with closing(sqlite3.connect(self.db_path)) as db:
            asset = db.execute(
                "SELECT media_type,display_name FROM assets WHERE asset_id=?",
                (result.asset_id,),
            ).fetchone()
            file = db.execute(
                """SELECT asset_id,role,storage_key,sha256,byte_size,mime_type,
                          parent_file_id,processing_run_id
                   FROM asset_files WHERE file_id=?""",
                (result.file_id,),
            ).fetchone()
            self.assertEqual(asset, (media_type, source.name))
            self.assertEqual(
                file,
                (
                    result.asset_id,
                    "ORIGINAL",
                    result.storage_key,
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    mime_type,
                    None,
                    None,
                ),
            )
            self.assertIsNone(verify_original_file(db, result.file_id, self.root))

    def test_mp4_original_preserves_bytes_and_metadata(self) -> None:
        self._check_import(
            self.base / "nguồn" / "Huế Kha.mp4",
            b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + b"sample video bytes",
            "VIDEO",
            "video/mp4",
        )

    def test_png_and_jpeg_originals_use_stored_signature(self) -> None:
        self._check_import(
            self.base / "nguồn" / "Ảnh ghi nhầm đuôi.jpg",
            b"\x89PNG\r\n\x1a\n" + b"sample png bytes",
            "IMAGE",
            "image/png",
        )
        self._check_import(
            self.base / "nguồn" / "Ảnh khác.jpeg",
            b"\xff\xd8\xff\xe0" + b"sample jpeg bytes",
            "IMAGE",
            "image/jpeg",
        )

    def test_unsupported_or_mismatched_media_rolls_back_file_and_rows(self) -> None:
        source = self.base / "nguồn" / "wrong.mp4"
        source.parent.mkdir()
        source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image bytes")

        with self.assertRaisesRegex(ValueError, "Unsupported video original"):
            intake_video_original(source, root=self.root)

        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM asset_files").fetchone()[0], 0
            )
        self.assertEqual(list((self.root / "storage" / "originals").iterdir()), [])
        self.assertEqual(source.read_bytes(), b"\x89PNG\r\n\x1a\n" + b"image bytes")
