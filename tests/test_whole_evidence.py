"""Verify exact whole-source Evidence with real text, pixels and MP4 decoding."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import initialize
from creator_loop.evidence import Evidence, EvidenceRepository
from creator_loop.evidence_reopen import EvidenceReopenError
from creator_loop.evidence_review import current_evidence_review
from creator_loop.text_intake import intake_text_original
from creator_loop.whole_evidence import (
    correct_whole_evidence,
    create_whole_evidence,
    read_verified_whole_source,
    reopen_whole_evidence,
)

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claims import EvidenceLink, claim_support_review, create_claim
    from creator_loop.evidence_correction import claim_versions_needing_review
    from creator_loop.evidence_review import record_evidence_review
    from creator_loop.image_thumbnail import create_image_thumbnail
    from creator_loop.media_intake import intake_image_original, intake_video_original


class _PortableOwnedFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = path.open("x+b")
        self.delete_on_close = False

    @classmethod
    def create_new(cls, path: Path) -> _PortableOwnedFile:
        return cls(path)

    def discard(self) -> None:
        self.delete_on_close = True

    def close(self) -> None:
        self.stream.close()
        if self.delete_on_close:
            self.path.unlink()


class WholeTextEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _PortableOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu toàn nguồn"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.original_bytes = (
            b"\xef\xbb\xbf" + "  Cafe\u0301 ở Huế 🏙\nDòng hai.\n".encode("utf-8")
        )
        source = Path(self.temp.name) / "Toàn nguồn.txt"
        source.write_bytes(self.original_bytes)
        self.imported = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.addCleanup(self.db.close)

    def _create(self, **changes):
        args = {
            "file_id": self.imported.file_id,
            "data_root": self.root,
            "actor": " creator ",
            "whole_source_relevant": True,
        }
        args.update(changes)
        return create_whole_evidence(self.db, **args)

    def _correct(self, version_id: str, **changes):
        args = {
            "evidence_version_id": version_id,
            "data_root": self.root,
            "actor": " editor ",
            "reason": " Xác nhận lại toàn nguồn ",
            "whole_source_relevant": True,
        }
        args.update(changes)
        return correct_whole_evidence(self.db, **args)

    def test_full_nfc_snapshot_is_preserved_and_pending_after_reopen(self) -> None:
        version = self._create()
        content, source = reopen_whole_evidence(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(content, "  Café ở Huế 🏙\nDòng hai.\n")
        self.assertEqual(content, version.content)
        self.assertEqual(source.text_snapshot, content)
        self.assertEqual(source.anchor_path, self.stored.resolve())
        self.assertEqual(source.anchor_file_id, self.imported.file_id)
        self.assertEqual(source.asset_id, self.imported.asset_id)
        self.assertEqual((source.media_type, source.role), ("TEXT", "ORIGINAL"))
        self.assertIsNone(source.image)
        self.assertIsNone(source.duration_ms)
        self.assertEqual(self.stored.read_bytes(), self.original_bytes)
        self.assertEqual(
            (
                version.locator_type,
                version.locator_data,
                version.producer_type,
                version.processing_run_id,
                version.created_by,
            ),
            ("WHOLE_ASSET", "{}", "HUMAN", None, "creator"),
        )
        self.assertEqual(
            self.db.execute("SELECT evidence_type FROM evidences").fetchone()[0],
            "DIRECT_TEXT",
        )
        self.assertEqual(
            current_evidence_review(self.db, version.evidence_version_id), "PENDING"
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
        self.db.close()
        self.db = sqlite3.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.assertEqual(
            reopen_whole_evidence(self.db, version.evidence_version_id, self.root)[0],
            content,
        )

    def test_confirmation_actor_and_exact_direct_text_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "Explicit confirmation"):
            create_whole_evidence(
                self.db,
                file_id=self.imported.file_id,
                data_root=self.root,
                actor="creator",
            )
        for changes in (
            {"whole_source_relevant": False},
            {"whole_source_relevant": 1},
            {"actor": " "},
            {"content": "Café"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._create(**changes)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )
        self.assertFalse(self.db.in_transaction)

    def test_changed_missing_or_unsupported_anchor_cannot_create_or_reopen(
        self,
    ) -> None:
        version = self._create()
        self.stored.write_bytes(self.original_bytes.replace(b"Cafe", b"Bake"))
        for operation in (
            self._create,
            lambda: reopen_whole_evidence(
                self.db, version.evidence_version_id, self.root
            ),
            lambda: self._correct(version.evidence_version_id),
        ):
            with self.assertRaises(EvidenceReopenError) as failure:
                operation()
            self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.stored.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            self._create()
        self.assertEqual(failure.exception.reason, "missing_file")
        self.stored.write_bytes(self.original_bytes)
        self.db.execute(
            "UPDATE assets SET media_type='DOCUMENT' WHERE asset_id=?",
            (self.imported.asset_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported whole-source"):
            self._create()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            read_verified_whole_source(self.db, "missing", self.root)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )

    def test_correction_guards_latest_live_version_and_original_locator(self) -> None:
        version = self._create()
        for changes in (
            {"whole_source_relevant": False},
            {"reason": " "},
            {"actor": " "},
            {"content": "Changed quote"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._correct(version.evidence_version_id, **changes)
        result = self._correct(version.evidence_version_id)
        with self.assertRaisesRegex(ValueError, "latest Evidence Version"):
            self._correct(version.evidence_version_id)
        self.db.execute(
            "UPDATE evidences SET deleted_at=? WHERE evidence_id=?",
            ("2026-10-02T00:00:00Z", version.evidence_id),
        )
        self.db.commit()
        for version_id in (result.new_version_id, "missing"):
            with (
                self.subTest(version_id=version_id),
                self.assertRaisesRegex(ValueError, "Current WHOLE_ASSET"),
            ):
                self._correct(version_id)
        self.assertEqual(
            reopen_whole_evidence(self.db, version.evidence_version_id, self.root)[0],
            version.content,
        )
        self.db.execute(
            "UPDATE assets SET deleted_at=? WHERE asset_id=?",
            ("2026-10-02T00:00:00Z", self.imported.asset_id),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "live Asset"):
            self._create()
        self.assertEqual(
            reopen_whole_evidence(self.db, result.new_version_id, self.root)[0],
            version.content,
        )

    def test_create_and_correct_success_do_not_commit_caller_work(self) -> None:
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        version = self._create()
        corrected = self._correct(version.evidence_version_id)
        self.assertTrue(self.db.in_transaction)
        other = sqlite3.connect(self.db_path)
        self.addCleanup(other.close)
        for table in ("projects", "evidences", "evidence_versions", "review_events"):
            self.assertEqual(
                other.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )
        self.db.commit()
        self.assertEqual(
            other.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 2
        )
        self.assertEqual(
            other.execute(
                "SELECT evidence_version_id,action,actor_id,reason FROM review_events"
            ).fetchone(),
            (
                version.evidence_version_id,
                "CORRECT",
                "editor",
                "Xác nhận lại toàn nguồn",
            ),
        )
        self.assertEqual(
            current_evidence_review(self.db, corrected.new_version_id), "PENDING"
        )

    def test_review_fail_rolls_back_version_and_trigger_writes_with_caller_preserved(
        self,
    ) -> None:
        version = self._create()
        self.db.execute(f"""CREATE TEMP TRIGGER reject_whole_correction AFTER INSERT ON review_events
            WHEN NEW.action='CORRECT' BEGIN
            UPDATE assets SET display_name='Unwanted trigger mutation' WHERE asset_id='{self.imported.asset_id}';
            SELECT RAISE(FAIL,'review blocked'); END""")
        before = self.db.execute("SELECT display_name FROM assets").fetchone()[0]
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        for caller_owned in (True, False):
            with (
                self.subTest(caller_owned=caller_owned),
                self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"),
            ):
                self._correct(version.evidence_version_id)
            self.assertEqual(self.db.in_transaction, caller_owned)
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0],
                1,
            )
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
            )
            self.assertEqual(
                self.db.execute("SELECT display_name FROM assets").fetchone()[0], before
            )
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM projects").fetchone()[0],
                int(caller_owned),
            )
            self.db.rollback()

    def test_create_failure_is_atomic_and_retains_caller_transaction(self) -> None:
        self.db.execute("""CREATE TEMP TRIGGER reject_whole_version AFTER INSERT ON evidence_versions
            BEGIN UPDATE projects SET title='Unwanted trigger mutation' WHERE project_id='caller';
            SELECT RAISE(FAIL,'version blocked'); END""")
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "version blocked"):
            self._create()
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute("SELECT title FROM projects").fetchone()[0], "Caller work"
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidences").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 0
        )


@unittest.skipUnless(QApplication is not None, "requires Windows and Qt Multimedia")
class WholeMediaEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu toàn ảnh video"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        source = Path(self.temp.name) / "Toàn ảnh.png"
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        for y in range(2, 4):
            for x in range(2, 4):
                image.setPixelColor(x, y, QColor("blue"))
        self.assertTrue(image.save(str(source)))
        self.image = intake_image_original(source, root=self.root)
        self.video = intake_video_original(
            Path(__file__).parent / "fixtures" / "video-with-tone.mp4", root=self.root
        )
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.addCleanup(self.db.close)

    def _create(self, imported=None, **changes):
        args = {
            "file_id": (imported or self.image).file_id,
            "data_root": self.root,
            "actor": "creator",
            "whole_source_relevant": True,
            "content": "Toàn nguồn có các vùng màu đỏ và xanh",
        }
        args.update(changes)
        return create_whole_evidence(self.db, **args)

    def _correct(self, version_id, **changes):
        args = {
            "evidence_version_id": version_id,
            "data_root": self.root,
            "actor": "editor",
            "reason": "Mô tả rõ quan sát",
            "whole_source_relevant": True,
            "content": "Vùng đầu đỏ, vùng cuối xanh",
        }
        args.update(changes)
        return correct_whole_evidence(self.db, **args)

    def test_whole_image_and_mp4_reopen_exact_anchor_and_decoded_pixels(self) -> None:
        for imported, media_type in ((self.image, "IMAGE"), (self.video, "VIDEO")):
            with self.subTest(media_type=media_type):
                version = self._create(imported)
                content, source = reopen_whole_evidence(
                    self.db, version.evidence_version_id, self.root
                )
                self.assertEqual(content, version.content)
                self.assertEqual(source.media_type, media_type)
                self.assertEqual(source.anchor_file_id, imported.file_id)
                self.assertEqual(
                    source.anchor_path,
                    self.root.joinpath(*imported.storage_key.split("/")).resolve(),
                )
                self.assertIsNone(source.text_snapshot)
                self.assertFalse(source.image.isNull())
                if media_type == "IMAGE":
                    self.assertEqual(
                        (source.image.width(), source.image.height()), (4, 4)
                    )
                    self.assertEqual(source.image.pixelColor(0, 0), QColor("red"))
                    self.assertEqual(source.image.pixelColor(3, 3), QColor("blue"))
                    self.assertIsNone(source.duration_ms)
                else:
                    self.assertGreaterEqual(source.duration_ms, 800)
                    self.assertGreater(
                        source.image.pixelColor(0, 0).red(),
                        source.image.pixelColor(0, 0).blue(),
                    )
                self.assertEqual(
                    self.db.execute(
                        "SELECT evidence_type FROM evidences WHERE evidence_id=?",
                        (version.evidence_id,),
                    ).fetchone()[0],
                    "VISUAL_OBSERVATION",
                )
                self.assertEqual(
                    current_evidence_review(self.db, version.evidence_version_id),
                    "PENDING",
                )
                self.assertEqual(
                    (
                        version.locator_data,
                        version.producer_type,
                        version.processing_run_id,
                    ),
                    ("{}", "HUMAN", None),
                )

    def test_each_media_correction_preserves_history_review_and_sealed_citations(
        self,
    ) -> None:
        for imported in (self.image, self.video):
            with self.subTest(file_id=imported.file_id):
                first = self._create(imported)
                before = self.db.execute(
                    "SELECT * FROM evidence_versions WHERE evidence_version_id=?",
                    (first.evidence_version_id,),
                ).fetchone()
                claim = create_claim(
                    self.db,
                    claim_type="FACTUAL",
                    statement="Nguồn có vùng xanh",
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
                self.assertEqual(
                    self.db.execute(
                        "SELECT * FROM evidence_versions WHERE evidence_version_id=?",
                        (first.evidence_version_id,),
                    ).fetchone(),
                    before,
                )
                self.assertEqual(
                    self.db.execute(
                        "SELECT anchor_file_id,locator_type,locator_data,producer_type,processing_run_id,version_no FROM evidence_versions WHERE evidence_version_id=?",
                        (corrected.new_version_id,),
                    ).fetchone(),
                    (imported.file_id, "WHOLE_ASSET", "{}", "HUMAN", None, 2),
                )
                self.assertEqual(
                    reopen_whole_evidence(
                        self.db, first.evidence_version_id, self.root
                    )[0],
                    first.content,
                )
                self.assertEqual(
                    reopen_whole_evidence(self.db, corrected.new_version_id, self.root)[
                        0
                    ],
                    "Vùng đầu đỏ, vùng cuối xanh",
                )
                self.assertEqual(
                    current_evidence_review(self.db, corrected.new_version_id),
                    "PENDING",
                )
                projection = claim_support_review(self.db, claim.claim_version_id)
                self.assertEqual(
                    (projection.accepted_current_supports, projection.stale_links),
                    (0, 1),
                )
                self.assertEqual(
                    claim_versions_needing_review(self.db, first.evidence_id),
                    [claim.claim_version_id],
                )
                self.assertEqual(
                    self.db.execute(
                        "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id=?",
                        (claim.claim_version_id,),
                    ).fetchone()[0],
                    first.evidence_version_id,
                )

    def test_visual_content_type_and_locator_guards_are_enforced(self) -> None:
        for changes in (
            {"content": None},
            {"content": " "},
            {"actor": " "},
            {"whole_source_relevant": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._create(**changes)
        first = self._create()
        for locator_type, locator_data, evidence_type in (
            ("WHOLE_ASSET", "{}", "DIRECT_TEXT"),
            (
                "IMAGE_REGION",
                '{"x":0,"y":0,"width":1,"height":1}',
                "VISUAL_OBSERVATION",
            ),
        ):
            invalid = replace(
                first,
                evidence_id=locator_type,
                evidence_version_id=locator_type,
                locator_type=locator_type,
                locator_data=locator_data,
            )
            EvidenceRepository(self.db).create_with_version(
                Evidence(
                    invalid.evidence_id,
                    invalid.asset_id,
                    evidence_type,
                    invalid.created_at,
                    None,
                ),
                invalid,
            )
            with self.assertRaises(ValueError):
                reopen_whole_evidence(self.db, invalid.evidence_version_id, self.root)
            with self.assertRaises(ValueError):
                self._correct(invalid.evidence_version_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_image_decode_is_required_and_video_tamper_is_rejected(self) -> None:
        fake = Path(self.temp.name) / "Ảnh không giải mã.png"
        fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not a decodable image")
        imported = intake_image_original(fake, root=self.root)
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            self._create(imported)
        version = self._create(self.video)
        stored = self.root.joinpath(*self.video.storage_key.split("/"))
        raw = bytearray(stored.read_bytes())
        raw[-1] ^= 1
        stored.write_bytes(raw)
        for operation in (
            lambda: self._create(self.video),
            lambda: reopen_whole_evidence(
                self.db, version.evidence_version_id, self.root
            ),
            lambda: self._correct(version.evidence_version_id),
        ):
            with self.assertRaises(ValueError) as failure:
                operation()
            self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_thumbnail_is_whole_exact_thumbnail_with_no_original_fallback(self) -> None:
        thumbnail = create_image_thumbnail(
            self.image.file_id, data_root=self.root, max_edge=2
        )
        version = self._create(file_id=thumbnail.file_id)
        original = self.root.joinpath(*self.image.storage_key.split("/"))
        original.unlink()
        content, source = reopen_whole_evidence(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(content, version.content)
        self.assertEqual(
            (source.role, source.anchor_file_id), ("THUMBNAIL", thumbnail.file_id)
        )
        self.assertEqual((source.image.width(), source.image.height()), (2, 2))
        corrected = self._correct(version.evidence_version_id)
        self.assertEqual(
            reopen_whole_evidence(self.db, corrected.new_version_id, self.root)[
                1
            ].anchor_file_id,
            thumbnail.file_id,
        )
