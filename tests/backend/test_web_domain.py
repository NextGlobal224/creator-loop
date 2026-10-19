"""Cloud adapter domain regressions; these are fixtures, never AI quality proof."""

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from creator_loop.backend.creator.web_domain import WebDomain, request_from_json
from creator_loop.backend.runtime.database import _connect_write, initialize, validate


class WebDomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initialize(self.root / "creator_loop.sqlite3")
        self.domain = WebDomain(self.root)
        self.project = self.domain.project("Project cô lập")
        self.source = (
            "Bản ghi thử nghiệm có hai dòng.\nĐây là nội dung nguồn nguyên văn."
        )
        self.material = self.domain.intake(
            {
                "project_id": self.project["project_id"],
                "media_type": "TEXT",
                "display_name": "Nguồn thử",
                "source": {
                    "url": "https://example.org/fixture",
                    "rights": "REFERENCE_ONLY",
                },
            },
            self.source.encode(),
        )
        self.selected = self.domain.pipeline.materials()[0]
        self.request = request_from_json(
            {
                "project_id": self.project["project_id"],
                "mode": "AUTOMATIC",
                "purpose": "Kiểm cấu trúc lưu nguồn",
                "audience": "Fixture",
                "style": "Rõ",
                "output_format": "TEXT",
                "material_ids": [self.selected.material_id],
            }
        )

    def test_project_material_links_are_explicit_many_to_many_and_enforced(self):
        a = self.project["project_id"]
        b = self.domain.project("Project B trống")["project_id"]
        self.assertEqual(len(self.domain.state(a)["materials"]), 1)
        self.assertEqual(self.domain.state(b)["materials"], [])
        self.assertEqual(self.domain.state(b)["workflows"], [])
        self.assertEqual(len(self.domain.state(b)["library_materials"]), 1)
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "chưa liên kết"):
            self.domain.prepare(replace(self.request, project_id=b))
        self.domain.link_material(b, self.selected.material_id)
        self.domain.link_material(b, self.selected.material_id)
        self.assertEqual(len(self.domain.state(b)["materials"]), 1)
        self.assertEqual(len(self.domain.state(a)["materials"]), 1)
        self.assertEqual(
            len(self.domain.prepare(replace(self.request, project_id=b))), 1
        )
        with closing(_connect_write(self.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM project_references").fetchone()[0], 2
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
            validate(db)

    def test_image_intake_is_project_scoped_and_original_bytes_unchanged(self):
        a = self.project["project_id"]
        b = self.domain.project("Project B ảnh")["project_id"]
        raw = b"\x89PNG\r\n\x1a\nfixture-format-only"
        result = self.domain.intake(
            {
                "project_id": a,
                "media_type": "IMAGE",
                "display_name": "Ảnh có dấu và khoảng trắng.png",
            },
            raw,
        )
        self.domain.require_project_image(a, result["file_id"])
        with self.assertRaisesRegex(ValueError, "chưa liên kết"):
            self.domain.require_project_image(b, result["file_id"])
        self.assertEqual((self.root / result["storage_key"]).read_bytes(), raw)
        self.assertEqual(self.domain.state(b)["images"], [])

    def test_text_file_validation_has_safe_specific_errors(self):
        from creator_loop.backend.creator.web_domain import WebInputError

        with self.assertRaisesRegex(WebInputError, "UTF-8"):
            self.domain.intake(
                {"media_type": "TEXT", "display_name": "wrong"}, b"\xffbad"
            )
        with self.assertRaisesRegex(WebInputError, "PNG"):
            self.domain.intake(
                {"media_type": "IMAGE", "display_name": "wrong"}, b"<svg/>"
            )

    def fixture(self):
        fact = "Đây là nội dung nguồn nguyên văn."
        body = fact + "\n\n" + " ".join(f"từthử{i}" for i in range(110))
        return {
            "observations": [
                {
                    "material_id": self.selected.material_id,
                    "quote": fact,
                    "observation": "Nguồn chứa dòng thử nghiệm.",
                }
            ],
            "inferences": ["Giả thuyết chưa xác nhận: đây chỉ là fixture."],
            "direction": "Hướng thử nghiệm riêng",
            "draft_body": body,
            "factual_claims": [
                {
                    "text": fact,
                    "material_ids": [self.selected.material_id],
                    "source_quote": fact,
                }
            ],
        }

    def test_original_source_and_reload_are_exact(self):
        current = WebDomain(self.root).pipeline.materials()[0]
        self.assertEqual(current.source_id, self.material["source_id"])
        self.assertEqual(current.source_url, "https://example.org/fixture")
        self.assertEqual(
            (self.root / self.material["storage_key"]).read_bytes(),
            self.source.encode(),
        )
        with closing(_connect_write(self.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT rights_status FROM sources").fetchone()[0],
                "REFERENCE_ONLY",
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            validate(db)

    def test_native_analyze_never_runs_and_manual_requires_direction(self):
        with self.assertRaises(RuntimeError):
            self.domain.pipeline.analyze(self.request)
        value = copy.deepcopy(self.request.__dict__)
        value.pop("actor")
        value["material_ids"] = list(value["material_ids"])
        value["mode"] = "MANUAL"
        with self.assertRaises(ValueError):
            request_from_json(value)

    def test_unsupported_quote_cannot_commit_workflow(self):
        result = self.fixture()
        result["factual_claims"][0]["source_quote"] = "Không tồn tại trong nguồn"
        with self.assertRaises(ValueError):
            self.domain.finish(
                self.request,
                result,
                {"version": "fixture", "run_id": uuid4().hex},
                "fixture",
            )
        with closing(_connect_write(self.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM creator_workflows").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0], 0
            )

    def test_sealed_draft_and_edit_keep_exact_versions_without_acceptance(self):
        run = uuid4().hex
        self.domain.begin_run(self.request, run, "fixture", "fixture-model")
        with closing(_connect_write(self.domain.path)) as db:
            db.execute(
                "UPDATE processing_runs SET status='RUNNING',started_at='2026-10-18T00:00:00Z' WHERE run_id=?",
                (run,),
            )
            db.commit()
        workflow = self.domain.finish(
            self.request,
            self.fixture(),
            {"version": "fixture", "backend": "FIXTURE_ONLY", "run_id": run},
            "fixture",
        )
        first = self.domain.draft({"workflow_id": workflow})
        second = self.domain.draft(
            {
                "workflow_id": workflow,
                "body_text": first["body_text"] + "\n\nChỉnh sửa fixture.",
            }
        )
        self.assertEqual(second["version_no"], 2)
        reopened = WebDomain(self.root).pipeline
        self.assertEqual(
            reopened.draft(first["draft_version_id"]).body_text, first["body_text"]
        )
        self.assertEqual(
            reopened.draft(second["draft_version_id"]).body_text, second["body_text"]
        )
        with closing(_connect_write(self.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE draft_versions SET body_text='changed' WHERE draft_version_id=?",
                    (first["draft_version_id"],),
                )
            db.rollback()
            validate(db)
