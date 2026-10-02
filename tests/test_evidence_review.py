"""Check append-only human review of a verified Evidence Version."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize
from creator_loop.evidence_correction import correct_text_evidence
from creator_loop.evidence_reopen import EvidenceReopenError
from creator_loop.evidence_review import (
    current_evidence_review,
    record_evidence_review,
)
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


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu review"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Bản gốc.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        imported = intake_text_original(source, root=self.root)
        self.stored = self.root.joinpath(*imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.version = create_text_evidence(
            self.db,
            file_id=imported.file_id,
            data_root=self.root,
            start=0,
            end=4,
            actor="creator",
        )

    def _review(self, action: str, *, reason: str = "") -> str:
        return record_evidence_review(
            self.db,
            evidence_version_id=self.version.evidence_version_id,
            data_root=self.root,
            action=action,
            actor="reviewer",
            reason=reason,
        )

    def test_review_history_derives_current_action(self) -> None:
        self.assertEqual(
            current_evidence_review(self.db, self.version.evidence_version_id),
            "PENDING",
        )
        self._review("ACCEPT")
        self._review("REQUEST_CHANGES", reason="Cần kiểm câu dài hơn")
        self._review("REOPEN", reason="Đã xem lại nguồn")
        self.assertEqual(
            current_evidence_review(self.db, self.version.evidence_version_id),
            "REOPEN",
        )
        self._review("ACCEPT", reason="Đã kiểm lại")
        events = self.db.execute(
            "SELECT action,actor_id,reason FROM review_events ORDER BY rowid"
        ).fetchall()
        self.assertEqual(
            events,
            [
                ("ACCEPT", "reviewer", None),
                ("REQUEST_CHANGES", "reviewer", "Cần kiểm câu dài hơn"),
                ("REOPEN", "reviewer", "Đã xem lại nguồn"),
                ("ACCEPT", "reviewer", "Đã kiểm lại"),
            ],
        )
        self.assertEqual(
            current_evidence_review(self.db, self.version.evidence_version_id),
            "ACCEPT",
        )

    def test_accept_rechecks_original_but_reject_can_explain_unavailable_file(
        self,
    ) -> None:
        raw = bytearray(self.stored.read_bytes())
        raw[-1] ^= 1
        self.stored.write_bytes(raw)
        with self.assertRaises(EvidenceReopenError) as failure:
            self._review("ACCEPT")
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
        self._review("REJECT", reason="Tệp gốc đổi byte")
        self.assertEqual(
            current_evidence_review(self.db, self.version.evidence_version_id),
            "REJECT",
        )

    def test_corrected_old_version_cannot_receive_new_review(self) -> None:
        corrected = correct_text_evidence(
            self.db,
            evidence_version_id=self.version.evidence_version_id,
            data_root=self.root,
            start=7,
            end=10,
            actor="editor",
            reason="Chọn địa danh",
        )
        self.assertEqual(
            current_evidence_review(self.db, self.version.evidence_version_id),
            "CORRECT",
        )
        self.assertEqual(
            current_evidence_review(self.db, corrected.new_version_id), "PENDING"
        )
        with self.assertRaisesRegex(ValueError, "latest Evidence Version"):
            self._review("ACCEPT")

    def test_invalid_action_and_missing_reason_do_not_append(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid Evidence review action"):
            self._review("CORRECT")
        with self.assertRaisesRegex(ValueError, "Review reason is required"):
            self._review("REJECT")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_insert_failure_rolls_back_review(self) -> None:
        self.db.execute(
            """CREATE TRIGGER deny_review BEFORE INSERT ON review_events
               BEGIN SELECT RAISE(ABORT,'review blocked'); END"""
        )
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._review("ACCEPT")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )

    def test_review_preserves_pending_caller_writes_on_failure_and_success(
        self,
    ) -> None:
        self.db.execute(
            "INSERT INTO projects(project_id,title,status,created_at) VALUES(?,?,?,?)",
            ("caller-project", "Caller work", "ACTIVE", "2026-10-02T00:00:00Z"),
        )
        self.db.execute(
            """CREATE TEMP TRIGGER reject_review BEFORE INSERT ON review_events
               BEGIN SELECT RAISE(ABORT,'review blocked'); END"""
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "review blocked"):
            self._review("ACCEPT")
        with self.assertRaisesRegex(ValueError, "Live Evidence Version"):
            record_evidence_review(
                self.db,
                evidence_version_id="missing",
                data_root=self.root,
                action="ACCEPT",
                actor="reviewer",
            )
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
        self.db.execute("DROP TRIGGER reject_review")
        self._review("ACCEPT")
        other = sqlite3.connect(self.root / "creator_loop.sqlite3")
        self.addCleanup(other.close)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            other.execute("SELECT count(*) FROM projects").fetchone()[0], 0
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM review_events").fetchone()[0], 0
        )
        self.db.commit()
        self.assertEqual(
            other.execute("SELECT count(*) FROM projects").fetchone()[0], 1
        )
        self.assertEqual(
            other.execute("SELECT count(*) FROM review_events").fetchone()[0], 1
        )
