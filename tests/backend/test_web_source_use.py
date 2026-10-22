"""Protocol fixtures; these do not establish real audio/video/text quality."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.creator.web_domain import WebDomain, request_from_json
from creator_loop.backend.knowledge.source_use import permitted_source_versions
from creator_loop.backend.runtime import database
from creator_loop.backend.runtime.database import _connect_write, initialize, validate


class WebSourceUseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize(self.root / "creator_loop.sqlite3")
        self.domain = WebDomain(self.root)
        self.a = self.domain.project("Nguồn A")["project_id"]
        self.b = self.domain.project("Nguồn B")["project_id"]
        self.text = (
            "Mở đầu 🧭.\nĐây là đoạn tiếng Việt nguyên văn.\nĐoạn riêng chưa chọn."
        )
        self.file = self.domain.intake(
            {
                "project_id": self.a,
                "media_type": "TEXT",
                "display_name": "Nguồn có dấu.txt",
            },
            self.text.encode(),
        )["file_id"]
        self.start = self.text.index("Đây")
        self.end = self.text.index("\nĐoạn")
        self.version = self.domain.source_text_range(
            {
                "project_id": self.a,
                "file_id": self.file,
                "start": self.start,
                "end": self.end,
            }
        )["evidence_version_id"]

    def choose(self, **changes):
        data = {
            "project_id": self.a,
            "file_id": self.file,
            "state": "SELECTED",
            "evidence_version_ids": [self.version],
            "expected_parent": None,
            "explicit_source_use_choice": True,
        }
        return self.domain.source_use(data | changes)

    def permitted(self, project=None):
        with closing(_connect_write(self.domain.path)) as db:
            return permitted_source_versions(
                db, data_root=self.root, project_id=project or self.a, file_id=self.file
            )

    def test_explicit_range_choice_persists_without_factual_or_publication_accept(self):
        with self.assertRaisesRegex(ValueError, "xác nhận"):
            self.permitted()
        with self.assertRaisesRegex(ValueError, "Xác nhận"):
            self.choose(explicit_source_use_choice=False)
        selected = self.choose()
        self.assertFalse(selected["factual_approval"])
        self.assertFalse(selected["publication_approval"])
        self.assertEqual(
            self.permitted(), ((self.version, self.text[self.start : self.end]),)
        )
        reopened = WebDomain(self.root).source_review(self.a, self.file)
        self.assertEqual(reopened["text"], self.text)
        self.assertEqual(
            reopened["source_use"]["use_version_id"], selected["use_version_id"]
        )
        ev = reopened["evidence_versions"][0]
        self.assertEqual(ev["review_state"], "PENDING")
        self.assertIn(str(self.start), ev["locator_data"])
        with closing(_connect_write(self.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            validate(db)

    def test_shared_original_does_not_share_project_permission(self):
        self.choose()
        with self.assertRaisesRegex(ValueError, "chưa liên kết"):
            self.domain.source_review(self.b, self.file)
        material = self.domain.state(self.a)["materials"][0]["material_id"]
        self.domain.link_material(self.b, material)
        self.assertIsNone(self.domain.source_review(self.b, self.file)["source_use"])
        with self.assertRaises(ValueError):
            self.permitted(self.b)
        self.assertEqual(len(self.permitted()), 1)

    def test_real_web_prepare_uses_only_explicitly_selected_exact_passage(self):
        mid = self.domain.state(self.a)["materials"][0]["material_id"]
        request = request_from_json(
            {
                "project_id": self.a,
                "mode": "MANUAL",
                "purpose": "Đọc nguồn",
                "direction": "Chỉ phần đã chọn",
                "audience": "Fixture",
                "style": "Rõ",
                "output_format": "TEXT",
                "material_ids": [mid],
            }
        )
        with self.assertRaises(ValueError):
            self.domain.prepare(request)
        self.choose()
        material = self.domain.prepare(request)[0]
        self.assertEqual(material.text, self.text[self.start : self.end])
        self.assertNotIn("Đoạn riêng", material.text)
        self.assertEqual(material.evidence_version_ids, (self.version,))

    def test_exclusion_and_corrected_ranges_invalidate_old_selection_keep_history(self):
        selected = self.choose()
        excluded = self.choose(
            state="EXCLUDED",
            evidence_version_ids=[],
            expected_parent=selected["use_version_id"],
        )
        with self.assertRaises(ValueError):
            self.permitted()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.choose(expected_parent=selected["use_version_id"])
        self.choose(expected_parent=excluded["use_version_id"])
        self.assertTrue(self.domain.state(self.a)["materials"][0]["usable_for_draft"])
        corrected = self.domain.source_text_range(
            {
                "project_id": self.a,
                "file_id": self.file,
                "start": 0,
                "end": self.end,
                "evidence_version_id": self.version,
                "reason": "Chọn thêm mở đầu",
            }
        )
        self.assertNotEqual(corrected["evidence_version_id"], self.version)
        with self.assertRaisesRegex(ValueError, "phiên bản hiện tại"):
            self.permitted()
        material = self.domain.state(self.a)["materials"][0]
        self.assertFalse(material["usable_for_draft"])
        self.assertIn("phiên bản hiện tại", material["source_use_message"])
        reopened = self.domain.source_review(self.a, self.file)
        self.assertEqual(len(reopened["evidence_versions"]), 2)
        self.assertEqual(
            reopened["evidence_versions"][0]["content"],
            self.text[self.start : self.end],
        )

    def test_immutable_selection_snapshot_and_anchor_fks(self):
        selected = self.choose()
        with closing(_connect_write(self.domain.path)) as db:
            for statement in [
                "UPDATE project_material_use_versions SET state='EXCLUDED'",
                "DELETE FROM project_material_use_items",
                "DELETE FROM project_material_use_seals",
            ]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
                db.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT OR REPLACE INTO project_material_use_items VALUES(?,?,?)",
                    (selected["use_version_id"], 0, self.version),
                )
            db.rollback()
            validate(db)

    def test_schema10_is_readable_and_additive_migration_keeps_original_no_auto_permission(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_validate = database.validate
            with (
                patch.object(database, "MIGRATIONS", database.MIGRATIONS[:10]),
                patch.object(database, "SCHEMA_VERSION", 10),
                patch.object(
                    database,
                    "validate",
                    side_effect=lambda db: original_validate(db, expected_version=10),
                ),
            ):
                initialize(root / "creator_loop.sqlite3")
                domain = WebDomain(root)
                project = domain.project("Giữ dữ liệu schema10")["project_id"]
                original = domain.intake(
                    {
                        "project_id": project,
                        "media_type": "TEXT",
                        "display_name": "Nguồn cũ",
                    },
                    "Nội dung cũ được giữ nguyên.".encode(),
                )
            self.assertIsNone(
                WebDomain(root).source_review(project, original["file_id"])[
                    "source_use"
                ]
            )
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                before = db.serialize()
                rows = db.execute("SELECT * FROM asset_files").fetchall()
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    database._migrate_locked(db, 10)
                validate(db)
                self.assertEqual(
                    db.execute("SELECT * FROM asset_files").fetchall(), rows
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM project_material_use_versions"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
                )
            # The actual preimage is separately restorable, not a renamed database.
            restored = root / "isolated-preimage.sqlite3"
            restored.write_bytes(before)
            with closing(_connect_write(restored)) as db:
                validate(db, expected_version=10)
                self.assertEqual(
                    db.execute("SELECT * FROM asset_files").fetchall(), rows
                )


if __name__ == "__main__":
    unittest.main()
