"""Upgrade supported legacy schemas without rewriting or repairing decisions."""

from __future__ import annotations

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
from creator_loop.selections import list_selections


class SelectionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Lịch sử lựa chọn"
        self.root.mkdir()

    def _legacy(self, version, suffix=""):
        path = self.root / f"legacy-{version}{suffix}.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for migration in MIGRATIONS[:version]:
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
            db.execute(f"PRAGMA user_version={version}")
            db.execute(
                "INSERT INTO projects VALUES('p','Historical','ARCHIVED','now',NULL)"
            )
            db.execute("INSERT INTO drafts VALUES('d','p','ARCHIVED','now')")
            for number in (1, 2):
                db.execute(
                    "INSERT INTO draft_versions VALUES(?,?,?,'Café 🏞','POST','editor','original-time')",
                    (f"v{number}", "d", number),
                )
                if version >= 3:
                    db.execute(
                        "INSERT INTO draft_version_seals VALUES(?,'now')",
                        (f"v{number}",),
                    )
            db.execute(
                "INSERT INTO selection_events VALUES('choice','p','v2','old-set',NULL,'old-editor','old-time')"
            )
            db.executemany(
                "INSERT INTO selection_candidates VALUES('choice',?)",
                [("v1",), ("v2",)],
            )
            db.commit()
        return path

    def test_all_supported_schemas_upgrade_preserve_history_and_readable_backup(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                path = self._legacy(version)
                with closing(sqlite3.connect(path)) as old:
                    before = {
                        t: old.execute(f"SELECT * FROM {t}").fetchall()
                        for t in (
                            "selection_events",
                            "selection_candidates",
                            "draft_versions",
                        )
                    }
                initialize(path)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    validate(db)
                    self.assertEqual(
                        db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
                    )
                    for table, rows in before.items():
                        self.assertEqual(
                            db.execute(f"SELECT * FROM {table}").fetchall(), rows
                        )
                    decision = list_selections(db, project_id="p")[0]
                    self.assertEqual(
                        (decision.actor_id, decision.reason_text, decision.created_at),
                        ("old-editor", None, "old-time"),
                    )
                    self.assertEqual(decision.candidate_version_ids, ("v1", "v2"))
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("UPDATE selection_events SET reason_text='rewrite'")
                backups = list(
                    path.parent.glob(f"{path.name}.pre-v{SCHEMA_VERSION}-*.sqlite3")
                )
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as snapshot:
                    snapshot.execute("PRAGMA foreign_keys=ON")
                    validate(snapshot, expected_version=version)
                    self.assertEqual(
                        snapshot.execute("SELECT * FROM selection_events").fetchall(),
                        before["selection_events"],
                    )
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

    def test_invalid_membership_project_or_unsealed_draft_keeps_schema_and_backup(self):
        for invalid in ("membership", "project", "unsealed"):
            with self.subTest(invalid=invalid):
                path = self._legacy(3, invalid)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    if invalid == "membership":
                        db.execute(
                            "DELETE FROM selection_candidates WHERE draft_version_id='v2'"
                        )
                    elif invalid == "project":
                        db.execute(
                            "INSERT INTO projects VALUES('other','Other','ACTIVE','now',NULL)"
                        )
                        db.execute("UPDATE selection_events SET project_id='other'")
                    else:
                        db.execute(
                            "INSERT INTO draft_versions VALUES('raw','d',3,'body','POST','editor','now')"
                        )
                        db.execute(
                            "INSERT INTO selection_candidates VALUES('choice','raw')"
                        )
                    db.commit()
                    before = db.execute("SELECT * FROM selection_events").fetchall()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "invalid selection"
                ):
                    initialize(path)
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    validate(db, expected_version=3)
                    self.assertEqual(
                        db.execute("SELECT * FROM selection_events").fetchall(), before
                    )
                    self.assertIsNone(
                        db.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='selection_event_seals'"
                        ).fetchone()
                    )
                backups = list(
                    path.parent.glob(f"{path.name}.pre-v{SCHEMA_VERSION}-*.sqlite3")
                )
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    validate(db, expected_version=3)


if __name__ == "__main__":
    unittest.main()
