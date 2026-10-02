"""Verify corrected TEXT versions retain history and reveal stale Claim links."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize
from creator_loop.evidence_correction import (
    claim_versions_needing_review,
    correct_text_evidence,
)
from creator_loop.evidence_reopen import EvidenceReopenError, reopen_evidence_version
from creator_loop.text_evidence import create_text_evidence
from creator_loop.text_intake import intake_text_original


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


class EvidenceCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu sửa Evidence"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Bản ghi.txt"
        source.write_text("Cafe\u0301 ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.first = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=0,
            end=4,
            actor="creator",
        )

    def _correct(self, *, start: int = 0, end: int = 10):
        return correct_text_evidence(
            self.db,
            evidence_version_id=self.first.evidence_version_id,
            data_root=self.root,
            start=start,
            end=end,
            actor="editor",
            reason="Mở rộng câu trích",
        )

    def test_correction_keeps_old_version_and_marks_claim_dependency(self) -> None:
        self.db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES(?,?,?)",
            ("claim-1", "FACTUAL", "2026-10-02T00:00:00Z"),
        )
        self.db.execute(
            """INSERT INTO claim_versions(
                 claim_version_id,claim_id,version_no,statement,created_at)
               VALUES(?,?,?,?,?)""",
            ("claim-version-1", "claim-1", 1, "Một câu", "2026-10-02T00:00:00Z"),
        )
        self.db.execute(
            """INSERT INTO claim_evidence(
                 claim_version_id,evidence_version_id,relation_type)
               VALUES(?,?,?)""",
            ("claim-version-1", self.first.evidence_version_id, "SUPPORTS"),
        )
        self.db.commit()

        corrected = self._correct()
        versions = self.db.execute(
            """SELECT evidence_version_id,version_no,content
               FROM evidence_versions WHERE evidence_id=? ORDER BY version_no""",
            (self.first.evidence_id,),
        ).fetchall()
        self.assertEqual(versions[0], (self.first.evidence_version_id, 1, "Café"))
        self.assertEqual(versions[1], (corrected.new_version_id, 2, "Café ở Huế"))
        review = self.db.execute(
            "SELECT evidence_version_id,action,actor_id,reason FROM review_events"
        ).fetchone()
        self.assertEqual(
            review,
            (
                self.first.evidence_version_id,
                "CORRECT",
                "editor",
                "Mở rộng câu trích",
            ),
        )
        self.assertEqual(
            claim_versions_needing_review(self.db, self.first.evidence_id),
            ["claim-version-1"],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_version_id FROM claim_evidence"
            ).fetchone()[0],
            self.first.evidence_version_id,
        )
        with closing(sqlite3.connect(self.root / "creator_loop.sqlite3")) as read_db:
            old = reopen_evidence_version(
                read_db, self.first.evidence_version_id, self.root
            )
            new = reopen_evidence_version(read_db, corrected.new_version_id, self.root)
        self.assertEqual((old.text_excerpt, new.text_excerpt), ("Café", "Café ở Huế"))

    def test_invalid_range_rolls_back_review_and_new_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds snapshot"):
            self._correct(end=999)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_changed_original_cannot_be_corrected(self) -> None:
        self.stored.write_bytes(self.stored.read_bytes().replace(b"Cafe", b"Bake"))
        with self.assertRaises(EvidenceReopenError) as failure:
            self._correct()
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )

    def test_only_latest_version_can_be_corrected(self) -> None:
        self._correct()
        with self.assertRaisesRegex(ValueError, "latest Evidence Version"):
            self._correct()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 2
        )

    def test_review_insert_failure_rolls_back_new_version(self) -> None:
        self.db.execute(
            """CREATE TEMP TRIGGER reject_correction
               BEFORE INSERT ON review_events
               WHEN NEW.action='CORRECT'
               BEGIN SELECT RAISE(ABORT,'review blocked'); END"""
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._correct()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
