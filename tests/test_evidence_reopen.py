import hashlib
import json
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import EvidenceReopenError, reopen_evidence_version


class EvidenceReopenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        db_path = self.root / "library.sqlite3"
        initialize(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.db.execute(
            "INSERT INTO assets(asset_id,media_type,display_name,created_at) "
            "VALUES('asset-1','TEXT','Sample','2026-10-01T00:00:00Z')"
        )
        self.db.commit()

    def _anchor(
        self,
        file_id: str,
        role: str,
        key: str,
        content: bytes,
        *,
        create_file: bool = True,
        duration_ms: int | None = None,
    ) -> Path:
        path = self.root.joinpath(*key.split("/"))
        if create_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.db.execute(
            """INSERT INTO asset_files(
                 file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,
                 created_at,duration_ms)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                file_id,
                "asset-1",
                role,
                key,
                hashlib.sha256(content).hexdigest(),
                len(content),
                "text/plain" if role == "ORIGINAL" else "video/mp4",
                "2026-10-01T00:00:00Z",
                duration_ms,
            ),
        )
        self.db.commit()
        return path

    def _version(
        self,
        file_id: str,
        locator_type: str,
        locator: dict[str, object],
        *,
        evidence_type: str = "DIRECT_TEXT",
    ) -> None:
        EvidenceRepository(self.db).create_with_version(
            Evidence(
                "evidence-1", "asset-1", evidence_type, "2026-10-01T00:00:00Z", None
            ),
            EvidenceVersion(
                "version-1",
                "evidence-1",
                "asset-1",
                1,
                file_id,
                "Recorded observation",
                locator_type,
                json.dumps(locator),
                "HUMAN",
                None,
                "creator",
                "2026-10-01T00:00:00Z",
            ),
        )

    def test_text_range_reopens_exact_original_with_nfc_code_points(self) -> None:
        source = "Cafe\u0301 and more"
        normalized = unicodedata.normalize("NFC", source)
        path = self._anchor(
            "file-text",
            "ORIGINAL",
            "storage/originals/Đà Nẵng/source.txt",
            b"\xef\xbb\xbf" + source.encode("utf-8"),
        )
        self._version(
            "file-text",
            "TEXT_RANGE",
            {
                "start": 0,
                "end": 4,
                "text_digest": "sha256:"
                + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            },
        )

        reopened = reopen_evidence_version(self.db, "version-1", self.root)

        self.assertEqual(reopened.anchor_file_id, "file-text")
        self.assertEqual(reopened.anchor_path, path.resolve())
        self.assertEqual(reopened.text_excerpt, "Café")
        self.assertEqual(reopened.locator_type, "TEXT_RANGE")

    def test_derived_anchor_is_selected_without_falling_back_to_original(self) -> None:
        self.db.execute("UPDATE assets SET media_type='VIDEO' WHERE asset_id='asset-1'")
        self.db.commit()
        self._anchor(
            "file-original", "ORIGINAL", "storage/originals/source.mp4", b"original"
        )
        derived = self._anchor(
            "file-derived",
            "DERIVED_AUDIO",
            "storage/derived/track.mp4",
            b"derived",
            duration_ms=3000,
        )
        self._version(
            "file-derived",
            "TIME_RANGE",
            {"start_ms": 1000, "end_ms": 2000, "track": "audio"},
            evidence_type="SPEECH",
        )

        reopened = reopen_evidence_version(self.db, "version-1", self.root)
        self.assertEqual(reopened.anchor_path, derived.resolve())
        self.assertEqual(reopened.locator["start_ms"], 1000)

        derived.unlink()
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_evidence_version(self.db, "version-1", self.root)
        self.assertEqual(failure.exception.reason, "missing_file")

    def test_changed_bytes_fail_even_when_size_is_unchanged(self) -> None:
        path = self._anchor(
            "file-text", "ORIGINAL", "storage/originals/source.txt", b"abc"
        )
        self._version("file-text", "WHOLE_ASSET", {})
        path.write_bytes(b"xyz")

        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_evidence_version(self.db, "version-1", self.root)
        self.assertEqual(failure.exception.reason, "digest_mismatch")

    def test_unsafe_storage_key_is_rejected(self) -> None:
        self._anchor(
            "file-text",
            "ORIGINAL",
            "storage/originals/../derived/escape.txt",
            b"abc",
            create_file=False,
        )
        self._version("file-text", "WHOLE_ASSET", {})

        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_evidence_version(self.db, "version-1", self.root)
        self.assertEqual(failure.exception.reason, "unsafe_path")

    def test_text_digest_must_match_anchor_snapshot(self) -> None:
        self._anchor("file-text", "ORIGINAL", "storage/originals/source.txt", b"actual")
        self._version(
            "file-text",
            "TEXT_RANGE",
            {"start": 0, "end": 3, "text_digest": "sha256:" + "0" * 64},
        )

        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_evidence_version(self.db, "version-1", self.root)
        self.assertEqual(failure.exception.reason, "invalid_locator")

    def test_unknown_version_reports_missing_version(self) -> None:
        with self.assertRaises(EvidenceReopenError) as failure:
            reopen_evidence_version(self.db, "missing", self.root)
        self.assertEqual(failure.exception.reason, "missing_version")
