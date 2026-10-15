from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creator_loop.backend.analysis.creator_llm import validate_response
from creator_loop.backend.creator.research_pipeline import ResearchPipeline
from creator_loop.backend.runtime.database import open_readonly
from creator_loop.shared.workflow import ResearchMaterial

from tests.backend.test_research_pipeline import fixture


class LLMResponseTests(unittest.TestCase):
    def setUp(self):
        self.material = ResearchMaterial(
            "source:s",
            "s",
            "a",
            "f",
            None,
            "TEXT",
            "Niềm tin cần được kiểm chứng bằng sự thật.",
        )
        fact = "Niềm tin cần được kiểm chứng bằng sự thật."
        self.response = {
            "observations": [
                {
                    "material_id": "source:s",
                    "quote": fact,
                    "observation": "Nguồn khuyên kiểm chứng.",
                }
            ],
            "inferences": ["Giả thuyết: hướng hành động cụ thể."],
            "direction": "Kiểm tra nguồn",
            "draft_body": fact
            + "\n"
            + " ".join(
                "Lời khuyên biên tập cần được đối chiếu cẩn thận." for _ in range(12)
            ),
            "factual_claims": [
                {"text": fact, "material_ids": ["source:s"], "source_quote": fact}
            ],
        }

    def test_exact_source_quote_and_exact_body_span_are_required(self):
        validate_response(self.response, [self.material])
        for path, value in (
            ("quote", "Một câu không có trong nguồn"),
            ("text", "Một câu không có trong bài"),
        ):
            response = copy.deepcopy(self.response)
            target = (
                response["observations"][0]
                if path == "quote"
                else response["factual_claims"][0]
            )
            target[path] = value
            with self.assertRaises(ValueError):
                validate_response(response, [self.material])

    def test_untrusted_ids_and_placeholder_do_not_escape_validation(self):
        for ids in ([{}], ["unknown"], ["source:s", "source:s"]):
            response = copy.deepcopy(self.response)
            response["factual_claims"][0]["material_ids"] = ids
            with self.assertRaises(ValueError):
                validate_response(response, [self.material])
        self.response["draft_body"] += " [placeholder]"
        with self.assertRaises(ValueError):
            validate_response(self.response, [self.material])

    def test_repeated_paragraphs_are_not_complete_creative_output(self):
        self.response["draft_body"] += "\n\n" + self.response["draft_body"]
        with self.assertRaises(ValueError):
            validate_response(self.response, [self.material])

    def test_one_quote_cannot_claim_support_from_an_unrelated_second_material(self):
        other = ResearchMaterial(
            "source:t",
            "t",
            "b",
            "g",
            None,
            "TEXT",
            "Một ghi chú khác không chứa trích dẫn này.",
        )
        self.response["factual_claims"][0]["material_ids"].append("source:t")
        with self.assertRaises(ValueError):
            validate_response(self.response, [self.material, other])


@unittest.skipUnless(
    sys.platform == "win32", "Exact generation parent ownership requires Windows"
)
class LLMPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.request = replace(fixture(self.root), output_format="TEXT")
        self.pipeline = ResearchPipeline(self.root)

    def test_zero_or_oversized_generated_scene_cannot_commit_success(self):
        (self.root / "manifests").mkdir()
        (self.root / "manifests/creator-llm-components.json").write_text("{}")
        for duration in (0, 999):
            body = (
                "KỊCH BẢN VIDEO NHÁP\nLỜI ĐỌC\nMột tình huống giả định.\nKẾ HOẠCH CẢNH\n"
                + "\n".join(
                    f"{i}. Chữ: Cảnh {i} — {duration} giây" for i in range(1, 7)
                )
            )
            provider = SimpleNamespace(
                name="LOCAL_LLAMA_CPP",
                version="fixture-" + str(duration),
                model=SimpleNamespace(component_id="model", version="fixture"),
                generate=lambda *args: {"draft_body": body},
            )
            with patch(
                "creator_loop.backend.creator.local_generation.selected_provider",
                return_value=provider,
            ):
                with self.assertRaises(ValueError):
                    self.pipeline.analyze(replace(self.request, output_format="VIDEO"))
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT status FROM processing_runs").fetchall(),
                [("FAILED",), ("FAILED",)],
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM creator_workflows").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0], 0
            )

    def test_recovery_requires_matching_dead_parent_and_keeps_history(self):
        from creator_loop.backend.creator.generation_recovery import recover_generation
        from creator_loop.backend.runtime.database import _connect_write

        self.test_model_failure_has_failed_run_and_no_draft_or_workflow()
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db, db:
            run = db.execute("SELECT run_id FROM processing_runs").fetchone()[0]
            db.execute(
                "UPDATE processing_runs SET status='RUNNING',finished_at=NULL WHERE run_id=?",
                (run,),
            )
        with patch(
            "creator_loop.backend.creator.generation_recovery._identity_dead",
            return_value=False,
        ):
            self.assertEqual(recover_generation(self.root), ())
        path = self.root / "runtime" / ("creator-task-" + run) / "ownership.json"
        marker = json.loads(path.read_text())
        wrong = {**marker, "component_version": "another/worker"}
        path.write_text(json.dumps(wrong))
        with patch(
            "creator_loop.backend.creator.generation_recovery._identity_dead",
            return_value=True,
        ):
            self.assertEqual(recover_generation(self.root), ())
            path.write_text(json.dumps(marker))
            self.assertEqual(recover_generation(self.root), (run,))
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT status,error_code FROM processing_runs").fetchone(),
                ("CANCELLED", "PARENT_EXIT"),
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )

    def test_model_failure_has_failed_run_and_no_draft_or_workflow(self):
        (self.root / "manifests").mkdir()
        (self.root / "manifests/creator-llm-components.json").write_text("{}")
        provider = SimpleNamespace(
            name="LOCAL_LLAMA_CPP",
            version="fixture",
            model=SimpleNamespace(component_id="model", version="fixture"),
            generate=lambda *args: (_ for _ in ()).throw(ValueError("rejected")),
        )
        with patch(
            "creator_loop.backend.creator.local_generation.selected_provider",
            return_value=provider,
        ):
            with self.assertRaisesRegex(ValueError, "rejected"):
                self.pipeline.analyze(self.request)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT status FROM processing_runs").fetchall(),
                [("FAILED",)],
            )
            for table in (
                "creator_workflows",
                "draft_versions",
                "review_events",
                "draft_render_requests",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )

    def test_generated_draft_keeps_exact_factual_citation_without_human_acceptance(
        self,
    ):
        (self.root / "manifests").mkdir()
        (self.root / "manifests/creator-llm-components.json").write_text("{}")
        fact = "Niềm tin cần được kiểm chứng bằng sự thật."
        result = {
            "observations": [
                {
                    "material_id": "source:s",
                    "quote": fact,
                    "observation": "Nguồn khuyên kiểm chứng.",
                }
            ],
            "inferences": ["Giả thuyết biên tập."],
            "direction": "Đọc nguồn",
            "draft_body": fact + "\nLời khuyên do biên tập viết.",
            "factual_claims": [
                {"text": fact, "material_ids": ["source:s"], "source_quote": fact}
            ],
            "provenance": {"backend": "LOCAL_LLAMA_CPP", "version": "fixture"},
        }
        provider = SimpleNamespace(
            name="LOCAL_LLAMA_CPP",
            version="fixture",
            model=SimpleNamespace(component_id="model", version="fixture"),
            generate=lambda *args: json.loads(json.dumps(result)),
        )
        with patch(
            "creator_loop.backend.creator.local_generation.selected_provider",
            return_value=provider,
        ):
            wid = self.pipeline.analyze(self.request)
        draft = self.pipeline.create_draft(wid)
        self.assertEqual(
            ResearchPipeline(self.root).draft(draft.draft_version_id), draft
        )
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM processing_runs WHERE task_type='CREATOR_GENERATION'"
                ).fetchone()[0],
                "SUCCEEDED",
            )
            cid = db.execute(
                "SELECT claim_version_id FROM draft_assertions WHERE draft_version_id=? AND review_state='UNREVIEWED'",
                (draft.draft_version_id,),
            ).fetchone()[0]
            self.assertIsNotNone(
                db.execute(
                    "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id=?",
                    (cid,),
                ).fetchone()
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM draft_render_requests").fetchone()[0],
                0,
            )
        edited = self.pipeline.create_draft(
            wid, body_text="Mở bài mới.\n" + draft.body_text + "\nKết do Creator sửa."
        )
        self.assertNotEqual(edited.draft_version_id, draft.draft_version_id)
        self.assertEqual(self.pipeline.draft(draft.draft_version_id), draft)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            span = db.execute(
                "SELECT text_start,text_end,claim_version_id,review_state FROM draft_assertions WHERE draft_version_id=? AND claim_version_id IS NOT NULL",
                (edited.draft_version_id,),
            ).fetchone()
            self.assertEqual(edited.body_text[span[0] : span[1]], fact)
            self.assertEqual(span[2:], (cid, "UNREVIEWED"))
        with (
            patch(
                "creator_loop.backend.creator.local_generation.selected_provider",
                return_value=provider,
            ),
            patch(
                "creator_loop.backend.analysis.generation_prompt.generation_prompt",
                return_value="Changed effective prompt",
            ),
        ):
            new = self.pipeline.analyze(self.request)
        self.assertNotEqual(new, wid, "Prompt change must not reuse old workflow cache")
