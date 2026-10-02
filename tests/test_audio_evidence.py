"""Check an MP4 audio locator against decoded synthetic samples."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import initialize

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.audio_evidence import (
            create_audio_evidence,
            reopen_audio_evidence,
        )
        from creator_loop.evidence_reopen import EvidenceReopenError
        from creator_loop.media_intake import intake_video_original
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "requires Windows and Qt Multimedia")
class AudioEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu audio"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        fixture = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
        self.imported = intake_video_original(fixture, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def test_audio_range_reopens_same_verified_original(self) -> None:
        version = create_audio_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start_ms=200,
            end_ms=600,
            content="Âm kiểm thử 440 Hz",
            actor="creator",
            evidence_type="OTHER",
        )
        content, decoded, start_ms, end_ms = reopen_audio_evidence(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(content, "Âm kiểm thử 440 Hz")
        self.assertEqual((start_ms, end_ms), (200, 600))
        self.assertEqual(version.anchor_file_id, self.imported.file_id)
        self.assertGreaterEqual(decoded.duration_ms, 600)
        self.assertLess(decoded.buffer_start_ms, 600)
        self.assertGreater(decoded.buffer_end_ms, 200)
        self.assertEqual(decoded.anchor_path, self.stored.resolve())
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_type FROM evidences WHERE evidence_id=?",
                (version.evidence_id,),
            ).fetchone()[0],
            "OTHER",
        )

    def test_range_outside_duration_creates_no_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds decoded duration"):
            create_audio_evidence(
                self.db,
                file_id=self.imported.file_id,
                data_root=self.root,
                start_ms=200,
                end_ms=5000,
                content="Ngoài audio",
                actor="creator",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )

    def test_audio_metadata_and_evidence_respect_caller_transaction(self) -> None:
        self.db.execute(
            """CREATE TEMP TRIGGER reject_audio_version
               BEFORE INSERT ON evidence_versions
               BEGIN SELECT RAISE(ABORT,'version blocked'); END"""
        )
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller-project", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "version blocked"):
            create_audio_evidence(
                self.db,
                file_id=self.imported.file_id,
                data_root=self.root,
                start_ms=200,
                end_ms=600,
                content="Âm kiểm thử",
                actor="creator",
            )
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT duration_ms FROM asset_files WHERE file_id=?",
                (self.imported.file_id,),
            ).fetchone()[0],
            None,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM projects WHERE project_id='caller-project'"
            ).fetchone()[0],
            1,
        )
        self.db.execute("DROP TRIGGER reject_audio_version")
        version = create_audio_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start_ms=200,
            end_ms=600,
            content="Âm kiểm thử",
            actor="creator",
        )
        self.assertTrue(self.db.in_transaction)
        other = sqlite3.connect(self.root / "creator_loop.sqlite3")
        self.addCleanup(other.close)
        self.assertEqual(
            other.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )
        self.assertEqual(
            other.execute(
                "SELECT duration_ms FROM asset_files WHERE file_id=?",
                (self.imported.file_id,),
            ).fetchone()[0],
            None,
        )
        self.db.commit()
        self.assertEqual(
            other.execute(
                "SELECT anchor_file_id FROM evidence_versions WHERE evidence_version_id=?",
                (version.evidence_version_id,),
            ).fetchone()[0],
            self.imported.file_id,
        )

    def test_video_without_audio_track_is_rejected(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "video-red-blue.mp4"
        silent = intake_video_original(fixture, root=self.root)
        with self.assertRaisesRegex(ValueError, "no playable audio track"):
            create_audio_evidence(
                self.db,
                file_id=silent.file_id,
                data_root=self.root,
                start_ms=200,
                end_ms=600,
                content="Không tồn tại",
                actor="creator",
            )

    def test_changed_original_cannot_be_reopened(self) -> None:
        version = create_audio_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start_ms=200,
            end_ms=600,
            content="Âm kiểm thử",
            actor="creator",
            evidence_type="OTHER",
        )
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_audio_evidence(self.db, version.evidence_version_id, self.root)
        self.assertEqual(failure.exception.reason, "digest_mismatch")
