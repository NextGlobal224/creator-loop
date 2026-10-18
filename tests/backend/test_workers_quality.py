import copy
import json
import unicodedata
import unittest
from pathlib import Path

from creator_loop.backend.analysis.workers_quality import (
    ARTICLE_REQUIREMENT_IDS,
    requires_hypothetical_opening,
    validate_quality_audit,
    worker_audit_findings,
)


class WorkersQualityTests(unittest.TestCase):
    def test_project_profile_is_exact_and_normalized_not_a_keyword_guess(self):
        legacy = "Giúp người đọc nhận thức đúng về sự thật trước khi chia sẻ ảnh chụp màn hình"
        self.assertTrue(
            requires_hypothetical_opening(unicodedata.normalize("NFD", legacy))
        )
        self.assertFalse(requires_hypothetical_opening(legacy, "Kết bằng bản tóm tắt"))
        self.assertFalse(
            requires_hypothetical_opening(
                "Hướng dẫn chia sẻ ảnh chụp màn hình trên điện thoại"
            )
        )

    def test_optional_fiction_still_needs_its_label(self):
        body = "Bạn bước vào một bảo tàng tưởng tượng."
        audit = {
            "draft_body": body,
            "issues": [],
            "finding_checks": [],
            "paragraphs": [
                {"text": body, "kind": "HYPOTHETICAL", "factual_passages": []}
            ],
        }
        with self.assertRaisesRegex(ValueError, "hypothetical passage"):
            validate_quality_audit(
                {"draft_body": body, "factual_claims": []},
                audit,
                require_hypothetical=False,
            )

    def test_generic_project_accepts_a_cited_factual_opening(self):
        fact = "Bảo tàng mở cửa từ thứ ba đến chủ nhật."
        content = {"draft_body": fact, "factual_claims": [{"text": fact}]}
        audit = {
            "draft_body": fact,
            "issues": [],
            "finding_checks": [],
            "paragraphs": [
                {"text": fact, "kind": "SOURCE_FACT", "factual_passages": [fact]}
            ],
        }
        validate_quality_audit(content, audit, require_hypothetical=False)
        with self.assertRaisesRegex(ValueError, "explicit label"):
            validate_quality_audit(content, audit)
        with self.assertRaisesRegex(ValueError, "Claim annotation"):
            validate_quality_audit(
                {**content, "factual_claims": []}, audit, require_hypothetical=False
            )

    def test_global_checks_cover_opening_and_every_segment_within_paragraph(self):
        pieces = [
            self.paragraphs[0],
            self.paragraphs[1],
            "Bạn có thể ghi lại điều còn chưa rõ.",
            self.paragraphs[2],
        ]
        body = "\n\n".join([pieces[0], " ".join(pieces[1:3]), pieces[3]])
        audit = copy.deepcopy(self.audit)
        audit["draft_body"] = body
        audit["paragraphs"][1]["text"] = " ".join(pieces[1:3])
        findings = ["PROJECT_NO_UNSOURCED_RULE", "PROJECT_NONREPETITIVE_DEVELOPMENT"]
        audit["finding_checks"] = [
            {
                "finding": finding,
                "verdict": "ADDRESSED",
                "reason": "Synthetic exact-coverage fixture",
                "segments": [
                    {"index": i, "text": text} for i, text in enumerate(pieces)
                ],
            }
            for finding in findings
        ]
        content = {**self.content, "draft_body": body}
        validate_quality_audit(content, audit, findings)
        for finding in findings:
            invalid = copy.deepcopy(audit)
            next(c for c in invalid["finding_checks"] if c["finding"] == finding)[
                "verdict"
            ] = "NOT_APPLICABLE"
            with (
                self.subTest(finding=finding),
                self.assertRaisesRegex(ValueError, "Project requirement"),
            ):
                validate_quality_audit(content, invalid, findings)
        for omitted in (0, 2):
            invalid = copy.deepcopy(audit)
            invalid["finding_checks"][0]["segments"].pop(omitted)
            with (
                self.subTest(omitted=omitted),
                self.assertRaisesRegex(ValueError, "global audit omitted"),
            ):
                validate_quality_audit(content, invalid, findings)

    def test_actual_f103545_audit_needs_every_editorial_paragraph(self):
        path = Path(__file__).parents[1] / "fixtures/workers_b_f103545_repetition.json"
        actual = json.loads(path.read_text(encoding="utf-8"))
        before = copy.deepcopy(actual)
        self.assertEqual(actual["audit"]["issues"], [])
        with self.assertRaisesRegex(ValueError, "task coverage"):
            worker_audit_findings(actual["editing_stage"])
        findings = ARTICLE_REQUIREMENT_IDS.copy()
        with self.assertRaisesRegex(ValueError, "finding coverage"):
            validate_quality_audit(actual["content"], actual["audit"], findings)
        audit = copy.deepcopy(actual["audit"])
        paragraphs = actual["content"]["draft_body"].split("\n\n")
        check = {
            "finding": "PROJECT_NONREPETITIVE_DEVELOPMENT",
            "verdict": "ADDRESSED",
            "reason": "Synthetic receipt coverage only, not semantic QA",
            "segments": [{"index": 3, "text": paragraphs[3]}],
        }
        audit["finding_checks"].append(check)
        with self.assertRaisesRegex(
            ValueError, "development check|global audit omitted|Project requirement"
        ):
            validate_quality_audit(actual["content"], audit, findings)
        check["segments"] = [
            {"index": i, "text": text} for i, text in enumerate(paragraphs) if i
        ]
        check["verdict"] = "NOT_APPLICABLE"
        with self.assertRaisesRegex(
            ValueError, "development check|global audit omitted|Project requirement"
        ):
            validate_quality_audit(actual["content"], audit, findings)
        for receipt in audit["finding_checks"]:
            receipt["segments"] = [
                {"index": i, "text": text} for i, text in enumerate(paragraphs)
            ]
        check["verdict"] = "UNRESOLVED"
        with self.assertRaisesRegex(ValueError, "unresolved"):
            validate_quality_audit(actual["content"], audit, findings)
        self.assertEqual(actual, before)

    def test_task_coverage_cannot_depend_on_empty_editor_summary(self):
        stage = {
            "stage": "editing",
            "task_requirements": ARTICLE_REQUIREMENT_IDS.copy(),
            "value": {
                "source_issues": [],
                "edits": [{"support": "INSUFFICIENT", "note": "Unsupported rule"}],
            },
        }
        self.assertEqual(
            worker_audit_findings(stage),
            ["Unsupported rule", *ARTICLE_REQUIREMENT_IDS],
        )
        for coverage in (None, [], list(reversed(ARTICLE_REQUIREMENT_IDS))):
            invalid = {**stage, "task_requirements": coverage}
            with self.assertRaisesRegex(ValueError, "task coverage"):
                worker_audit_findings(invalid)

    def test_original_insufficient_findings_are_deduplicated_and_cannot_disappear(self):
        stage = {
            "stage": "editing",
            "task_requirements": ARTICLE_REQUIREMENT_IDS.copy(),
            "value": {
                "source_issues": ["Unsupported rule"],
                "edits": [{"support": "INSUFFICIENT", "note": "Unsupported rule"}],
            },
        }
        self.assertEqual(
            worker_audit_findings(stage), ["Unsupported rule", *ARTICLE_REQUIREMENT_IDS]
        )
        stage["value"]["edits"][0]["note"] = " "
        with self.assertRaisesRegex(ValueError, "explanation"):
            worker_audit_findings(stage)

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
