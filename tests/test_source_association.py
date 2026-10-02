import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.source_association import (
    SourceDetails,
    create_source_for_asset,
    link_existing_source,
)


class SourceAssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = Path(self.temp.name) / "library.sqlite3"
        initialize(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        for asset_id in ("asset-1", "asset-2"):
            self.db.execute(
                "INSERT INTO assets(asset_id,media_type,display_name,created_at) "
                "VALUES(?,?,?,?)",
                (asset_id, "TEXT", asset_id, "2026-10-02T00:00:00Z"),
            )
        self.db.commit()

    def test_many_sources_one_asset_and_one_source_many_assets(self) -> None:
        local_id = create_source_for_asset(
            self.db,
            asset_id="asset-1",
            details=SourceDetails(platform="LOCAL", canonical_url="  "),
            relationship_type="UNKNOWN",
        )
        public_id = create_source_for_asset(
            self.db,
            asset_id="asset-1",
            details=SourceDetails(
                platform="WEB",
                canonical_url="https://example.invalid/post",
                publisher_name="Publisher",
                rights_status="REFERENCE_ONLY",
            ),
            relationship_type="REFERENCE",
        )
        link_existing_source(
            self.db,
            source_id=public_id,
            asset_id="asset-2",
            relationship_type="REFERENCE",
        )

        local = self.db.execute(
            "SELECT canonical_url,external_id,published_at,captured_at,rights_status "
            "FROM sources WHERE source_id=?",
            (local_id,),
        ).fetchone()
        self.assertEqual(local, (None, None, None, None, "UNKNOWN"))
        links = self.db.execute(
            "SELECT source_id,asset_id,relationship_type,verification_status "
            "FROM source_assets ORDER BY asset_id,source_id"
        ).fetchall()
        self.assertEqual(
            set(links),
            {
                (local_id, "asset-1", "UNKNOWN", "UNREVIEWED"),
                (public_id, "asset-1", "REFERENCE", "UNREVIEWED"),
                (public_id, "asset-2", "REFERENCE", "UNREVIEWED"),
            },
        )

    def test_failed_association_does_not_leave_orphan_source(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            create_source_for_asset(
                self.db,
                asset_id="missing",
                details=SourceDetails(platform="LOCAL"),
                relationship_type="UNKNOWN",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM sources").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM source_assets").fetchone()[0], 0
        )

    def test_invalid_rights_or_relationship_has_no_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown rights status"):
            create_source_for_asset(
                self.db,
                asset_id="asset-1",
                details=SourceDetails(platform="WEB", rights_status="ASSUMED_OWNED"),
                relationship_type="ORIGIN",
            )
        with self.assertRaisesRegex(ValueError, "Unknown Source relationship"):
            create_source_for_asset(
                self.db,
                asset_id="asset-1",
                details=SourceDetails(platform="WEB"),
                relationship_type="MAGIC",
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM sources").fetchone()[0], 0
        )
