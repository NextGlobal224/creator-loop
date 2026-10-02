"""Check image Evidence correction against decoded original and thumbnail pixels."""

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
    try:
        from creator_loop.claims import EvidenceLink, claim_support_review, create_claim
        from creator_loop.evidence_correction import claim_versions_needing_review
        from creator_loop.evidence_reopen import EvidenceReopenError
        from creator_loop.evidence_review import (
            current_evidence_review,
            record_evidence_review,
        )
        from creator_loop.image_evidence import (
            create_image_evidence,
            reopen_image_region,
        )
        from creator_loop.image_evidence_correction import correct_image_evidence
        from creator_loop.image_thumbnail import create_image_thumbnail
        from creator_loop.media_intake import intake_image_original
        from PySide6.QtGui import QColor, QImage
    except ImportError:
        QImage = None
else:
    QImage = None


@unittest.skipUnless(QImage is not None, "requires Windows and PySide6")
class ImageEvidenceCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu sửa ảnh"
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

    def _create(self, file_id: str | None = None):
        return create_image_evidence(
            self.db,
            file_id=file_id or self.imported.file_id,
            data_root=self.root,
            region={"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
            content="Vùng xanh",
            actor="creator",
        )

    def _correct(self, version_id: str, **changes):
        arguments = {
            "evidence_version_id": version_id,
            "data_root": self.root,
            "region": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
            "content": "Vùng đỏ",
            "actor": "editor",
            "reason": "Chọn lại vùng ảnh",
        }
        arguments.update(changes)
        return correct_image_evidence(self.db, **arguments)

    def test_original_history_review_and_claim_projection(self) -> None:
        first = self._create()
        claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Ảnh có vùng xanh",
            actor="editor",
            links=[EvidenceLink(first.evidence_version_id, "SUPPORTS")],
        )
        record_evidence_review(
            self.db,
            evidence_version_id=first.evidence_version_id,
            data_root=self.root,
            action="ACCEPT",
            actor="reviewer",
        )
        corrected = self._correct(first.evidence_version_id)

        versions = self.db.execute(
            """SELECT evidence_version_id,version_no,anchor_file_id,content,
                      producer_type,processing_run_id FROM evidence_versions
               WHERE evidence_id=? ORDER BY version_no""",
            (first.evidence_id,),
        ).fetchall()
        self.assertEqual(
            versions,
            [
                (
                    first.evidence_version_id,
                    1,
                    self.imported.file_id,
                    "Vùng xanh",
                    "HUMAN",
                    None,
                ),
                (
                    corrected.new_version_id,
                    2,
                    self.imported.file_id,
                    "Vùng đỏ",
                    "HUMAN",
                    None,
                ),
            ],
        )
        old_content, old_crop = reopen_image_region(
            self.db, first.evidence_version_id, self.root
        )
        new_content, new_crop = reopen_image_region(
            self.db, corrected.new_version_id, self.root
        )
        self.assertEqual((old_content, new_content), ("Vùng xanh", "Vùng đỏ"))
        self.assertEqual(old_crop.pixelColor(0, 0), QColor("blue"))
        self.assertEqual(new_crop.pixelColor(0, 0), QColor("red"))
        self.assertEqual(
            self.db.execute(
                "SELECT action,actor_id,reason FROM review_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone(),
            ("CORRECT", "editor", "Chọn lại vùng ảnh"),
        )
        self.assertEqual(
            current_evidence_review(self.db, corrected.new_version_id), "PENDING"
        )
        projection = claim_support_review(self.db, claim.claim_version_id)
        self.assertEqual(
            (
                projection.support_links,
                projection.accepted_current_supports,
                projection.stale_links,
            ),
            (1, 0, 1),
        )
        self.assertEqual(
            claim_versions_needing_review(self.db, first.evidence_id),
            [claim.claim_version_id],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_version_id FROM claim_evidence"
            ).fetchone()[0],
            first.evidence_version_id,
        )

    def test_thumbnail_remains_exact_anchor_when_original_changes(self) -> None:
        thumbnail = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=2
        )
        first = self._create(thumbnail.file_id)
        original = bytearray(self.stored.read_bytes())
        original[-1] ^= 1
        self.stored.write_bytes(original)

        corrected = self._correct(first.evidence_version_id)
        self.assertEqual(
            self.db.execute(
                "SELECT anchor_file_id FROM evidence_versions WHERE evidence_version_id=?",
                (corrected.new_version_id,),
            ).fetchone()[0],
            thumbnail.file_id,
        )
        _, crop = reopen_image_region(self.db, corrected.new_version_id, self.root)
        self.assertEqual(crop.pixelColor(0, 0), QColor("red"))

        derived = self.root.joinpath(*thumbnail.storage_key.split("/"))
        damaged = bytearray(derived.read_bytes())
        damaged[-1] ^= 1
        derived.write_bytes(damaged)
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct(corrected.new_version_id)
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM evidence_versions WHERE evidence_id=?",
                (first.evidence_id,),
            ).fetchone()[0],
            2,
        )

    def test_tampered_or_missing_original_cannot_be_corrected(self) -> None:
        first = self._create()
        original = self.stored.read_bytes()
        damaged = bytearray(original)
        damaged[-1] ^= 1
        self.stored.write_bytes(damaged)
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct(first.evidence_version_id)
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.stored.write_bytes(original)
        self.stored.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct(first.evidence_version_id)
        self.assertEqual(failure.exception.reason, "missing_file")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_thumbnail_lineage_and_live_identity_are_required(self) -> None:
        thumbnail = create_image_thumbnail(
            self.imported.file_id, data_root=self.root, max_edge=2
        )
        first = self._create(thumbnail.file_id)
        self.db.execute(
            "UPDATE processing_runs SET status='FAILED',error_code='INVALID' WHERE run_id=?",
            (thumbnail.run_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "lineage is invalid"):
            self._correct(first.evidence_version_id)
        self.db.execute(
            "UPDATE processing_runs SET status='SUCCEEDED',error_code=NULL WHERE run_id=?",
            (thumbnail.run_id,),
        )
        self.db.execute(
            "UPDATE evidences SET deleted_at=? WHERE evidence_id=?",
            ("2026-10-02T00:00:00Z", first.evidence_id),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Current IMAGE_REGION"):
            self._correct(first.evidence_version_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )

    def test_invalid_region_and_stale_version_do_not_append(self) -> None:
        first = self._create()
        with self.assertRaisesRegex(ValueError, "Invalid image region"):
            self._correct(
                first.evidence_version_id,
                region={"x": 0.8, "y": 0.0, "width": 0.5, "height": 0.5},
            )
        with self.assertRaisesRegex(ValueError, "resolves to no pixels"):
            self._correct(
                first.evidence_version_id,
                region={"x": 0.5, "y": 0.5, "width": 1e-320, "height": 0.5},
            )
        with self.assertRaisesRegex(ValueError, "required"):
            self._correct(first.evidence_version_id, content=" ")
        corrected = self._correct(first.evidence_version_id)
        with self.assertRaisesRegex(ValueError, "latest Evidence Version"):
            self._correct(first.evidence_version_id)
        self.assertEqual(corrected.version_no, 2)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM evidence_versions WHERE evidence_id=?",
                (first.evidence_id,),
            ).fetchone()[0],
            2,
        )

    def test_review_failure_rolls_back_new_version(self) -> None:
        first = self._create()
        self.db.execute(
            """CREATE TEMP TRIGGER reject_image_correction
               BEFORE INSERT ON review_events
               WHEN NEW.action='CORRECT'
               BEGIN SELECT RAISE(ABORT,'review blocked'); END"""
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._correct(first.evidence_version_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_existing_caller_transaction_is_preserved(self) -> None:
        first = self._create()
        self.db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES(?,?,?)",
            ("caller-claim", "FACTUAL", "2026-10-02T00:00:00Z"),
        )
        with self.assertRaisesRegex(ValueError, "clean transaction"):
            self._correct(first.evidence_version_id)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM claims WHERE claim_id='caller-claim'"
            ).fetchone()[0],
            1,
        )
        self.db.rollback()
