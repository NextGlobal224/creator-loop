"""Check a real derived image and its task run without changing the original."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from creator_loop.database import initialize

if os.name == "nt":
    try:
        from creator_loop.image_thumbnail import create_image_thumbnail
        from creator_loop.media_intake import intake_image_original
        from PySide6.QtGui import QColor, QImage
    except ImportError:
        QImage = None
else:
    QImage = None


@unittest.skipUnless(QImage is not None, "requires Windows and PySide6")
class ImageThumbnailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu thumbnail"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        source = Path(self.temp.name) / "Original.png"
        image = QImage(10, 6, QImage.Format.Format_RGB32)
        image.fill(QColor("magenta"))
        self.assertTrue(image.save(str(source)))
        self.imported = intake_image_original(source, root=self.root)
        self.original = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.original_bytes = self.original.read_bytes()

    def test_thumbnail_file_and_run_have_same_asset_lineage(self) -> None:
        result = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=5
        )
        derived = self.root.joinpath(*result.storage_key.split("/"))
        stored = derived.read_bytes()
        with closing(sqlite3.connect(self.db_path)) as db:
            file_row = db.execute(
                """SELECT asset_id,role,parent_file_id,processing_run_id,
                          sha256,byte_size,width_px,height_px
                   FROM asset_files WHERE file_id=?""",
                (result.file_id,),
            ).fetchone()
            run_row = db.execute(
                """SELECT asset_id,input_file_id,task_type,status,tool_name,
                          model_name,started_at,finished_at
                   FROM processing_runs WHERE run_id=?""",
                (result.run_id,),
            ).fetchone()
        self.assertEqual(
            file_row[:4],
            (self.imported.asset_id, "THUMBNAIL", self.imported.file_id, result.run_id),
        )
        self.assertEqual(
            file_row[4:6], (hashlib.sha256(stored).hexdigest(), len(stored))
        )
        self.assertEqual(file_row[6:], (5, 3))
        self.assertEqual(
            run_row[:4],
            (
                self.imported.asset_id,
                self.imported.file_id,
                "IMAGE_THUMBNAIL",
                "SUCCEEDED",
            ),
        )
        self.assertIn("Qt image scaler", run_row[4])
        self.assertIsNone(run_row[5])
        self.assertTrue(run_row[6] and run_row[7])
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(
            (QImage(str(derived)).width(), QImage(str(derived)).height()), (5, 3)
        )

    def test_changed_original_records_failed_run_without_derived_file(self) -> None:
        changed = bytearray(self.original_bytes)
        changed[-1] ^= 1
        self.original.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            create_image_thumbnail(self.imported.file_id, data_root=self.root)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status,error_code,finished_at FROM processing_runs"
                ).fetchone()[:2],
                ("FAILED", "THUMBNAIL_FAILED"),
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM asset_files WHERE role='THUMBNAIL'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.original.read_bytes(), changed)

    def test_existing_destination_is_not_overwritten(self) -> None:
        derived = self.root / "storage" / "derived"
        derived.mkdir()
        occupied = derived / f"{UUID(int=2).hex}.png"
        occupied.write_bytes(b"not owned by this run")
        with patch(
            "creator_loop.image_thumbnail.uuid4",
            side_effect=(UUID(int=1), UUID(int=2)),
        ):
            with self.assertRaises(FileExistsError):
                create_image_thumbnail(self.imported.file_id, data_root=self.root)
        self.assertEqual(occupied.read_bytes(), b"not owned by this run")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT status FROM processing_runs").fetchone()[0], "FAILED"
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM asset_files WHERE role='THUMBNAIL'"
                ).fetchone()[0],
                0,
            )
