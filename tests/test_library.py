import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.library import Asset, LibraryRepository, Source


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = Path(self.temp.name) / "library.sqlite3"
        initialize(self.db_path)

        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

        self.repo = LibraryRepository(self.db)

    def test_create_source_asset_and_link(self) -> None:
        self.repo.create_source(
            Source(
                source_id="source-1",
                platform="FACEBOOK",
                canonical_url="https://example.invalid/post/1",
                external_id="post-1",
                publisher_name="Synthetic Publisher",
                published_at=None,
                captured_at="2026-09-30T00:00:00Z",
                rights_status="REFERENCE_ONLY",
                created_at="2026-09-30T00:00:00Z",
            )
        )

        self.repo.create_asset(
            Asset(
                asset_id="asset-1",
                media_type="VIDEO",
                display_name="Synthetic video",
                created_at="2026-09-30T00:00:00Z",
                deleted_at=None,
            )
        )

        self.repo.link_source_asset(
            source_id="source-1",
            asset_id="asset-1",
            relationship_type="ORIGIN",
            verification_status="UNREVIEWED",
            recorded_at="2026-09-30T00:00:00Z",
        )

        row = self.db.execute(
            """
            SELECT
                s.source_id,
                a.asset_id,
                sa.relationship_type,
                sa.verification_status
            FROM source_assets sa
            JOIN sources s ON s.source_id = sa.source_id
            JOIN assets a ON a.asset_id = sa.asset_id
            """
        ).fetchone()

        self.assertEqual(
            row,
            ("source-1", "asset-1", "ORIGIN", "UNREVIEWED"),
        )

    def test_database_rejects_invalid_media_type(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_asset(
                Asset(
                    asset_id="asset-bad",
                    media_type="MAGIC",
                    display_name="Invalid asset",
                    created_at="2026-09-30T00:00:00Z",
                    deleted_at=None,
                )
            )

    def test_database_rejects_invalid_rights_status(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_source(
                Source(
                    source_id="source-bad",
                    platform="TEST",
                    canonical_url=None,
                    external_id=None,
                    publisher_name=None,
                    published_at=None,
                    captured_at=None,
                    rights_status="TRUST_ME",
                    created_at="2026-09-30T00:00:00Z",
                )
            )



    def test_create_asset_file(self) -> None:
        self.repo.create_source(
            Source(
                source_id="source-file",
                platform="LOCAL",
                canonical_url=None,
                external_id=None,
                publisher_name=None,
                published_at=None,
                captured_at=None,
                rights_status="UNKNOWN",
                created_at="2026-09-30T00:00:00Z",
            )
        )
        self.repo.create_asset(
            Asset(
                asset_id="asset-file",
                media_type="VIDEO",
                display_name="Raw video",
                created_at="2026-09-30T00:00:00Z",
                deleted_at=None,
            )
        )

        self.repo.create_asset_file(
            asset_id="asset-file",
            file_id="file-original",
            role="ORIGINAL",
            storage_key="storage/originals/video.mp4",
            sha256="a" * 64,
            byte_size=1234,
            mime_type="video/mp4",
            parent_file_id=None,
            processing_run_id=None,
            created_at="2026-09-30T00:00:00Z",
        )

        row = self.db.execute(
            """
            SELECT asset_id, role, storage_key, byte_size
            FROM asset_files
            WHERE file_id=?
            """,
            ("file-original",),
        ).fetchone()

        self.assertEqual(
            row,
            ("asset-file", "ORIGINAL", "storage/originals/video.mp4", 1234),
        )

    def test_create_processing_run_for_asset_file(self) -> None:
        self.repo.create_asset(
            Asset(
                asset_id="asset-run",
                media_type="VIDEO",
                display_name="Processing video",
                created_at="2026-09-30T00:00:00Z",
                deleted_at=None,
            )
        )

        self.repo.create_asset_file(
            asset_id="asset-run",
            file_id="file-input",
            role="ORIGINAL",
            storage_key="storage/originals/input.mp4",
            sha256="b" * 64,
            byte_size=5678,
            mime_type="video/mp4",
            parent_file_id=None,
            processing_run_id=None,
            created_at="2026-09-30T00:00:00Z",
        )

        self.repo.create_processing_run(
            run_id="run-1",
            asset_id="asset-run",
            input_file_id="file-input",
            task_type="TRANSCRIBE",
            status="QUEUED",
            tool_name="fake-engine",
            tool_version="1.0",
            model_name=None,
            model_version=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            created_at="2026-09-30T00:00:00Z",
        )

        row = self.db.execute(
            """
            SELECT asset_id, input_file_id, task_type, status
            FROM processing_runs
            WHERE run_id=?
            """,
            ("run-1",),
        ).fetchone()

        self.assertEqual(
            row,
            ("asset-run", "file-input", "TRANSCRIBE", "QUEUED"),
        )


    def test_processing_run_rejects_input_file_from_another_asset(self) -> None:
        for asset_id in ("asset-a", "asset-b"):
            self.repo.create_asset(
                Asset(
                    asset_id=asset_id,
                    media_type="VIDEO",
                    display_name=asset_id,
                    created_at="2026-09-30T00:00:00Z",
                    deleted_at=None,
                )
            )

        self.repo.create_asset_file(
            asset_id="asset-a",
            file_id="file-a",
            role="ORIGINAL",
            storage_key="storage/originals/a.mp4",
            sha256="c" * 64,
            byte_size=100,
            mime_type="video/mp4",
            parent_file_id=None,
            processing_run_id=None,
            created_at="2026-09-30T00:00:00Z",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_processing_run(
                run_id="run-cross-asset",
                asset_id="asset-b",
                input_file_id="file-a",
                task_type="TRANSCRIBE",
                status="QUEUED",
                tool_name="fake-engine",
                tool_version="1.0",
                model_name=None,
                model_version=None,
                started_at=None,
                finished_at=None,
                error_code=None,
                error_message=None,
                created_at="2026-09-30T00:00:00Z",
            )

        count = self.db.execute(
            "SELECT count(*) FROM processing_runs WHERE run_id=?",
            ("run-cross-asset",),
        ).fetchone()[0]

        self.assertEqual(count, 0)

if __name__ == "__main__":
    unittest.main()
