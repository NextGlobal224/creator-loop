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


if __name__ == "__main__":
    unittest.main()
