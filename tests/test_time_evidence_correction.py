"""Verify time correction using real Qt audio/video and immutable history."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import initialize

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.audio_evidence import (
        create_audio_evidence,
        reopen_audio_evidence,
    )
    from creator_loop.claims import EvidenceLink, claim_support_review, create_claim
    from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
    from creator_loop.evidence_correction import claim_versions_needing_review
    from creator_loop.evidence_reopen import EvidenceReopenError
    from creator_loop.evidence_review import (
        current_evidence_review,
        record_evidence_review,
    )
    from creator_loop.media_intake import intake_video_original
    from creator_loop.time_evidence_correction import correct_time_evidence
    from creator_loop.video_evidence import (
        create_video_evidence,
        reopen_video_evidence,
    )


@unittest.skipUnless(QApplication is not None, "requires Windows and Qt Multimedia")
class TimeEvidenceCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu sửa thời gian"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        fixture = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
        self.imported = intake_video_original(fixture, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.original_bytes = self.stored.read_bytes()
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def _create(self, track: str = "video", *, evidence_type: str = "OTHER"):
        arguments = {
            "file_id": self.imported.file_id,
            "data_root": self.root,
            "start_ms": 600 if track == "video" else 200,
            "end_ms": 800 if track == "video" else 600,
            "content": "Khung hình xanh" if track == "video" else "Âm kiểm thử",
            "actor": "creator",
        }
        if track == "video":
            return create_video_evidence(self.db, **arguments)
        return create_audio_evidence(self.db, **arguments, evidence_type=evidence_type)

    def _correct(self, version_id: str, **changes):
        arguments = {
            "evidence_version_id": version_id,
            "data_root": self.root,
            "start_ms": 100,
            "end_ms": 400,
            "content": "Quan sát đã sửa",
            "actor": " editor ",
            "reason": " Chọn lại đoạn ",
        }
        arguments.update(changes)
        return correct_time_evidence(self.db, **arguments)

    def _version_row(self, version_id: str):
        return self.db.execute(
            "SELECT * FROM evidence_versions WHERE evidence_version_id=?", (version_id,)
        ).fetchone()

    def _assert_no_correction(self, evidence_id: str) -> None:
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM evidence_versions WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_video_history_reopens_and_claim_projection_is_stale(self) -> None:
        first = self._create()
        old_row = self._version_row(first.evidence_version_id)
        file_row = self.db.execute("SELECT * FROM asset_files").fetchone()
        claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Đoạn video có khung hình xanh",
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
        corrected = self._correct(first.evidence_version_id, content=" Khung hình đỏ ")
        self.assertEqual(corrected.version_no, 2)
        self.assertEqual(corrected.evidence_id, first.evidence_id)
        self.assertEqual(self._version_row(first.evidence_version_id), old_row)
        self.assertEqual(
            self.db.execute("SELECT * FROM asset_files").fetchone(), file_row
        )
        self.assertEqual(self.stored.read_bytes(), self.original_bytes)

        old_content, old, old_start, old_end = reopen_video_evidence(
            self.db, first.evidence_version_id, self.root
        )
        new_content, new, new_start, new_end = reopen_video_evidence(
            self.db, corrected.new_version_id, self.root
        )
        self.assertEqual(
            (old_content, new_content), ("Khung hình xanh", "Khung hình đỏ")
        )
        self.assertEqual((old_start, old_end, new_start, new_end), (600, 800, 100, 400))
        self.assertEqual(old.anchor_path, self.stored.resolve())
        self.assertEqual(new.anchor_path, self.stored.resolve())
        self.assertGreater(
            old.image.pixelColor(0, 0).blue(), old.image.pixelColor(0, 0).red()
        )
        self.assertGreater(
            new.image.pixelColor(0, 0).red(), new.image.pixelColor(0, 0).blue()
        )
        self.assertEqual(
            self.db.execute(
                """SELECT anchor_file_id,producer_type,processing_run_id,created_by
                   FROM evidence_versions WHERE evidence_version_id=?""",
                (corrected.new_version_id,),
            ).fetchone(),
            (first.anchor_file_id, "HUMAN", None, "editor"),
        )
        self.assertEqual(
            self.db.execute(
                """SELECT evidence_version_id,action,actor_id,reason
                   FROM review_events ORDER BY rowid DESC LIMIT 1"""
            ).fetchone(),
            (first.evidence_version_id, "CORRECT", "editor", "Chọn lại đoạn"),
        )
        self.assertEqual(
            current_evidence_review(self.db, corrected.new_version_id), "PENDING"
        )
        projection = claim_support_review(self.db, claim.claim_version_id)
        self.assertEqual(
            (projection.accepted_current_supports, projection.stale_links), (0, 1)
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

    def test_audio_other_range_retains_track_type_and_exact_anchor(self) -> None:
        first = self._create("audio")
        before = self._version_row(first.evidence_version_id)
        corrected = self._correct(first.evidence_version_id, start_ms=600, end_ms=900)
        self.assertEqual(self._version_row(first.evidence_version_id), before)
        for version_id, expected_range in (
            (first.evidence_version_id, (200, 600)),
            (corrected.new_version_id, (600, 900)),
        ):
            _content, decoded, start, end = reopen_audio_evidence(
                self.db, version_id, self.root
            )
            self.assertEqual((start, end), expected_range)
            self.assertEqual(decoded.anchor_path, self.stored.resolve())
            self.assertLess(decoded.buffer_start_ms, end)
            self.assertGreater(decoded.buffer_end_ms, start)
            locator = self.db.execute(
                "SELECT locator_data FROM evidence_versions WHERE evidence_version_id=?",
                (version_id,),
            ).fetchone()[0]
            self.assertEqual(json.loads(locator)["track"], "audio")
        self.assertEqual(
            self.db.execute("SELECT evidence_type FROM evidences").fetchone()[0],
            "OTHER",
        )

    def test_machine_speech_run_and_raw_content_are_not_overwritten(self) -> None:
        timestamp = "2026-10-02T00:00:00Z"
        self.db.execute(
            """INSERT INTO processing_runs(
                 run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,
                 model_name,model_version,started_at,finished_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "raw-run",
                self.imported.asset_id,
                self.imported.file_id,
                "TRANSCRIPTION",
                "SUCCEEDED",
                "fixture",
                "1",
                "fixture-model",
                "1",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        self.db.commit()
        first = EvidenceVersion(
            "raw-version",
            "speech",
            self.imported.asset_id,
            1,
            self.imported.file_id,
            "RAW transcript fixture",
            "TIME_RANGE",
            json.dumps({"start_ms": 200, "end_ms": 600, "track": "audio"}),
            "MODEL",
            "raw-run",
            None,
            timestamp,
        )
        EvidenceRepository(self.db).create_with_version(
            Evidence("speech", self.imported.asset_id, "SPEECH", timestamp, None), first
        )
        old = self._version_row(first.evidence_version_id)
        run = self.db.execute("SELECT * FROM processing_runs").fetchone()
        corrected = self._correct(first.evidence_version_id, content="Human correction")
        self.assertEqual(self._version_row(first.evidence_version_id), old)
        self.assertEqual(
            self.db.execute("SELECT * FROM processing_runs").fetchone(), run
        )
        self.assertEqual(self.stored.read_bytes(), self.original_bytes)
        self.assertEqual(
            self.db.execute(
                "SELECT producer_type,processing_run_id,content FROM evidence_versions WHERE evidence_version_id=?",
                (corrected.new_version_id,),
            ).fetchone(),
            ("HUMAN", None, "Human correction"),
        )
        self.assertEqual(
            self.db.execute("SELECT evidence_type FROM evidences").fetchone()[0],
            "SPEECH",
        )

    def test_invalid_ranges_and_required_inputs_do_not_append(self) -> None:
        first = self._create()
        for changes in (
            {"start_ms": True},
            {"end_ms": 400.5},
            {"start_ms": -1},
            {"start_ms": 400, "end_ms": 400},
            {"end_ms": 5000},
            {"content": " "},
            {"actor": " "},
            {"reason": " "},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._correct(first.evidence_version_id, **changes)
            self._assert_no_correction(first.evidence_id)

    def test_video_range_without_decoded_frame_is_rejected(self) -> None:
        first = self._create()
        with self.assertRaisesRegex(ValueError, "No decoded video frame"):
            self._correct(first.evidence_version_id, start_ms=601, end_ms=602)
        self._assert_no_correction(first.evidence_id)

    def test_recorded_and_decoded_duration_both_bound_correction(self) -> None:
        first = self._create("audio")
        self.db.execute(
            "UPDATE asset_files SET duration_ms=650 WHERE file_id=?",
            (self.imported.file_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "exceeds duration"):
            self._correct(first.evidence_version_id, start_ms=600, end_ms=800)
        self._assert_no_correction(first.evidence_id)
        self.db.execute(
            "UPDATE asset_files SET duration_ms=NULL WHERE file_id=?",
            (self.imported.file_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "exceeds decoded duration"):
            self._correct(first.evidence_version_id, end_ms=5000)
        self._assert_no_correction(first.evidence_id)

    def test_stale_deleted_or_missing_version_is_rejected(self) -> None:
        first = self._create()
        corrected = self._correct(first.evidence_version_id)
        with self.assertRaisesRegex(ValueError, "latest Evidence Version"):
            self._correct(first.evidence_version_id)
        self.db.execute(
            "UPDATE evidences SET deleted_at=? WHERE evidence_id=?",
            ("2026-10-02T00:00:00Z", first.evidence_id),
        )
        self.db.commit()
        for version_id in (corrected.new_version_id, "missing"):
            with (
                self.subTest(version_id=version_id),
                self.assertRaisesRegex(ValueError, "Current TIME_RANGE"),
            ):
                self._correct(version_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 1
        )

    def test_other_locator_and_unsupported_track_type_are_rejected(self) -> None:
        first = self._create()
        repository = EvidenceRepository(self.db)
        for locator_type, locator_data, evidence_type in (
            ("WHOLE_ASSET", "{}", "VISUAL_OBSERVATION"),
            ("TIME_RANGE", first.locator_data, "SPEECH"),
        ):
            version = replace(
                first,
                evidence_id=locator_type,
                evidence_version_id=locator_type,
                locator_type=locator_type,
                locator_data=locator_data,
            )
            repository.create_with_version(
                Evidence(
                    locator_type,
                    self.imported.asset_id,
                    evidence_type,
                    first.created_at,
                    None,
                ),
                version,
            )
            with self.assertRaises(ValueError):
                self._correct(version.evidence_version_id)
            self._assert_no_correction(version.evidence_id)

    def test_tampered_or_missing_anchor_has_no_correction(self) -> None:
        first = self._create("audio")
        damaged = bytearray(self.original_bytes)
        damaged[-1] ^= 1
        self.stored.write_bytes(damaged)
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct(first.evidence_version_id)
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self._assert_no_correction(first.evidence_id)
        self.stored.write_bytes(self.original_bytes)
        self.stored.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct(first.evidence_version_id)
        self.assertEqual(failure.exception.reason, "missing_file")
        self._assert_no_correction(first.evidence_id)

    def test_review_failure_rolls_back_new_version_and_preserves_caller(self) -> None:
        first = self._create()
        self.db.execute(
            """CREATE TEMP TRIGGER reject_time_correction BEFORE INSERT ON review_events WHEN NEW.action='CORRECT' BEGIN SELECT RAISE(ABORT,'review blocked'); END"""
        )
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller-project", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._correct(first.evidence_version_id)
        self.assertTrue(self.db.in_transaction)
        self._assert_no_correction(first.evidence_id)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM projects WHERE project_id='caller-project'"
            ).fetchone()[0],
            1,
        )
        self.db.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._correct(first.evidence_version_id)
        self.assertFalse(self.db.in_transaction)
        self._assert_no_correction(first.evidence_id)

    def test_successful_correction_does_not_commit_caller_transaction(self) -> None:
        first = self._create("audio")
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller-project", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        corrected = self._correct(first.evidence_version_id)
        self.assertTrue(self.db.in_transaction)
        other = sqlite3.connect(self.db_path)
        self.addCleanup(other.close)
        self.assertEqual(
            other.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM projects").fetchone()[0], 0
        )
        self.db.commit()
        self.assertEqual(
            other.execute(
                "SELECT evidence_version_id FROM evidence_versions WHERE version_no=2"
            ).fetchone()[0],
            corrected.new_version_id,
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM review_events").fetchone()[0], 1
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM projects").fetchone()[0], 1
        )
