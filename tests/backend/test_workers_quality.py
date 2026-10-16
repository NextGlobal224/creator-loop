import copy
import json
import unittest
from pathlib import Path

from creator_loop.backend.analysis.workers_quality import validate_quality_audit


class WorkersQualityTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = [
            "Giả sử bạn nhận được một ảnh thiếu đường dẫn gốc.",
            "Hãy hỏi người gửi về nguồn và ngày công bố.",
            "Ghi chú COR nêu ba câu hỏi kiểm chứng.",
        ]
        self.content = {
            "draft_body": "\n\n".join(self.paragraphs),
            "factual_claims": [{"text": self.paragraphs[2]}],
        }
        self.audit = {
            "draft_body": self.content["draft_body"],
            "issues": [],
            "finding_checks": [],
            "paragraphs": [
                {"text": text, "kind": kind, "factual_passages": facts}
                for text, kind, facts in zip(
                    self.paragraphs,
                    ["HYPOTHETICAL", "ADVICE", "SOURCE_FACT"],
                    [[], [], [self.paragraphs[2]]],
                    strict=True,
                )
            ],
        }

    def test_real_v11_pilot_lost_label_is_rejected_without_rewriting(self):
        path = Path(__file__).parents[1] / "fixtures/workers_b_v11_failed.txt"
        raw = path.read_text(encoding="utf-8").strip()
        content = {"draft_body": raw, "factual_claims": []}
        with self.assertRaisesRegex(ValueError, "explicit label"):
            validate_quality_audit(content, None)
        self.assertEqual(content["draft_body"], raw)
        self.assertIn("Biểu đồ trong ảnh thường", raw)

    def test_complete_audit_and_exact_annotated_fact_can_pass_boundary(self):
        validate_quality_audit(self.content, self.audit)

    def test_audit_must_be_present_and_bound_to_all_paragraphs(self):
        invalid = [None, {}, {**self.audit, "draft_body": "changed"}]
        invalid.append({**self.audit, "paragraphs": self.audit["paragraphs"][:-1]})
        changed = copy.deepcopy(self.audit)
        changed["paragraphs"].reverse()
        invalid.append(changed)
        for audit in invalid:
            with self.subTest(audit=audit), self.assertRaises(ValueError):
                validate_quality_audit(self.content, audit)

    def test_adverse_audit_and_unannotated_fact_cannot_pass(self):
        for reason in [
            "Unsupported chart origin",
            "Lost note scope",
            "Missing citation",
        ]:
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, "issues"),
            ):
                validate_quality_audit(self.content, {**self.audit, "issues": [reason]})
        for facts in [[], ["A different assertion"]]:
            audit = copy.deepcopy(self.audit)
            audit["paragraphs"][2]["factual_passages"] = facts
            with self.subTest(facts=facts), self.assertRaises(ValueError):
                validate_quality_audit(self.content, audit)
        self.content["factual_claims"] = []
        with self.assertRaisesRegex(ValueError, "Claim annotation"):
            validate_quality_audit(self.content, self.audit)

    def test_actual_sdk_pilot_empty_audit_cannot_hide_uncited_assertions(self):
        path = (
            Path(__file__).parents[1] / "fixtures/workers_b_v25_uncited_assertions.json"
        )
        actual = json.loads(path.read_text(encoding="utf-8"))
        before = copy.deepcopy(actual)
        self.assertEqual(actual["audit"]["issues"], [])
        with self.assertRaisesRegex(ValueError, "uncited comparative or absolute"):
            validate_quality_audit(
                actual["content"], {**actual["audit"], "finding_checks": []}
            )
        self.assertEqual(actual, before)

    def test_cited_comparative_still_needs_complete_source_audit(self):
        text = "Ghi chú nói khả năng sai lệch sẽ cao hơn."
        self.paragraphs[2] = text
        self.content["draft_body"] = "\n\n".join(self.paragraphs)
        self.content["factual_claims"] = [{"text": text}]
        self.audit["draft_body"] = self.content["draft_body"]
        self.audit["paragraphs"][2].update(text=text, factual_passages=[text])
        validate_quality_audit(self.content, self.audit)
        with self.assertRaisesRegex(ValueError, "issues"):
            validate_quality_audit(
                self.content, {**self.audit, "issues": ["Not supported"]}
            )

    def test_empty_global_audit_cannot_drop_editor_finding_or_rename_it(self):
        findings = ["Selected introduction does not describe these steps"]
        with self.assertRaisesRegex(ValueError, "finding coverage"):
            validate_quality_audit(self.content, self.audit, findings)
        check = {
            "finding": findings[0],
            "verdict": "UNRESOLVED",
            "reason": "The final passage still claims unavailable steps.",
            "segments": [{"index": 1, "text": self.paragraphs[1]}],
        }
        audit = {**self.audit, "finding_checks": [check]}
        with self.assertRaisesRegex(ValueError, "finding unresolved"):
            validate_quality_audit(self.content, audit, findings)
        check["verdict"] = "ADDRESSED"
        check["reason"] = "Final text asks for the source, without claiming steps."
        validate_quality_audit(self.content, audit, findings)
        for change in ({"finding": "Different finding"}, {"reason": " "}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_quality_audit(
                    self.content,
                    {**audit, "finding_checks": [{**check, **change}]},
                    findings,
                )
        check["segments"][0]["text"] = "Text not present in the final article"
        with self.assertRaisesRegex(ValueError, "exact passage"):
            validate_quality_audit(self.content, audit, findings)
