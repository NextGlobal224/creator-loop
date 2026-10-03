"""Validate legacy Draft upgrade, backup, immutable snapshots and failure recovery."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    backup,
    initialize,
    migration_path,
    validate,
)


class DraftMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Dữ liệu cũ" / "legacy.sqlite3"
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for migration in MIGRATIONS[:2]:
                sql = migration_path(migration).read_text(encoding="utf-8")
                db.executescript(sql)
                db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                    (
                        migration,
                        hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                        "now",
                        "0.1.0",
                    ),
                )
            db.execute("PRAGMA user_version=2")
            db.execute("INSERT INTO projects VALUES('p','Legacy','ACTIVE','now',NULL)")
            db.execute("INSERT INTO drafts VALUES('d','p','ACTIVE','now')")
            for number in (1, 2):
                db.execute(
                    "INSERT INTO draft_versions VALUES(?,?,?,'Café 🏞','POST','editor','now')",
                    (f"v{number}", "d", number),
                )
            db.execute("INSERT INTO draft_version_parents VALUES('v2','v1','EDIT')")
            db.execute(
                "INSERT INTO draft_assertions VALUES('span','v1',5,6,'🏞',NULL,'EDITORIAL')"
            )
            db.commit()

    def _old_valid(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=2)
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='draft_version_seals'"
                ).fetchone()
            )
        snapshots = list(
            self.path.parent.glob(f"legacy.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3")
        )
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=2)

    def test_upgrade_seals_legacy_snapshots_without_rewriting_and_backup_opens(self):
        with closing(sqlite3.connect(self.path)) as db:
            before = {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "draft_versions",
                    "draft_version_parents",
                    "draft_assertions",
                )
            }
        initialize(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db)
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM draft_version_seals").fetchone()[0], 2
            )
            for table, rows in before.items():
                self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), rows)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE draft_assertions SET review_state='UNREVIEWED'")
        snapshots = list(
            self.path.parent.glob(f"legacy.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3")
        )
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=2)
            self.assertEqual(
                db.execute(
                    "SELECT body_text FROM draft_versions ORDER BY version_no"
                ).fetchall(),
                [("Café 🏞",), ("Café 🏞",)],
            )
        initialize(self.path)
        self.assertEqual(
            len(
                list(
                    self.path.parent.glob(
                        f"legacy.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3"
                    )
                )
            ),
            1,
        )

    def test_invalid_historical_offsets_and_cycles_roll_back_and_keep_backup(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE draft_assertions SET asserted_text='wrong'")
            db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid draft snapshot"):
            initialize(self.path)
        self._old_valid()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE draft_assertions SET asserted_text='🏞'")
            db.execute("INSERT INTO draft_version_parents VALUES('v1','v2','COMBINE')")
            db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid draft snapshot"):
            initialize(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=2)
            self.assertEqual(
                db.execute("SELECT count(*) FROM draft_version_parents").fetchone()[0],
                2,
            )

    def test_cross_project_parent_history_is_not_silently_sealed(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO projects VALUES('other','Other','ACTIVE','now',NULL)"
            )
            db.execute("INSERT INTO drafts VALUES('foreign','other','ACTIVE','now')")
            db.execute(
                "INSERT INTO draft_versions VALUES('foreign-v','foreign',1,'body','POST','editor','now')"
            )
            db.execute(
                "INSERT INTO draft_version_parents VALUES('v1','foreign-v','COMBINE')"
            )
            db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid draft snapshot"):
            initialize(self.path)
        self._old_valid()

    def test_schema_one_draft_history_upgrades_through_both_steps(self):
        path = self.path.with_name("schema-one.sqlite3")
        sql = migration_path(MIGRATIONS[0]).read_text(encoding="utf-8")
        with closing(sqlite3.connect(path)) as db:
            db.executescript(sql)
            db.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                (
                    MIGRATIONS[0],
                    hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                    "now",
                    "0.1.0",
                ),
            )
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO projects VALUES('p','Legacy','ACTIVE','now',NULL)")
            db.execute("INSERT INTO drafts VALUES('d','p','ACTIVE','now')")
            db.execute(
                "INSERT INTO draft_versions VALUES('v','d',1,'Café 🏞','POST','editor','original-time')"
            )
            db.execute(
                "INSERT INTO draft_assertions VALUES('a','v',5,6,'🏞',NULL,'EDITORIAL')"
            )
            db.commit()
        initialize(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db)
            self.assertEqual(
                db.execute(
                    "SELECT body_text,created_at FROM draft_versions"
                ).fetchone(),
                ("Café 🏞", "original-time"),
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM draft_version_seals").fetchone()[0], 1
            )
        snapshots = list(
            path.parent.glob(f"schema-one.sqlite3.pre-v{SCHEMA_VERSION}-*.sqlite3")
        )
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=1)

    def test_backup_race_aborts_before_migration_and_keeps_original_schema(self):
        def backup_then_change(source, destination, *, expected_version):
            backup(source, destination, expected_version=expected_version)
            with closing(sqlite3.connect(source)) as db:
                db.execute(
                    "INSERT INTO projects VALUES('racer','Concurrent','ACTIVE','now',NULL)"
                )
                db.commit()

        with (
            patch("creator_loop.database.backup", backup_then_change),
            self.assertRaisesRegex(RuntimeError, "Database changed"),
        ):
            initialize(self.path)
        self._old_valid()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNotNone(
                db.execute("SELECT 1 FROM projects WHERE project_id='racer'").fetchone()
            )
