"""Check MP4 time Evidence with a small locally generated H.264 fixture."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.evidence_reopen import EvidenceReopenError
        from creator_loop.media_intake import intake_video_original
        from creator_loop.video_evidence import (
            create_video_evidence,
            reopen_video_evidence,
        )
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "requires Windows and Qt Multimedia")
class VideoEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu video"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        fixture = Path(__file__).parent / "fixtures" / "video-red-blue.mp4"
        self.imported = intake_video_original(fixture, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def test_decoded_frame_and_range_reopen_from_same_original(self) -> None:
        version = create_video_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start_ms=600,
            end_ms=800,
            content="Khung hình xanh",
            actor="creator",
        )
        content, decoded, start_ms, end_ms = reopen_video_evidence(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(content, "Khung hình xanh")
        self.assertEqual((start_ms, end_ms), (600, 800))
        self.assertEqual(version.anchor_file_id, self.imported.file_id)
        self.assertGreaterEqual(decoded.duration_ms, 800)
        self.assertGreaterEqual(decoded.frame_time_ms, 600)
        pixel = decoded.image.pixelColor(0, 0)
        self.assertGreater(pixel.blue(), pixel.red())
        self.assertGreater(pixel.blue(), pixel.green())

    def test_end_after_decoded_duration_creates_no_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds duration"):
            create_video_evidence(
                self.db,
                file_id=self.imported.file_id,
                data_root=self.root,
                start_ms=600,
                end_ms=5000,
                content="Ngoài video",
                actor="creator",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )

    def test_changed_original_cannot_be_reopened(self) -> None:
        version = create_video_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start_ms=600,
            end_ms=800,
            content="Khung hình xanh",
            actor="creator",
        )
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_video_evidence(self.db, version.evidence_version_id, self.root)
        self.assertEqual(failure.exception.reason, "digest_mismatch")
