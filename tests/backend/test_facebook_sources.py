from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.facebook.facebook_sources import (
    post_link,
    save_capture,
    saved_sources,
)
from creator_loop.backend.library.library import LibraryRepository, Source
from creator_loop.backend.library.originals import verify_original_file
from creator_loop.backend.runtime.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    _connect_write,
    _execute_migration,
    _migration_sql,
    initialize,
    validate,
)

from tests.backend.test_text_intake import _NonWindowsOwnedFile


class FacebookSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu Facebook"
        self.root.mkdir()
        self.root = self.root.resolve(strict=True)
        (self.root / "runtime").mkdir()
        initialize(self.root / "creator_loop.sqlite3")
        if os.name != "nt":
            p = patch(
                "creator_loop.backend.library.text_intake.OwnedWindowsFile",
                _NonWindowsOwnedFile,
            )
            p.start()
            self.addCleanup(p.stop)

    def db(self):
        return _connect_write(self.root / "creator_loop.sqlite3")

    def test_post_paths_keep_identity_not_first_profile(self):
        valid = {
            "https://m.facebook.com/creator/posts/pfbidAbC123/?ref=tracking": "pfbidAbC123",
            "https://www.facebook.com/groups/23/posts/45/": "45",
            "https://www.facebook.com/groups/23/permalink/45/?ref=tracking": "45",
            "https://www.facebook.com/groups/23/?multi_permalinks=45&hoisted_section_header_type=recently_seen": "45",
            "https://www.facebook.com/reel/67": "67",
            "https://www.facebook.com/creator/videos/89": "89",
            "https://facebook.com/watch/?v=90&ref=tracking": "90",
            "https://www.facebook.com/story.php?story_fbid=123&id=456&fbclid=secret": "123",
        }
        for value, identity in valid.items():
            with self.subTest(value=value):
                link = post_link(value)
                self.assertIsNotNone(link)
                self.assertEqual(link.external_id, identity)
                self.assertNotIn("tracking", link.url)
                self.assertNotIn("fbclid", link.url)
        self.assertIn("story_fbid=123&id=456", post_link(list(valid)[-1]).url)
        for value in (
            "https://www.facebook.com/profile.php?id=12",
            "https://www.facebook.com/creator",
            "https://evil.test/posts/12",
            "http://facebook.com/reel/1",
            "https://facebook.com@evil.test/reel/1",
            "https://www.facebook.com/watch/?v=1&v=2",
            "https://facebook.com/%2f..%2f/posts/1",
            "https://facebook.com/share/p/1",
            "https://www.facebook.com/groups/23/?multi_permalinks=45,67",
            "https://www.facebook.com/groups/23/?multi_permalinks=45&multi_permalinks=67",
        ):
            self.assertIsNone(post_link(value), value)

    def test_url_only_never_fabricates_asset_file(self):
        source, new = save_capture(
            self.root,
            keyword="Đà Nẵng",
            url="https://www.facebook.com/reel/12",
            excerpt=None,
        )
        self.assertTrue(new)
        with closing(self.db()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM asset_files").fetchone()[0], 0
            )
            row = db.execute(
                "SELECT platform,rights_status,published_at FROM sources WHERE source_id=?",
                (source,),
            ).fetchone()
            self.assertEqual(row, ("FB", "UNKNOWN", None))
            validate(db)

    def test_snapshot_reopen_and_cross_run_dedup_preserve_bytes(self):
        source, new = save_capture(
            self.root,
            keyword="từ khóa 1",
            url="https://www.facebook.com/creator/posts/123",
            excerpt="Trích đoạn thật",
            publisher="Nguồn",
        )
        self.assertTrue(new)
        rows = saved_sources(self.root)
        self.assertEqual(len(rows), 1)
        with closing(self.db()) as db:
            item = db.execute(
                "SELECT f.asset_id,f.file_id,f.sha256,f.byte_size,a.media_type FROM asset_files f JOIN assets a USING(asset_id)"
            ).fetchone()
            self.assertEqual(item[4], "TEXT")
            verify_original_file(db, item[1], self.root)
            # Existing verifier returns metadata identity; compare stored bytes directly below.
            key = db.execute("SELECT storage_key FROM asset_files").fetchone()[0]
        before = (self.root / key).read_bytes()
        self.assertIn("Trích đoạn thật", before.decode("utf-8"))
        source2, new2 = save_capture(
            self.root,
            keyword="từ khóa 2",
            url="https://m.facebook.com/creator/posts/123?tracking=2",
            excerpt="KHÔNG ghi đè",
        )
        self.assertEqual(source2, source)
        self.assertFalse(new2)
        self.assertEqual((self.root / key).read_bytes(), before)
        self.assertEqual(saved_sources(self.root), rows)

    def test_failed_capture_rolls_back_physical_text_and_rows(self):
        with closing(self.db()) as db:
            db.execute(
                "CREATE TRIGGER reject_capture BEFORE INSERT ON source_captures BEGIN SELECT RAISE(ABORT,'test failure'); END"
            )
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            save_capture(
                self.root,
                keyword="rollback",
                url="https://facebook.com/reel/99",
                excerpt="fail atomically",
            )
        with closing(self.db()) as db:
            for table in ("sources", "source_captures", "assets", "asset_files"):
                self.assertEqual(
                    db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0
                )
        self.assertEqual(list((self.root / "storage" / "originals").glob("*")), [])
        self.assertEqual(list((self.root / "runtime").glob("facebook-capture-*")), [])

    def test_existing_manual_source_identity_and_rights_are_preserved(self):
        with closing(self.db()) as db:
            LibraryRepository(db).create_source(
                Source(
                    "manual",
                    "FB",
                    "https://facebook.com/reel/100",
                    "100",
                    "manual publisher",
                    None,
                    None,
                    "OWNED",
                    "2026-10-01T00:00:00Z",
                )
            )
            db.commit()
        sid, new = save_capture(
            self.root, keyword="now", url="https://facebook.com/reel/100", excerpt=None
        )
        self.assertEqual(sid, "manual")
        self.assertTrue(new)
        with closing(self.db()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT publisher_name,rights_status FROM sources"
                ).fetchone(),
                ("manual publisher", "OWNED"),
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM sources").fetchone()[0], 1
            )

    def test_capture_immutable_replace_update_delete(self):
        sid, _ = save_capture(
            self.root, keyword="first", url="https://facebook.com/reel/12", excerpt=None
        )
        with closing(self.db()) as db:
            original = db.execute("SELECT * FROM source_captures").fetchone()
            for sql in (
                "UPDATE source_captures SET keyword='other'",
                "DELETE FROM source_captures",
                "INSERT OR REPLACE INTO source_captures VALUES (?,?,?,?,?)",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql, original if "REPLACE" in sql else ())
                self.assertEqual(
                    db.execute("SELECT * FROM source_captures").fetchone(), original
                )

    def test_migration_from_exact_schema6_backs_up_and_preserves_source(self):
        old = self.root / "old.sqlite3"
        with closing(_connect_write(old)) as db:
            for number, migration in enumerate(MIGRATIONS[:6], 1):
                sql, digest = _migration_sql(migration)
                _execute_migration(db, sql)
                db.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                    (migration, digest, "2026-10-01T00:00:00Z", "0.1.0"),
                )
                db.execute("PRAGMA user_version=" + str(number))
                db.commit()
            LibraryRepository(db).create_source(
                Source(
                    "old",
                    "FB",
                    "https://facebook.com/reel/123",
                    "123",
                    None,
                    None,
                    None,
                    "UNKNOWN",
                    "2026-10-01T00:00:00Z",
                )
            )
            db.commit()
            before = db.execute("SELECT * FROM sources").fetchall()
        initialize(old)
        with closing(_connect_write(old)) as db:
            validate(db)
            self.assertEqual(db.execute("SELECT * FROM sources").fetchall(), before)
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_captures").fetchone()[0], 0
            )
        backups = list(self.root.glob(f"old.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(_connect_write(backups[0])) as db:
            validate(db, expected_version=6)
            self.assertEqual(db.execute("SELECT * FROM sources").fetchall(), before)

    def test_cancel_after_original_copy_rolls_back(self):
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] >= 3

        with self.assertRaises(InterruptedError):
            save_capture(
                self.root,
                keyword="cancel",
                url="https://facebook.com/reel/77",
                excerpt="no commit",
                cancelled=cancel,
            )
        with closing(self.db()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_captures").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM asset_files").fetchone()[0], 0
            )
        self.assertEqual(list((self.root / "storage" / "originals").glob("*")), [])

    def test_captured_reference_cannot_be_removed_or_reinterpreted(self):
        sid, _ = save_capture(
            self.root,
            keyword="retained",
            url="https://facebook.com/reel/78",
            excerpt="text",
        )
        with closing(self.db()) as db:
            original = db.execute("SELECT * FROM source_assets").fetchone()
            for sql in (
                "DELETE FROM source_assets",
                "UPDATE source_assets SET relationship_type='REPOST'",
            ):
                values = (
                    (original[0], original[1], "PRIMARY", original[3], original[4])
                    if "REPLACE" in sql
                    else ()
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql, values)
                self.assertEqual(
                    db.execute("SELECT * FROM source_assets").fetchone(), original
                )

    def test_other_source_relationships_remain_allowed(self):
        sid, _ = save_capture(
            self.root,
            keyword="reference",
            url="https://facebook.com/reel/79",
            excerpt="text",
        )
        with closing(self.db()) as db:
            row = db.execute("SELECT * FROM source_assets").fetchone()
            db.execute(
                "INSERT INTO source_assets VALUES (?,?,?,?,?)",
                (row[0], row[1], "ORIGIN", row[3], row[4]),
            )
            db.execute("DELETE FROM source_assets WHERE relationship_type='ORIGIN'")
            self.assertEqual(db.execute("SELECT * FROM source_assets").fetchone(), row)
