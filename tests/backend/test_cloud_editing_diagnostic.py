"""Actual QA-only handler/alarm with isolated SQL and controlled bridge; no cloud."""

import ast
import asyncio
import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from creator_loop.backend.creator.web_domain import WebInputError

from tests.backend import test_cloud_import_api


class EditingDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_cloud_import_api.CloudImportAPITests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.api, self.db = self.fixture.api, self.fixture.coord
        self.space = self.api.authorized.__func__.__globals__
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        tree = ast.parse(source.read_text("utf8"))
        names = {"save_job", "sync_run", "probe_editing", "alarm"}
        nodes = [
            m
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "Database"
            for m in n.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and m.name in names
        ]
        nodes += [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "valid_included_grant"
        ]
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"),
            self.space,
        )
        for name in names:
            setattr(type(self.api), name, self.space[name])
        self.space.update(urlsplit=urlsplit, python_to_rpc=lambda v: v)
        self.api.ctx.storage.setAlarm = AsyncMock()
        self.api.recover_wakeup = AsyncMock()
        self.api.env.CREATOR_MODEL = "@cf/openai/gpt-oss-120b"
        self.api.env.CREATOR_VERSION = SimpleNamespace(id="fixture-deployment")
        self.api.env.AI = object()
        self.key = "e" * 32
        self.packet = {
            "format": "TEXT",
            "mode": "MANUAL",
            "prompt": "fixture",
            "writing_prompt": "fixture",
            "schema": {},
            "stage": "editing",
            "previous": [
                {"stage": "analysis", "value": {}},
                {"stage": "writing", "value": {}},
            ],
        }
        self.data = self.payload(self.packet)
        now = time.time()
        self.grant = {
            "grant": "B-finish-20261017-free",
            "model": self.api.env.CREATOR_MODEL,
            "account_plan": "Paid",
            "included_only": True,
            "max_extra_charge_usd": 0,
            "observed_at": now,
            "expires_at": now + 110,
            "remaining_calls": 1,
            "remaining_neurons": 10000,
            "estimated_chain_neurons": 5000,
        }
        self.set_grant(self.grant)

    def payload(self, packet):
        raw = json.dumps(packet)
        return {
            "packet_json": raw,
            "input_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }

    def set_grant(self, data):
        self.db.execute(
            "INSERT INTO control VALUES('grant',?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
            (json.dumps(data),),
        )
        self.db.commit()

    def call(
        self,
        data=None,
        *,
        host="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
        operator=True,
        method="POST",
        path="/api/diagnostics/editing",
        key=None,
    ):
        headers = {"Idempotency-Key": self.key if key is None else key}
        if operator:
            headers["X-Operator-Key"] = "fixture"
        request = SimpleNamespace(headers=headers, url="https://" + host + path)
        return asyncio.run(
            self.api.authorized(
                request, path, method, self.data if data is None else data
            )
        )

    def checkpoint(self):
        return self.api.rows("SELECT * FROM checkpoint")

    def job(self):
        return json.loads(
            self.api.rows("SELECT record FROM jobs WHERE id=?", self.key)[0]["record"]
        )

    def test_only_QA_operator_POST_and_explicit_identity(self):
        before = self.checkpoint()
        self.assertEqual(
            self.call(
                host="creator-loop-b-web.creator-loop-ai-beta.workers.dev"
            ).status,
            404,
        )
        self.assertEqual(self.call(operator=False).status, 403)
        self.assertEqual(self.call(method="GET").status, 405)
        with self.assertRaises(WebInputError):
            self.call(key="")
        self.assertEqual(self.api.rows("SELECT * FROM jobs"), [])
        self.assertEqual(self.checkpoint(), before)

    def test_reject_bad_digest_size_or_nonediting_stage_before_reservation(self):
        cases = [
            dict(self.data, input_sha256="0" * 64),
            self.payload({**self.packet, "stage": "analysis"}),
            self.payload({**self.packet, "previous": []}),
            self.payload({**self.packet, "format": "VIDEO"}),
            self.payload({**self.packet, "prompt": "x" * 65537}),
        ]
        for data in cases:
            with (
                self.subTest(data_len=len(data["packet_json"])),
                self.assertRaises(WebInputError),
            ):
                self.call(data)
        self.assertEqual(
            json.loads(
                self.api.rows("SELECT record FROM control WHERE id='grant'")[0][
                    "record"
                ]
            )["remaining_calls"],
            1,
        )

    def test_one_fresh_reservation_and_idempotency_no_canonical_change(self):
        before = self.checkpoint()
        queued = self.call().value
        self.assertEqual(queued["kind"], "QA_EDITING_DIAGNOSTIC")
        self.assertEqual(queued["grant"]["remaining_calls"], 0)
        self.assertEqual(self.call().value, queued)
        self.assertEqual(len(self.api.rows("SELECT * FROM jobs")), 1)
        with self.assertRaises(WebInputError):
            self.call(self.payload({**self.packet, "prompt": "changed"}))
        with self.assertRaises(WebInputError):
            self.call(key="d" * 32)
        self.assertEqual(self.checkpoint(), before)

    def test_expired_insufficient_wrongmodel_or_multiple_call_grant_denied(self):
        for change in (
            {"observed_at": time.time() - 121},
            {"remaining_neurons": 4999},
            {"model": "different"},
            {"remaining_calls": 4},
            {"included_only": False},
        ):
            self.set_grant({**self.grant, **change})
            with self.subTest(change=change), self.assertRaises(WebInputError):
                self.call()
        self.assertEqual(self.api.rows("SELECT * FROM jobs"), [])

    def test_alarm_invokes_one_bridge_and_persists_unknown_before_call(self):
        self.call()
        before = self.checkpoint()
        seen = []

        async def stage(*args):
            job = self.job()
            seen.append(args)
            self.assertEqual(job["calls"][0]["outcome"], "UNKNOWN")
            self.assertEqual(job["calls"][0]["measured_usage"], None)
            return json.dumps(
                {
                    "ok": True,
                    "model_calls": 1,
                    "native_usage": {"prompt_tokens": 2},
                    "model": self.api.env.CREATOR_MODEL,
                    "worker_version": self.api.env.CREATOR_VERSION.id,
                    "stage_result": {"stage": "editing"},
                }
            )

        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=stage)
        asyncio.run(self.api.alarm())
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.job()["status"], "SUCCEEDED_DIAGNOSTIC")
        self.assertEqual(self.job()["calls"][0]["measured_usage"], {"prompt_tokens": 2})
        asyncio.run(self.api.alarm())
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.checkpoint(), before)

    def test_lost_binding_response_and_alarm_recovery_never_retry(self):
        self.call()
        before = self.checkpoint()

        async def stage(*_):
            raise RuntimeError("private native failure")

        binding = AsyncMock(side_effect=stage)
        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=binding)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.job()["calls"][0]["outcome"], "UNKNOWN")
        self.assertNotIn("private native failure", self.job()["message"])
        asyncio.run(self.api.alarm())
        self.assertEqual(binding.await_count, 1)
        job = self.job()
        job.update(status="RUNNING", lease_until=0)
        self.api.save_job(job)
        asyncio.run(self.api.alarm())
        self.assertTrue(self.job()["unknown_outcome"])
        self.assertEqual(binding.await_count, 1)
        self.assertEqual(self.checkpoint(), before)

    def test_get_cancel_and_late_result_preserve_operator_and_domain_boundary(self):
        self.call()
        before = self.checkpoint()
        path = "/api/jobs/" + self.key
        self.assertEqual(
            self.call({}, path=path, method="GET", operator=False).status, 403
        )
        self.assertEqual(
            self.call({"job_id": self.key}, path="/api/cancel", operator=False).status,
            403,
        )

        async def stage(*_):
            current = self.job()
            current["status"] = "CANCELLED"
            self.api.sync_run(current)
            return json.dumps({"ok": False, "native_usage": None, "model_calls": 1})

        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=stage)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "CANCELLED")
        self.assertEqual(self.job()["calls"][0]["outcome"], "RECEIVED")
        self.assertIsNone(self.job()["calls"][0]["measured_usage"])
        self.assertEqual(self.checkpoint(), before)

    def test_queued_cancel_or_changed_source_expires_without_inference(self):
        self.call()
        before = self.checkpoint()
        self.assertEqual(
            self.call({"job_id": self.key}, path="/api/cancel").value["status"],
            "CANCELLED",
        )
        binding = AsyncMock()
        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=binding)
        asyncio.run(self.api.alarm())
        binding.assert_not_awaited()
        job = self.job()
        job["status"] = "QUEUED"
        self.api.save_job(job)
        self.api.env.CREATOR_VERSION.id = "different-deployment"
        asyncio.run(self.api.alarm())
        binding.assert_not_awaited()
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.checkpoint(), before)

    def test_large_result_is_bounded_and_unknown_usage_is_not_zero(self):
        self.call()
        before = self.checkpoint()

        async def stage(*_):
            return json.dumps({"ok": True, "private": "x" * (256 * 1024 + 1)})

        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=stage)
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "FAILED")
        self.assertIsNone(job["calls"][0]["measured_usage"])
        self.assertEqual(
            job["stage_outputs"][0]["error"], "DIAGNOSTIC_RESULT_TOO_LARGE"
        )
        self.assertLess(len(json.dumps(job["stage_outputs"])), 1024)
        self.assertEqual(self.checkpoint(), before)

    def test_cancel_during_alarm_wakeup_prevents_binding_dispatch(self):
        self.call()
        before = self.checkpoint()

        async def wakeup(*_):
            current = self.job()
            if current["status"] == "RUNNING":
                current["status"] = "CANCELLED"
                self.api.sync_run(current)

        self.api.ctx.storage.setAlarm = AsyncMock(side_effect=wakeup)
        binding = AsyncMock()
        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=binding)
        asyncio.run(self.api.alarm())
        binding.assert_not_awaited()
        self.assertEqual(self.job()["status"], "CANCELLED")
        self.assertEqual(self.job()["calls"], [])
        self.assertEqual(self.checkpoint(), before)

    def test_actual_zero_calls_is_distinct_from_held_one_call_reservation(self):
        self.call()
        self.space["import_from_javascript"] = lambda _: SimpleNamespace(
            stage=AsyncMock(
                return_value=json.dumps(
                    {"ok": False, "model_calls": 0, "native_usage": None}
                )
            )
        )
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["calls"][0]["model_calls"], 0)
        self.assertEqual(self.job()["calls"][0]["reserved_model_calls"], 1)
        self.assertIsNone(self.job()["calls"][0]["measured_usage"])
        self.assertEqual(self.job()["status"], "FAILED")

    def test_success_missing_or_wrong_model_version_is_not_accepted(self):
        self.call()
        self.space["import_from_javascript"] = lambda _: SimpleNamespace(
            stage=AsyncMock(
                return_value=json.dumps(
                    {
                        "ok": True,
                        "model_calls": 1,
                        "model": "wrong",
                        "worker_version": "wrong",
                        "native_usage": {"neurons": 0},
                    }
                )
            )
        )
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertFalse(self.job()["calls"][0]["envelope_identity_valid"])
        self.assertIsNone(self.job()["calls"][0]["measured_usage"])

    def test_late_success_cannot_revive_failed_recovery(self):
        self.call()
        before = self.checkpoint()

        async def stage(*_):
            current = self.job()
            current.update(status="FAILED", unknown_outcome=True)
            self.api.sync_run(current)
            return json.dumps(
                {
                    "ok": True,
                    "model_calls": 1,
                    "model": self.api.env.CREATOR_MODEL,
                    "worker_version": self.api.env.CREATOR_VERSION.id,
                    "stage_result": {"stage": "editing"},
                }
            )

        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=stage)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertTrue(self.job()["unknown_outcome"])
        self.assertEqual(self.job()["calls"][0]["outcome"], "RECEIVED")
        self.assertEqual(self.checkpoint(), before)

    def test_late_response_kept_separate_if_attempt_identity_changed(self):
        self.call()
        before = self.checkpoint()

        async def stage(*_):
            current = self.job()
            current.update(status="FAILED", lease_until=0)
            self.api.sync_run(current)
            return json.dumps({"ok": False, "model_calls": 1, "native_usage": None})

        self.space["import_from_javascript"] = lambda _: SimpleNamespace(stage=stage)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.job()["calls"][0]["outcome"], "UNKNOWN")
        late = self.api.rows(
            "SELECT record FROM control WHERE id=?", "late-editing-" + self.key
        )
        self.assertEqual(
            json.loads(late[0]["record"])["calls"][0]["outcome"], "RECEIVED"
        )
        self.assertEqual(self.checkpoint(), before)
