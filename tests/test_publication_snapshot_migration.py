"""SQL replacement attacks and supported upgrades preserve publication history.

These are metadata fixtures, not codec or physical-media acceptance evidence.
"""

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    initialize,
    migration_path,
    validate,
)


class PublicationSnapshotMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Lịch sử Publication"
        self.root.mkdir()

    def _legacy(self, version):
        path = self.root / f"schema-{version}.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for migration in MIGRATIONS[:version]:
                sql = migration_path(migration).read_text(encoding="utf-8")
                db.executescript(sql)
                db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                    (
                        migration,
                        hashlib.sha256(sql.encode()).hexdigest(),
                        "now",
                        "0.1.0",
                    ),
                )
            db.execute(f"PRAGMA user_version={version}")
            db.execute("INSERT INTO assets VALUES('a','TEXT','Source','now',NULL)")
            db.execute(
                "INSERT INTO asset_files VALUES('f','a','ORIGINAL','storage/originals/a/source.txt',?,5,'text/plain',NULL,NULL,'now',NULL,NULL,NULL)",
                ("0" * 64,),
            )
            db.execute("INSERT INTO evidences VALUES('e','a','DIRECT_TEXT','now',NULL)")
            db.execute(
                "INSERT INTO evidence_versions VALUES('ev','e','a',1,'f','hello','TEXT_RANGE',?,'HUMAN',NULL,'editor','now')",
                ('{"start":0,"end":5,"text_digest":"sha256:' + "0" * 64 + '"}',),
            )
            db.execute("INSERT INTO claims VALUES('c','FACTUAL','now',NULL)")
            db.execute(
                "INSERT INTO claim_versions VALUES('cv','c',1,'hello','editor','now')"
            )
            db.execute("INSERT INTO claim_evidence VALUES('cv','ev','SUPPORTS')")
            if version >= 2:
                db.execute("INSERT INTO claim_version_seals VALUES('cv','now')")
            db.execute("INSERT INTO projects VALUES('p','History','ACTIVE','now',NULL)")
            db.execute("INSERT INTO drafts VALUES('d','p','ACTIVE','now')")
            db.execute(
                "INSERT INTO draft_versions VALUES('dv','d',1,'hello','POST','editor','now')"
            )
            db.execute("INSERT INTO draft_claims VALUES('dv','cv','ASSERTED')")
            db.execute(
                "INSERT INTO draft_assertions VALUES('assertion','dv',0,5,'hello','cv','SUPPORTED')"
            )
            if version >= 3:
                db.execute("INSERT INTO draft_version_seals VALUES('dv','now')")
            db.execute(
                "INSERT INTO review_events VALUES('review',NULL,'cv',NULL,'ACCEPT','human',NULL,'now')"
            )
            db.execute(
                "INSERT INTO publication_packages VALUES('pkg','p','dv','FB','POST','fingerprint',1,NULL,'now')"
            )
            db.execute(
                "INSERT INTO publication_package_items VALUES('item','pkg','CAPTION',0,'hello',NULL,'digest')"
            )
            db.execute("UPDATE publication_packages SET sealed_at='now'")
            db.execute(
                "INSERT INTO approvals VALUES('approval','pkg','fingerprint','APPROVED','human','now',NULL)"
            )
            db.execute(
                "INSERT INTO posts VALUES('post','pkg','FB','external','https://example.invalid/post','now','PUBLISHED','now')"
            )
            db.execute(
                "INSERT INTO observations VALUES('observation','post','now','MANUAL',NULL,'now')"
            )
            db.execute(
                "INSERT INTO observation_metrics VALUES('observation','views','POST',0,'count','v1',NULL)"
            )
            db.commit()
        return path

    def _snapshot(self, db):
        tables = (
            "assets",
            "asset_files",
            "evidences",
            "evidence_versions",
            "claims",
            "claim_versions",
            "claim_evidence",
            "draft_versions",
            "draft_assertions",
            "review_events",
            "publication_packages",
            "publication_package_items",
            "approvals",
            "posts",
            "observations",
            "observation_metrics",
        )
        return {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }

    def test_upgrade_schemas_1_2_3_4_and_validated_backup_preserve_history(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                path = self._legacy(version)
                with closing(sqlite3.connect(path)) as db:
                    before = self._snapshot(db)
                initialize(path)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    validate(db)
                    self.assertEqual(
                        db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
                    )
                    self.assertEqual(self._snapshot(db), before)
                backups = list(
                    path.parent.glob(f"{path.name}.pre-v{SCHEMA_VERSION}-*.sqlite3")
                )
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as backup:
                    backup.execute("PRAGMA foreign_keys=ON")
                    validate(backup, expected_version=version)
                    self.assertEqual(self._snapshot(backup), before)
                initialize(path)
                self.assertEqual(
                    len(
                        list(
                            path.parent.glob(
                                f"{path.name}.pre-v{SCHEMA_VERSION}-*.sqlite3"
                            )
                        )
                    ),
                    1,
                )

    def test_replace_cannot_delete_and_reinsert_snapshot_or_review_rows(self):
        path = self._legacy(4)
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA recursive_triggers=OFF")
            before = self._snapshot(db)
            tables = list(before) + ["claim_version_seals", "draft_version_seals"]
            for table in tables:
                if table == "claim_evidence":
                    continue  # Already rejected by sealed citation insert guard.
                row = db.execute(f"SELECT * FROM {table}").fetchone()
                with (
                    self.subTest(table=table),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    db.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' for _ in row)})",
                        row,
                    )
                db.rollback()
            self.assertEqual(self._snapshot(db), before)

    def test_replace_secondary_unique_keys_cannot_reassign_historical_ids(self):
        path = self._legacy(4)
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            before = self._snapshot(db)
            for table in (
                "evidence_versions",
                "claim_versions",
                "publication_package_items",
                "posts",
            ):
                row = list(db.execute(f"SELECT * FROM {table}").fetchone())
                row[0] = "replacement"
                with (
                    self.subTest(table=table),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    db.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' for _ in row)})",
                        row,
                    )
                db.rollback()
            self.assertEqual(self._snapshot(db), before)

    def test_item_or_assertion_id_cannot_be_moved_to_an_unsealed_snapshot(self):
        path = self._legacy(4)
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "INSERT INTO draft_versions VALUES('dv2','d',2,'hello','POST','editor','now')"
            )
            db.execute(
                "INSERT INTO publication_packages VALUES('building','p','dv','FB','POST','new',1,NULL,'now')"
            )
            db.commit()
            for table, target in (
                ("draft_assertions", "dv2"),
                ("publication_package_items", "building"),
            ):
                row = list(db.execute(f"SELECT * FROM {table}").fetchone())
                row[1] = target
                with (
                    self.subTest(table=table),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    db.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' for _ in row)})",
                        row,
                    )
                db.rollback()
            self.assertEqual(
                db.execute(
                    "SELECT package_id FROM publication_package_items"
                ).fetchone()[0],
                "pkg",
            )
            self.assertEqual(
                db.execute("SELECT draft_version_id FROM draft_assertions").fetchone()[
                    0
                ],
                "dv",
            )

    def test_claim_type_cannot_reclassify_existing_factual_history(self):
        path = self._legacy(4)
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE claims SET claim_type='EDITORIAL_HYPOTHESIS'")
            db.rollback()
            db.execute("UPDATE claims SET deleted_at='later'")
            db.execute(
                "UPDATE asset_files SET storage_key='storage/originals/a/relocated.txt'"
            )
            db.commit()
            self.assertEqual(
                db.execute("SELECT claim_type,deleted_at FROM claims").fetchone(),
                ("FACTUAL", "later"),
            )

    def test_old_replace_counterexample_is_preserved_but_cannot_be_repeated(self):
        path = self._legacy(4)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA recursive_triggers=OFF")
            row = list(db.execute("SELECT * FROM review_events").fetchone())
            row[4] = "REJECT"
            db.execute(
                "INSERT OR REPLACE INTO review_events VALUES(?,?,?,?,?,?,?,?)", row
            )
            db.commit()
            self.assertEqual(
                db.execute("SELECT action FROM review_events").fetchone()[0], "REJECT"
            )
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            row[4] = "ACCEPT"
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT OR REPLACE INTO review_events VALUES(?,?,?,?,?,?,?,?)", row
                )
            db.rollback()
            self.assertEqual(
                db.execute("SELECT action FROM review_events").fetchone()[0], "REJECT"
            )

    def test_failed_schema5_migration_keeps_schema4_and_openable_backup(self):
        path = self._legacy(4)
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TRIGGER evidence_identity_no_replace BEFORE INSERT ON evidences BEGIN SELECT 1; END"
            )
            db.commit()
            before = self._snapshot(db)
        with self.assertRaises(sqlite3.OperationalError):
            initialize(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=4)
            self.assertEqual(self._snapshot(db), before)
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name='package_no_replace'"
                ).fetchone()
            )
        backups = list(path.parent.glob(f"{path.name}.pre-v{SCHEMA_VERSION}-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            backup.execute("PRAGMA foreign_keys=ON")
            validate(backup, expected_version=4)
            self.assertEqual(self._snapshot(backup), before)
