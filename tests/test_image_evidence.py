"""Exercise image region Evidence against real decoded original pixels."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize

if os.name == "nt":
    try:
        from creator_loop.evidence_reopen import EvidenceReopenError
        from creator_loop.image_evidence import (
            create_image_evidence,
            reopen_image_region,
        )
        from creator_loop.image_thumbnail import create_image_thumbnail
        from creator_loop.media_intake import intake_image_original
        from PySide6.QtGui import QColor, QImage
    except ImportError:
        QImage = None
else:
    QImage = None


@unittest.skipUnless(QImage is not None, "requires Windows and PySide6")
class ImageEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu ảnh"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Ảnh mẫu.png"
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        for y in range(2, 4):
            for x in range(2, 4):
                image.setPixelColor(x, y, QColor("blue"))
        self.assertTrue(image.save(str(source)))
        self.imported = intake_image_original(source, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def _create(self, region: dict[str, float] | None = None):
        return create_image_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            region=region or {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
            content="Vùng màu xanh",
            actor="creator",
        )

    def test_region_reopens_exact_original_pixels(self) -> None:
        version = self._create()
        content, crop = reopen_image_region(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(version.anchor_file_id, self.imported.file_id)
        self.assertEqual(version.locator_type, "IMAGE_REGION")
        self.assertEqual(content, "Vùng màu xanh")
        self.assertEqual((crop.width(), crop.height()), (2, 2))
        self.assertEqual(crop.pixelColor(0, 0), QColor("blue"))

    def test_invalid_region_creates_no_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid image region"):
            self._create({"x": 0.8, "y": 0.5, "width": 0.5, "height": 0.5})
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )

    def test_region_without_decoded_pixels_cannot_be_saved(self) -> None:
        with self.assertRaisesRegex(ValueError, "resolves to no pixels"):
            self._create({"x": 0.5, "y": 0.5, "width": 1e-320, "height": 0.5})
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )

    def test_signature_only_image_is_not_accepted_as_observation(self) -> None:
        fake = Path(self.temp.name) / "Chưa giải mã.png"
        fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not a decodable image")
        imported = intake_image_original(fake, root=self.root)
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            create_image_evidence(
                self.db,
                file_id=imported.file_id,
                data_root=self.root,
                region={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                content="Không hợp lệ",
                actor="creator",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )

    def test_changed_original_is_rejected_on_reopen(self) -> None:
        version = self._create()
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_image_region(self.db, version.evidence_version_id, self.root)
        self.assertEqual(failure.exception.reason, "digest_mismatch")

    def test_region_can_anchor_verified_thumbnail_without_original_fallback(
        self,
    ) -> None:
        thumbnail = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=2
        )
        version = create_image_evidence(
            self.db,
            file_id=thumbnail.file_id,
            data_root=self.root,
            region={"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
            content="Vùng xanh trên thumbnail",
            actor="creator",
        )
        self.assertEqual(version.anchor_file_id, thumbnail.file_id)
        self.assertEqual(version.asset_id, self.imported.asset_id)
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        content, crop = reopen_image_region(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(content, "Vùng xanh trên thumbnail")
        self.assertEqual((crop.width(), crop.height()), (1, 1))
        self.assertEqual(crop.pixelColor(0, 0), QColor("blue"))

    def test_changed_thumbnail_is_rejected_without_opening_original(self) -> None:
        thumbnail = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=2
        )
        version = create_image_evidence(
            self.db,
            file_id=thumbnail.file_id,
            data_root=self.root,
            region={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            content="Thumbnail",
            actor="creator",
        )
        derived = self.root.joinpath(*thumbnail.storage_key.split("/"))
        raw = bytearray(derived.read_bytes())
        raw[-1] ^= 1
        derived.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_image_region(self.db, version.evidence_version_id, self.root)
        self.assertEqual(failure.exception.reason, "digest_mismatch")

    def test_thumbnail_from_non_successful_run_cannot_anchor_evidence(self) -> None:
        thumbnail = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=2
        )
        self.db.execute(
            "UPDATE processing_runs SET status='FAILED',error_code='INVALID' WHERE run_id=?",
            (thumbnail.run_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "lineage is invalid"):
            create_image_evidence(
                self.db,
                file_id=thumbnail.file_id,
                data_root=self.root,
                region={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                content="Không hợp lệ",
                actor="creator",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )
