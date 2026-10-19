"""Failure boundaries for durable Web wakeups, without a model or cloud service."""

import copy
import hashlib
import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class WebJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_python_analysis_boundary_rejects_changed_or_unverified_factual_background(
        self,
    ):
        from creator_loop.backend.analysis.web_analysis_audit import (
            validate_analysis_audit,
        )

        source = {"material_id": "m", "text": "Nguồn giới thiệu ba câu hỏi."}
        analysis = {
            "observations": [
                {
                    "material_id": "m",
                    "quote": source["text"],
                    "observation": source["text"],
                }
            ],
            "inferences": ["Giả thuyết chưa xác nhận: góc kể có thể phù hợp."],
            "direction": "Đề xuất góc nội dung, không hứa kết quả.",
        }
        premise = {"assertion": source["text"], "quote_ids": [0], "supported": True}
        receipt = {
            "analysis_sha256": hashlib.sha256(
                json.dumps(analysis, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest(),
            "checks": [
                {
                    "field": "observations.0",
                    "verdict": "SUPPORTED",
                    "premise_checks": [premise],
                    "reason": "Fixture",
                    "issues": [],
                },
                {
                    "field": "inferences.0",
                    "verdict": "HYPOTHESIS",
                    "truth_status": "UNCONFIRMED",
                    "premise_checks": [premise],
                    "reason": "Untested editorial hypothesis",
                    "issues": [],
                },
                {
                    "field": "direction",
                    "verdict": "EDITORIAL_ONLY",
                    "premise_checks": [],
                    "reason": "Content plan",
                    "issues": [],
                },
            ],
        }
        validate_analysis_audit(receipt, analysis, analysis, [source])
        for mutation in (
            "hash",
            "order",
            "status",
            "premise",
            "quote",
            "scope",
            "issues",
        ):
            changed = copy.deepcopy(receipt)
            if mutation == "hash":
                changed["analysis_sha256"] = "0" * 64
            elif mutation == "order":
                changed["checks"].reverse()
            elif mutation == "status":
                changed["checks"][1]["truth_status"] = "VERIFIED"
            elif mutation == "premise":
                changed["checks"][1]["premise_checks"][0]["supported"] = False
            elif mutation == "quote":
                changed["checks"][1]["premise_checks"][0]["quote_ids"] = [3]
            elif mutation == "scope":
                changed["checks"][2].update(
                    verdict="HYPOTHESIS", truth_status="UNCONFIRMED"
                )
            else:
                changed["checks"][1]["issues"] = ["Invented applicability"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_analysis_audit(changed, analysis, analysis, [source])
        with self.assertRaises(ValueError):
            validate_analysis_audit(
                receipt, analysis, {**analysis, "direction": "Changed"}, [source]
            )

    def setUp(self):
        module = types.ModuleType("workers")
        module.DurableObject = type("DurableObject", (), {})
        module.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
        module.Response = object
        module.import_from_javascript = lambda _: None
        module.python_to_rpc = lambda value: value
        path = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        spec = importlib.util.spec_from_file_location("web_recovery_under_test", path)
        loaded = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"workers": module}):
            spec.loader.exec_module(loaded)
        self.loaded = loaded
        self.app = object.__new__(loaded.Database)
        self.storage = types.SimpleNamespace(
            getAlarm=AsyncMock(return_value=None), setAlarm=AsyncMock()
        )
        self.app.ctx = types.SimpleNamespace(storage=self.storage)

    async def test_queued_receipt_without_alarm_rearms_exactly_once(self):
        self.app.rows = lambda *_: [
            {
                "status": "QUEUED",
                "record": json.dumps({"id": "same-id", "status": "QUEUED"}),
            }
        ]
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_awaited_once()
        self.assertGreater(
            self.storage.setAlarm.await_args.args[0], time.time() * 1000 - 1000
        )

    async def test_existing_alarm_does_not_duplicate_or_replace_schedule(self):
        self.app.rows = lambda *_: [{"status": "QUEUED", "record": "{}"}]
        self.storage.getAlarm.return_value = 1000
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_not_awaited()

    async def test_running_owner_loss_is_failed_unknown_without_another_call(self):
        original = {
            "id": "same-id",
            "status": "RUNNING",
            "lease_until": time.time() - 1,
            "calls": [{"outcome": "UNKNOWN"}],
        }
        self.app.rows = lambda *_: [{"record": json.dumps(original)}]
        observed = []
        self.app.sync_run = observed.append
        await self.app.alarm()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["status"], "FAILED")
        self.assertTrue(observed[0]["unknown_outcome"])
        self.assertEqual(observed[0]["calls"], original["calls"])

    async def test_live_running_lease_is_preserved_without_resubmission(self):
        lease = time.time() + 300
        self.app.rows = lambda *_: [
            {
                "record": json.dumps(
                    {"id": "same-id", "status": "RUNNING", "lease_until": lease}
                )
            }
        ]
        await self.app.alarm()
        self.storage.setAlarm.assert_awaited_once_with(int(lease * 1000) + 1)

    async def test_alarm_failure_before_enqueue_cannot_commit_a_job(self):
        # Assert the executable method order at the actual failure boundary,
        # rather than claiming that a mock tests remote storage durability.
        import ast
        import inspect
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(self.loaded.Database.authorized))
        )
        schedule = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "setAlarm"
        ]
        commit = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "transactionSync"
        ]
        self.assertLess(max(schedule), min(commit))

    async def test_unconfirmed_hypothesis_reaches_real_domain_finalization_without_review(
        self,
    ):
        from contextlib import closing
        from dataclasses import asdict

        from creator_loop.backend.analysis.workers_quality import (
            ARTICLE_REQUIREMENT_IDS,
        )
        from creator_loop.backend.runtime.database import _connect_write

        from tests.backend.test_web_domain import WebDomainTests

        fixture = WebDomainTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tmp.cleanup)
        content = fixture.fixture()
        analysis = {k: content[k] for k in ("observations", "inferences", "direction")}
        analysis_hash = hashlib.sha256(
            json.dumps(analysis, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        fact = content["factual_claims"][0]["text"]
        premise = {"assertion": fact, "quote_ids": [1], "supported": True}
        receipt = {
            "analysis_sha256": analysis_hash,
            "checks": [
                {
                    "field": "observations.0",
                    "verdict": "SUPPORTED",
                    "premise_checks": [premise],
                    "reason": "Source-bound fixture",
                    "issues": [],
                },
                {
                    "field": "inferences.0",
                    "verdict": "HYPOTHESIS",
                    "truth_status": "UNCONFIRMED",
                    "premise_checks": [premise],
                    "reason": "Fixture hypothesis remains unconfirmed",
                    "issues": [],
                },
                {
                    "field": "direction",
                    "verdict": "EDITORIAL_ONLY",
                    "premise_checks": [],
                    "reason": "Fixture editorial plan",
                    "issues": [],
                },
            ],
        }
        pieces = content["draft_body"].split("\n\n")
        audit = {
            "draft_body": content["draft_body"],
            "issues": [],
            "paragraphs": [
                {"text": pieces[0], "kind": "SOURCE_FACT", "factual_passages": [fact]},
                {"text": pieces[1], "kind": "ADVICE", "factual_passages": []},
            ],
            "finding_checks": [
                {
                    "finding": f,
                    "verdict": "ADDRESSED",
                    "reason": "Technical fixture only",
                    "segments": [{"index": i, "text": p} for i, p in enumerate(pieces)],
                }
                for f in ARTICLE_REQUIREMENT_IDS
            ],
        }
        run_id = "c" * 32
        fixture.domain.begin_run(fixture.request, run_id, "fixture", "fixture-model")
        job = {
            "id": run_id,
            "status": "QUEUED",
            "calls": [],
            "request": asdict(fixture.request),
            "sources": [asdict(fixture.selected)],
            "input_fingerprint": "fixture",
        }
        records = {run_id: copy.deepcopy(job)}
        self.app.rows = lambda *_: [{"record": json.dumps(records[run_id])}]
        self.app.save_job = lambda value: records.update({run_id: copy.deepcopy(value)})
        self.app.load = lambda: (fixture.root, 0)
        self.app.commit = lambda *_: None
        self.app.discard_projection = lambda *_: None
        self.storage.transactionSync = lambda function: function()
        self.app.env = types.SimpleNamespace(
            CREATOR_MODEL="fixture-model",
            CREATOR_VERSION=types.SimpleNamespace(id="fixture-version"),
            AI=None,
        )
        stages = ["analysis", "writing", "editing", "source_audit"]
        envelopes = []
        for s, value in zip(
            stages[:3], [analysis, {}, {"source_issues": [], "edits": []}], strict=True
        ):
            item = {"stage": s, "value": value}
            if s == "editing":
                item["task_requirements"] = ARTICLE_REQUIREMENT_IDS
            envelopes.append({"stage_result": item})
        envelopes.append(
            {
                "result": {"response": json.dumps(content)},
                "analysis_audit": receipt,
                "quality_audit": audit,
                "stages": [
                    {
                        "stage": s,
                        **({"body": content["draft_body"]} if s == "editing" else {}),
                    }
                    for s in stages
                ],
            }
        )
        sdk = types.SimpleNamespace(
            stage=AsyncMock(
                side_effect=[
                    json.dumps(
                        {
                            **e,
                            "ok": True,
                            "model": "fixture-model",
                            "worker_version": "fixture-version",
                            "model_calls": 1,
                        }
                    )
                    for e in envelopes
                ]
            )
        )
        with patch.object(self.loaded, "import_from_javascript", return_value=sdk):
            await self.app.alarm()
        self.assertEqual(records[run_id]["status"], "SUCCEEDED")
        self.assertEqual(sdk.stage.await_count, 4)
        self.assertEqual(records[run_id]["result"]["inferences"], content["inferences"])
        self.assertEqual(
            records[run_id]["stage_outputs"][-1]["analysis_audit"]["checks"][1][
                "truth_status"
            ],
            "UNCONFIRMED",
        )
        self.assertTrue(records[run_id]["saved_draft"]["draft_version_id"])
        with closing(_connect_write(fixture.domain.path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
