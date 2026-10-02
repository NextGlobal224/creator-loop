"""Exercise persistent Project references and transaction ownership."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.claims import EvidenceLink, append_claim_version, create_claim
from creator_loop.database import _connect_write, initialize, open_readonly
from creator_loop.projects import (
    add_project_reference,
    archive_project,
    create_project,
    list_project_references,
    list_projects,
)

T = "2026-10-02T00:00:00Z"


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Dữ liệu Creator" / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = _connect_write(self.path)
        self.addCleanup(self.db.close)
        self.db.execute(
            """INSERT INTO assets(asset_id,media_type,display_name,created_at)
               VALUES('asset','TEXT','Nguồn ở Huế',?)""",
            (T,),
        )
        self.db.execute(
            """INSERT INTO sources(source_id,platform,rights_status,created_at)
               VALUES('source','LOCAL','REFERENCE_ONLY',?)""",
            (T,),
        )
        self.db.execute(
            """INSERT INTO asset_files(
                 file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at)
               VALUES('file','asset','ORIGINAL','storage/originals/source.txt',?,
                      1,'text/plain',?)""",
            ("a" * 64, T),
        )
        self.db.execute(
            """INSERT INTO evidences(evidence_id,asset_id,evidence_type,created_at)
               VALUES('evidence','asset','DIRECT_TEXT',?)""",
            (T,),
        )
        self.db.execute(
            """INSERT INTO evidence_versions(
                 evidence_version_id,evidence_id,asset_id,version_no,anchor_file_id,
                 content,locator_type,locator_data,producer_type,created_by,created_at)
               VALUES('ev1','evidence','asset',1,'file','A','TEXT_RANGE',?,
                      'HUMAN','creator',?)""",
            ('{"start":0,"end":1,"text_digest":"sha256:' + "a" * 64 + '"}', T),
        )
        self.db.commit()
        self.claim = create_claim(
            self.db,
            claim_type="FACTUAL",
            statement="Nhận định đầu tiên",
            actor="editor",
            links=[EvidenceLink("ev1", "SUPPORTS")],
        )

    def _pending_caller_write(self) -> None:
        self.db.execute(
            """INSERT INTO projects(project_id,title,status,created_at)
               VALUES('caller','Pending caller','ACTIVE',?)""",
            (T,),
        )

    def test_unicode_project_persists_after_database_reopen(self) -> None:
        project = create_project(self.db, title="  Chuyện Huế 🏞  ")
        self.assertEqual(project.title, "Chuyện Huế 🏞")
        self.assertEqual((project.status, project.deleted_at), ("ACTIVE", None))
        self.assertTrue(project.created_at.endswith("Z"))
        self.assertFalse(self.db.in_transaction)
        with closing(open_readonly(self.path)) as reopened:
            self.assertEqual(list_projects(reopened), [project])
            self.assertEqual(
                list_project_references(reopened, project_id=project.project_id), []
            )

    def test_three_reference_targets_preserve_nulls_and_exact_claim_version(
        self,
    ) -> None:
        project = create_project(self.db, title="Nguồn cho bài")
        references = [
            add_project_reference(
                self.db,
                project_id=project.project_id,
                asset_id="asset",
                usage_intent="RESEARCH",
            ),
            add_project_reference(
                self.db,
                project_id=project.project_id,
                claim_version_id=self.claim.claim_version_id,
                usage_intent="QUOTE",
            ),
            add_project_reference(
                self.db,
                project_id=project.project_id,
                source_id="source",
                usage_intent="REUSE_MEDIA",
            ),
        ]
        second = append_claim_version(
            self.db,
            claim_id=self.claim.claim_id,
            statement="Nhận định đã sửa",
            actor="editor",
            links=[EvidenceLink("ev1", "CONTEXT")],
        )
        self.assertNotEqual(second.claim_version_id, references[1].claim_version_id)
        with closing(open_readonly(self.path)) as reopened:
            self.assertEqual(
                list_project_references(reopened, project_id=project.project_id),
                references,
            )
            rows = reopened.execute(
                """SELECT asset_id,claim_version_id,source_id,usage_intent
                   FROM project_references WHERE project_id=? ORDER BY rowid""",
                (project.project_id,),
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("asset", None, None, "RESEARCH"),
                    (None, self.claim.claim_version_id, None, "QUOTE"),
                    (None, None, "source", "REUSE_MEDIA"),
                ],
            )
            self.assertEqual(
                reopened.execute(
                    "SELECT rights_status FROM sources WHERE source_id='source'"
                ).fetchone()[0],
                "REFERENCE_ONLY",
            )
            self.assertEqual(
                reopened.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
            )

    def test_archiving_is_idempotent_and_retains_readable_history(self) -> None:
        project = create_project(self.db, title="Lịch sử")
        reference = add_project_reference(
            self.db,
            project_id=project.project_id,
            asset_id="asset",
            usage_intent="RESEARCH",
        )
        archived = archive_project(self.db, project_id=project.project_id)
        self.assertEqual(archived.status, "ARCHIVED")
        self.assertEqual(
            archive_project(self.db, project_id=project.project_id), archived
        )
        self.assertEqual(list_projects(self.db), [])
        self.assertEqual(list_projects(self.db, include_archived=True), [archived])
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), [reference]
        )
        with self.assertRaisesRegex(ValueError, "Archived"):
            add_project_reference(
                self.db,
                project_id=project.project_id,
                source_id="source",
                usage_intent="RESEARCH",
            )
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), [reference]
        )

    def test_invalid_title_target_count_and_usage_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "title"):
            create_project(self.db, title=" \n ")
        project = create_project(self.db, title="Validation")
        invalid_targets = [
            {},
            {"asset_id": "asset", "source_id": "source"},
            {"asset_id": "asset", "claim_version_id": self.claim.claim_version_id},
            {"claim_version_id": self.claim.claim_version_id, "source_id": "source"},
            {
                "asset_id": "asset",
                "claim_version_id": self.claim.claim_version_id,
                "source_id": "source",
            },
            {"asset_id": " "},
        ]
        for targets in invalid_targets:
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                add_project_reference(
                    self.db,
                    project_id=project.project_id,
                    usage_intent="RESEARCH",
                    **targets,
                )
        for intent in ("", "PUBLISH", None):
            with (
                self.subTest(intent=intent),
                self.assertRaisesRegex(ValueError, "intent"),
            ):
                add_project_reference(
                    self.db,
                    project_id=project.project_id,
                    asset_id="asset",
                    usage_intent=intent,
                )
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), []
        )

    def test_missing_deleted_and_unsealed_targets_are_rejected(self) -> None:
        project = create_project(self.db, title="Targets")
        for target_name in ("asset_id", "claim_version_id", "source_id"):
            with self.subTest(target=target_name), self.assertRaises(ValueError):
                add_project_reference(
                    self.db,
                    project_id=project.project_id,
                    usage_intent="RESEARCH",
                    **{target_name: "missing"},
                )
        self.db.execute("UPDATE assets SET deleted_at=? WHERE asset_id='asset'", (T,))
        self.db.execute(
            "UPDATE claims SET deleted_at=? WHERE claim_id=?", (T, self.claim.claim_id)
        )
        self.db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES('unsealed','FACTUAL',?)",
            (T,),
        )
        self.db.execute(
            """INSERT INTO claim_versions(
                 claim_version_id,claim_id,version_no,statement,created_at)
               VALUES('unsealed-version','unsealed',1,'Unfinished',?)""",
            (T,),
        )
        self.db.commit()
        for targets in (
            {"asset_id": "asset"},
            {"claim_version_id": self.claim.claim_version_id},
            {"claim_version_id": "unsealed-version"},
        ):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                add_project_reference(
                    self.db,
                    project_id=project.project_id,
                    usage_intent="RESEARCH",
                    **targets,
                )
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), []
        )
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_deleted_and_missing_projects_cannot_be_written(self) -> None:
        project = create_project(self.db, title="Deleted")
        reference = add_project_reference(
            self.db,
            project_id=project.project_id,
            source_id="source",
            usage_intent="QUOTE",
        )
        self.db.execute(
            "UPDATE projects SET deleted_at=? WHERE project_id=?",
            (T, project.project_id),
        )
        self.db.commit()
        for project_id in (project.project_id, "missing"):
            with self.subTest(project_id=project_id):
                with self.assertRaisesRegex(ValueError, "Live Project"):
                    archive_project(self.db, project_id=project_id)
                with self.assertRaisesRegex(ValueError, "Live Project"):
                    add_project_reference(
                        self.db,
                        project_id=project_id,
                        asset_id="asset",
                        usage_intent="RESEARCH",
                    )
        self.assertEqual(list_projects(self.db, include_archived=True), [])
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), [reference]
        )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            list_project_references(self.db, project_id="missing")

    def test_success_preserves_caller_transaction_until_commit(self) -> None:
        self._pending_caller_write()
        project = create_project(self.db, title="Caller scope")
        reference = add_project_reference(
            self.db,
            project_id=project.project_id,
            asset_id="asset",
            usage_intent="RESEARCH",
        )
        archived = archive_project(self.db, project_id=project.project_id)
        self.assertTrue(self.db.in_transaction)
        with closing(open_readonly(self.path)) as other:
            self.assertEqual(list_projects(other, include_archived=True), [])
            self.assertEqual(
                other.execute("SELECT count(*) FROM project_references").fetchone()[0],
                0,
            )
        self.db.commit()
        with closing(open_readonly(self.path)) as other:
            projects = list_projects(other, include_archived=True)
            self.assertEqual(
                {p.project_id for p in projects}, {"caller", project.project_id}
            )
            self.assertIn(archived, projects)
            self.assertEqual(
                list_project_references(other, project_id=project.project_id),
                [reference],
            )

    def test_injected_failure_and_validation_preserve_caller_writes(self) -> None:
        project = create_project(self.db, title="Failure scope")
        self.db.execute(
            """CREATE TEMP TRIGGER reject_reference AFTER INSERT ON project_references
               BEGIN
                 UPDATE projects SET title='Changed by failed trigger'
                 WHERE project_id=NEW.project_id;
                 SELECT RAISE(FAIL,'injected reference failure');
               END"""
        )
        self._pending_caller_write()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected reference failure"
        ):
            add_project_reference(
                self.db,
                project_id=project.project_id,
                source_id="source",
                usage_intent="RESEARCH",
            )
        with self.assertRaisesRegex(ValueError, "Live Asset"):
            add_project_reference(
                self.db,
                project_id=project.project_id,
                asset_id="missing",
                usage_intent="RESEARCH",
            )
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM projects WHERE project_id='caller'"
            ).fetchone()[0],
            "Pending caller",
        )
        self.assertEqual(
            list_project_references(self.db, project_id=project.project_id), []
        )
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM projects WHERE project_id=?", (project.project_id,)
            ).fetchone()[0],
            "Failure scope",
        )
        with closing(open_readonly(self.path)) as other:
            self.assertIsNone(
                other.execute(
                    "SELECT 1 FROM projects WHERE project_id='caller'"
                ).fetchone()
            )
        self.db.rollback()
        self.assertIsNone(
            self.db.execute(
                "SELECT 1 FROM projects WHERE project_id='caller'"
            ).fetchone()
        )

    def test_project_insert_and_archive_failure_leave_no_partial_changes(self) -> None:
        project = create_project(self.db, title="Original")
        self.db.execute(
            """CREATE TEMP TRIGGER reject_project AFTER INSERT ON projects
               WHEN NEW.title='Rejected'
               BEGIN SELECT RAISE(FAIL,'injected project failure'); END"""
        )
        self.db.execute(
            """CREATE TEMP TRIGGER reject_archive AFTER UPDATE OF status ON projects
               BEGIN SELECT RAISE(FAIL,'injected archive failure'); END"""
        )
        self._pending_caller_write()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "project failure"):
            create_project(self.db, title="Rejected")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "archive failure"):
            archive_project(self.db, project_id=project.project_id)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM projects WHERE project_id=?", (project.project_id,)
            ).fetchone()[0],
            "ACTIVE",
        )
        self.db.commit()
        with closing(open_readonly(self.path)) as other:
            self.assertEqual(
                {p.title for p in list_projects(other)}, {"Original", "Pending caller"}
            )


if __name__ == "__main__":
    unittest.main()
