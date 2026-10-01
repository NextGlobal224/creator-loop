from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize
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


class TextEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu"
        self.root.mkdir()
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        self.source = Path(self.temp.name) / "Bản gốc.txt"
        self.source.write_bytes(b"\xef\xbb\xbf" + "Cafe\u0301 ở Huế".encode("utf-8"))
        self.imported = intake_text_original(self.source, root=self.root)
        self.stored = self.root.joinpath(*self.imported.storage_key.split("/"))
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def _create(self, start: int = 0, end: int = 4):
        return create_text_evidence(
            self.db,
            file_id=self.imported.file_id,
            data_root=self.root,
            start=start,
            end=end,
            actor="creator",
        )

    def _evidence_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0]

    def test_import_create_and_reopen_same_nfc_excerpt(self) -> None:
        version = self._create()

        self.assertEqual(version.content, "Café")
        self.assertEqual(version.asset_id, self.imported.asset_id)
        self.assertEqual(version.anchor_file_id, self.imported.file_id)
        self.assertEqual(version.producer_type, "HUMAN")
        reopened = reopen_evidence_version(
            self.db, version.evidence_version_id, self.root
        )
        self.assertEqual(reopened.text_excerpt, "Café")
        self.assertEqual(reopened.anchor_path, self.stored.resolve())
        self.assertEqual(self._evidence_count(), 1)

    def test_changed_original_is_rejected_before_evidence_write(self) -> None:
        changed = self.stored.read_bytes().replace(b"Cafe", b"Bake")
        self.assertEqual(len(changed), self.stored.stat().st_size)
        self.stored.write_bytes(changed)

        with self.assertRaises(EvidenceReopenError) as failure:
            self._create()
        self.assertEqual(failure.exception.reason, "digest_mismatch")
        self.assertEqual(self._evidence_count(), 0)

    def test_out_of_bounds_range_has_no_partial_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "Text range exceeds"):
            self._create(0, 999)
        self.assertEqual(self._evidence_count(), 0)

    def test_missing_original_has_no_partial_evidence(self) -> None:
        self.stored.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            self._create()
        self.assertEqual(failure.exception.reason, "missing_file")
        self.assertEqual(self._evidence_count(), 0)
