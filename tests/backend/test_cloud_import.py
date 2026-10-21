"""Staging merge integrity; no cloud transfer, AI, publishing or media proof."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.creator.drafts import DraftChoices
from creator_loop.backend.runtime.cloud_import import B_TABLES, append_preserved_b_rows
from creator_loop.backend.runtime.database import initialize, validate

from tests.backend import test_drafts


class CloudImportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_drafts.DraftTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.version = self.fixture._create(
            format="IMAGE_CAPTION", choices=DraftChoices("Giữ nguyên hướng đã chọn")
        )
        self.fixture.db.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "cloud staging.sqlite3"
        initialize(path)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.db.execute(
            "INSERT INTO projects VALUES('cloud-existing','Dữ liệu cloud mới hơn','ACTIVE','now',NULL)"
        )
        self.db.commit()

    def payload(self):
        tables = {}
        for table in B_TABLES:
            cursor = self.fixture.db.execute('SELECT * FROM "' + table + '"')
            names = [c[0] for c in cursor.description]
            rows = [dict(zip(names, r)) for r in cursor]
            if rows:
                tables[table] = rows
        return tables

    def assert_target_unchanged(self):
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM assets").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM projects").fetchone()[0], 1
        )
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA defer_foreign_keys").fetchone()[0], 0)
        validate(self.db)

    def test_exact_history_choices_and_idempotent_replay_preserve_target(self):
        tables = self.payload()
        self.db.execute("BEGIN IMMEDIATE")
        result = append_preserved_b_rows(self.db, tables)
        self.assertEqual(result["inserted"], sum(map(len, tables.values())))
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        self.assertEqual(
            append_preserved_b_rows(self.db, tables),
            {"inserted": 0, "identical": result["inserted"]},
        )
        self.db.commit()
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM projects WHERE project_id='cloud-existing'"
            ).fetchone()[0],
            "Dữ liệu cloud mới hơn",
        )
        choice = self.db.execute(
            "SELECT direction_text FROM creator_draft_choices"
        ).fetchone()
        self.assertEqual(choice[0], "Giữ nguyên hướng đã chọn")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE draft_versions SET body_text='overwritten'")
        self.db.rollback()
        validate(self.db)

    def test_conflict_refuses_whole_batch_even_if_caller_catches_and_commits(self):
        tables = self.payload()
        row = {**tables["projects"][0], "title": "Cloud changed this identity"}
        self.db.execute(
            "INSERT INTO projects VALUES(?,?,?,?,?)",
            tuple(
                row[k]
                for k in ("project_id", "title", "status", "created_at", "deleted_at")
            ),
        )
        self.db.commit()
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "never overwrite"):
            append_preserved_b_rows(self.db, tables)
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM assets").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM projects").fetchone()[0], 2
        )

    def test_missing_dependency_and_invalid_seal_roll_back_partial_inserts(self):
        for damage in ("dependency", "snapshot"):
            with self.subTest(damage=damage):
                tables = self.payload()
                if damage == "dependency":
                    del tables["assets"]
                else:
                    tables["draft_assertions"][0]["asserted_text"] += " bị thay đổi"
                self.db.execute("BEGIN")
                with self.assertRaises((RuntimeError, ValueError)):
                    append_preserved_b_rows(self.db, tables)
                self.db.commit()
                self.assert_target_unchanged()

    def test_unknown_tables_duplicate_identity_and_invalid_values_refuse(self):
        original = self.payload()
        for damage in ("approval", "duplicate", "bool", "nonfinite", "columns"):
            with self.subTest(damage=damage):
                tables = copy.deepcopy(original)
                if damage == "approval":
                    tables["approvals"] = []
                elif damage == "duplicate":
                    tables["assets"].append(copy.deepcopy(tables["assets"][0]))
                elif damage == "bool":
                    tables["assets"][0]["display_name"] = True
                elif damage == "nonfinite":
                    tables["assets"][0]["display_name"] = float("nan")
                else:
                    tables["assets"][0]["extra_column"] = "unexpected"
                self.db.execute("BEGIN")
                with self.assertRaises(ValueError):
                    append_preserved_b_rows(self.db, tables)
                self.db.commit()
                self.assert_target_unchanged()

    def test_active_processing_and_implicit_transaction_refuse(self):
        with self.assertRaisesRegex(ValueError, "explicit transaction"):
            append_preserved_b_rows(self.db, self.payload())
        columns = [r[1] for r in self.db.execute("PRAGMA table_info(processing_runs)")]
        row = dict.fromkeys(columns)
        row.update(run_id="r" * 32, status="RUNNING")
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "Active historical"):
            append_preserved_b_rows(self.db, {"processing_runs": [row]})
        self.db.commit()
        self.assert_target_unchanged()

    def test_historical_render_seal_is_reconstructed_without_executing_it(self):
        db = self.fixture.db
        run = "c" * 32
        db.execute(
            "INSERT INTO processing_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run,
                "a",
                "f",
                "DRAFT_VIDEO_RENDER",
                "SUCCEEDED",
                "fixture",
                "1",
                None,
                None,
                "now",
                "now",
                None,
                None,
                "now",
            ),
        )
        db.execute(
            "INSERT INTO draft_render_requests VALUES(?,?,?,? ,NULL)",
            (run, self.version.draft_version_id, "{}", "d" * 64),
        )
        db.execute(
            "INSERT INTO draft_render_inputs VALUES(?,0,'f',?,'SCRIPT')",
            (run, "0" * 64),
        )
        db.execute(
            "UPDATE draft_render_requests SET sealed_at='historical' WHERE render_run_id=?",
            (run,),
        )
        db.execute(
            "INSERT INTO draft_render_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run,
                "storage/renders/fixture.mp4",
                "e" * 64,
                1,
                "video/mp4",
                1,
                1,
                1,
                "fixture",
                "1",
                "now",
            ),
        )
        db.commit()
        self.db.execute("BEGIN")
        append_preserved_b_rows(self.db, self.payload())
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT sealed_at FROM draft_render_requests").fetchone()[
                0
            ],
            "historical",
        )
        self.db.execute("BEGIN")
        self.assertEqual(
            append_preserved_b_rows(self.db, self.payload())["inserted"], 0
        )
        self.db.rollback()
        validate(self.db)
