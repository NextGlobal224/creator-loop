import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion


class EvidenceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = Path(self.temp.name) / "evidence.sqlite3"
        initialize(self.db_path)

        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

        self.db.execute(
            """
            INSERT INTO assets (
                asset_id, media_type, display_name, created_at, deleted_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "asset-1",
                "VIDEO",
                "Synthetic video",
                "2026-09-30T00:00:00Z",
                None,
            ),
        )

        self.db.execute(
            """
            INSERT INTO asset_files (
                file_id, asset_id, role, storage_key, sha256,
                byte_size, mime_type, parent_file_id,
                processing_run_id, created_at,
                width_px, height_px, duration_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "file-1",
                "asset-1",
                "ORIGINAL",
                "originals/synthetic.mp4",
                "sha256:test",
                100,
                "video/mp4",
                None,
                None,
                "2026-09-30T00:00:00Z",
                1920,
                1080,
                60000,
            ),
        )

        self.repo = EvidenceRepository(self.db)

    def test_create_evidence_with_first_version_and_provenance(self) -> None:
        evidence = Evidence(
            evidence_id="evidence-1",
            asset_id="asset-1",
            evidence_type="SPEECH",
            created_at="2026-09-30T00:00:00Z",
            deleted_at=None,
        )

        version = EvidenceVersion(
            evidence_version_id="evidence-version-1",
            evidence_id="evidence-1",
            asset_id="asset-1",
            version_no=1,
            anchor_file_id="file-1",
            content="Synthetic spoken evidence",
            locator_type="TIME_RANGE",
            locator_data=json.dumps(
                {
                    "start_ms": 1000,
                    "end_ms": 3000,
                    "track": "audio",
                }
            ),
            producer_type="HUMAN",
            processing_run_id=None,
            created_by="creator",
            created_at="2026-09-30T00:00:00Z",
        )

        self.repo.create_with_version(evidence, version)

        row = self.db.execute(
            """
            SELECT
                e.evidence_id,
                e.asset_id,
                v.version_no,
                v.anchor_file_id,
                v.locator_type,
                v.producer_type,
                v.created_by
            FROM evidences e
            JOIN evidence_versions v
              ON v.evidence_id = e.evidence_id
            WHERE e.evidence_id = ?
            """,
            ("evidence-1",),
        ).fetchone()

        self.assertEqual(
            row,
            (
                "evidence-1",
                "asset-1",
                1,
                "file-1",
                "TIME_RANGE",
                "HUMAN",
                "creator",
            ),
        )

    def test_invalid_locator_is_rejected_before_write(self) -> None:
        evidence = Evidence(
            evidence_id="evidence-bad",
            asset_id="asset-1",
            evidence_type="SPEECH",
            created_at="2026-09-30T00:00:00Z",
            deleted_at=None,
        )

        version = EvidenceVersion(
            evidence_version_id="evidence-version-bad",
            evidence_id="evidence-bad",
            asset_id="asset-1",
            version_no=1,
            anchor_file_id="file-1",
            content="Bad locator",
            locator_type="TIME_RANGE",
            locator_data=json.dumps(
                {
                    "start_ms": 50000,
                    "end_ms": 70000,
                    "track": "audio",
                }
            ),
            producer_type="MODEL",
            processing_run_id=None,
            created_by="model-test",
            created_at="2026-09-30T00:00:00Z",
        )

        with self.assertRaises(ValueError):
            self.repo.create_with_version(evidence, version)

        count = self.db.execute(
            "SELECT count(*) FROM evidences WHERE evidence_id=?",
            ("evidence-bad",),
        ).fetchone()[0]

        self.assertEqual(count, 0)


    def test_cross_asset_anchor_is_rejected_without_partial_evidence(self) -> None:
        self.db.execute(
            """
            INSERT INTO assets (
                asset_id, media_type, display_name, created_at, deleted_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "asset-other",
                "VIDEO",
                "Other synthetic video",
                "2026-09-30T00:00:00Z",
                None,
            ),
        )

        self.db.execute(
            """
            INSERT INTO asset_files (
                file_id,
                asset_id,
                role,
                storage_key,
                sha256,
                byte_size,
                mime_type,
                parent_file_id,
                processing_run_id,
                created_at,
                width_px,
                height_px,
                duration_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "file-other",
                "asset-other",
                "ORIGINAL",
                "originals/other.mp4",
                "b" * 64,
                200,
                "video/mp4",
                None,
                None,
                "2026-09-30T00:00:00Z",
                1920,
                1080,
                60000,
            ),
        )
        self.db.commit()

        evidence = Evidence(
            evidence_id="evidence-cross",
            asset_id="asset-1",
            evidence_type="SPEECH",
            created_at="2026-09-30T00:00:00Z",
            deleted_at=None,
        )

        version = EvidenceVersion(
            evidence_version_id="evidence-cross-v1",
            evidence_id="evidence-cross",
            asset_id="asset-1",
            version_no=1,
            anchor_file_id="file-other",
            content="Must never be written",
            locator_type="TIME_RANGE",
            locator_data='{"start_ms":0,"end_ms":1000,"track":"audio"}',
            producer_type="HUMAN",
            processing_run_id=None,
            created_by="creator",
            created_at="2026-09-30T00:00:00Z",
        )

        with self.assertRaises(ValueError):
            self.repo.create_with_version(evidence, version)

        evidence_count = self.db.execute(
            "SELECT count(*) FROM evidences WHERE evidence_id=?",
            ("evidence-cross",),
        ).fetchone()[0]

        version_count = self.db.execute(
            """
            SELECT count(*)
            FROM evidence_versions
            WHERE evidence_version_id=?
            """,
            ("evidence-cross-v1",),
        ).fetchone()[0]

        self.assertEqual(evidence_count, 0)
        self.assertEqual(version_count, 0)

    def test_append_version_preserves_history_and_requires_next_number(self) -> None:
        evidence = Evidence(
            evidence_id="evidence-history",
            asset_id="asset-1",
            evidence_type="SPEECH",
            created_at="2026-09-30T00:00:00Z",
            deleted_at=None,
        )

        first = EvidenceVersion(
            evidence_version_id="evidence-history-v1",
            evidence_id="evidence-history",
            asset_id="asset-1",
            version_no=1,
            anchor_file_id="file-1",
            content="Original transcript",
            locator_type="TIME_RANGE",
            locator_data='{"start_ms":0,"end_ms":1000,"track":"audio"}',
            producer_type="MODEL",
            processing_run_id=None,
            created_by="whisper",
            created_at="2026-09-30T00:00:00Z",
        )

        self.repo.create_with_version(evidence, first)

        second = EvidenceVersion(
            evidence_version_id="evidence-history-v2",
            evidence_id="evidence-history",
            asset_id="asset-1",
            version_no=2,
            anchor_file_id="file-1",
            content="Creator verified transcript",
            locator_type="TIME_RANGE",
            locator_data='{"start_ms":0,"end_ms":1000,"track":"audio"}',
            producer_type="HUMAN",
            processing_run_id=None,
            created_by="creator",
            created_at="2026-09-30T00:01:00Z",
        )

        self.repo.append_version(second)

        rows = self.db.execute(
            """
            SELECT version_no, content, producer_type
            FROM evidence_versions
            WHERE evidence_id=?
            ORDER BY version_no
            """,
            ("evidence-history",),
        ).fetchall()

        self.assertEqual(
            rows,
            [
                (1, "Original transcript", "MODEL"),
                (2, "Creator verified transcript", "HUMAN"),
            ],
        )

        skipped = EvidenceVersion(
            evidence_version_id="evidence-history-v4",
            evidence_id="evidence-history",
            asset_id="asset-1",
            version_no=4,
            anchor_file_id="file-1",
            content="Must not be written",
            locator_type="TIME_RANGE",
            locator_data='{"start_ms":0,"end_ms":1000,"track":"audio"}',
            producer_type="HUMAN",
            processing_run_id=None,
            created_by="creator",
            created_at="2026-09-30T00:02:00Z",
        )

        with self.assertRaises(ValueError):
            self.repo.append_version(skipped)

        count = self.db.execute(
            """
            SELECT count(*)
            FROM evidence_versions
            WHERE evidence_id=?
            """,
            ("evidence-history",),
        ).fetchone()[0]

        self.assertEqual(count, 2)
