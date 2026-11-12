"""Failure boundaries for durable Web wakeups, without a model or cloud service."""

import base64
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class FakeRequestBody:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.cancelled = False
        self.read_count = 0

    def getReader(self):
        return self

    async def read(self):
        self.read_count += 1
        if not self.chunks:
            return types.SimpleNamespace(done=True)
        chunk = self.chunks.pop(0)
        return types.SimpleNamespace(
            done=False,
            value=types.SimpleNamespace(byteLength=len(chunk), to_py=lambda: chunk),
        )

    async def cancel(self):
        self.cancelled = True


class WebJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_staging_status_reports_actual_object_and_version(self):
        target = "qa-isolated-restore-" + "a" * 32
        host = "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev"
        version = "11111111-1111-1111-1111-111111111111"
        self.app.ctx.id = types.SimpleNamespace(name=target)
        self.app.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME=host,
            B_WEB_QA_RESTORE_OBJECT_NAME=target,
            B_WEB_QA_SOURCE_OBJECT_NAME="qa-isolated-restore-" + "b" * 32,
            CREATOR_VERSION=types.SimpleNamespace(id=version),
            OPERATOR_KEY="fixture-key",
        )
        self.app._isolated_readonly_login = lambda url: True
        self.app._qa_snapshot_staging = MagicMock(
            return_value={"restored": False, "staged_parts": 1}
        )
        request = types.SimpleNamespace(
            url="https://" + host + "/api/qa-snapshot-staging",
            method="POST",
            headers={
                "X-QA-Expected-Version": version,
                "X-Operator-Key": "fixture-key",
                "Origin": "https://" + host,
                "Content-Type": "application/json",
            },
            body=FakeRequestBody([b'{"operation":"status"}']),
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            body, status = await self.app.fetch(request)
        self.assertEqual(
            (status, body["qa_object"], body["qa_deployment"]), (200, target, version)
        )

    async def test_fixture_runtime_reports_version_without_model_binding(self):
        self.app.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME="creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev",
            CREATOR_VERSION=types.SimpleNamespace(
                id="11111111-1111-1111-1111-111111111111"
            ),
        )
        sdk = types.SimpleNamespace(components=lambda: "{}")
        with (
            patch.object(self.loaded, "import_from_javascript", return_value=sdk),
            patch.object(
                self.loaded,
                "reply",
                side_effect=lambda body, status=200, **kwargs: (body, status),
            ),
        ):
            body, status = await self.app.authorized(None, "/api/runtime", "GET", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["deployment"], "11111111-1111-1111-1111-111111111111")
        self.assertIsNone(body["model"])

    async def test_fixture_routes_two_distinct_objects_only_after_operator_check(self):
        source, target = (
            "qa-isolated-restore-" + "a" * 32,
            "qa-isolated-restore-" + "b" * 32,
        )
        host = "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev"
        seen = []

        async def answer(request):
            return "ok"

        def lookup(name):
            seen.append(name)
            return types.SimpleNamespace(fetch=answer)

        worker = object.__new__(self.loaded.Default)
        worker.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME=host,
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_RESTORE_OBJECT_NAME=target,
            B_WEB_QA_SOURCE_OBJECT_NAME=source,
            OPERATOR_KEY="fixture-key",
            CREATOR_VERSION=types.SimpleNamespace(
                id="11111111-1111-1111-1111-111111111111"
            ),
            DATABASE=types.SimpleNamespace(getByName=lookup),
        )
        request = types.SimpleNamespace(
            url="https://" + host + "/api/qa-snapshot-staging",
            method="POST",
            headers={},
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            self.assertEqual((await worker.fetch(request))[1], 403)
            self.assertEqual(seen, [])
            request.headers = {
                "X-Operator-Key": "fixture-key",
                "X-QA-Expected-Version": "11111111-1111-1111-1111-111111111111",
                "X-QA-Object": "source",
                "Origin": "https://" + host,
                "Content-Type": "application/json",
            }
            request.method = "GET"
            self.assertEqual((await worker.fetch(request))[1], 403)
            request.method = "POST"
            request.headers["Origin"] = "https://other.example"
            self.assertEqual((await worker.fetch(request))[1], 403)
            request.headers["Origin"] = "https://" + host
            request.headers["Content-Type"] = "text/plain"
            self.assertEqual((await worker.fetch(request))[1], 403)
            self.assertEqual(seen, [])
            request.headers["Content-Type"] = "application/json"
            request.headers["X-QA-Expected-Version"] = (
                "22222222-2222-2222-2222-222222222222"
            )
            self.assertEqual((await worker.fetch(request))[1], 409)
            self.assertEqual(seen, [])
            request.headers["X-QA-Expected-Version"] = (
                "11111111-1111-1111-1111-111111111111"
            )
            self.assertEqual(await worker.fetch(request), "ok")
            self.assertEqual(seen[-1], source)
            request.headers["X-QA-Object"] = "target"
            self.assertEqual(await worker.fetch(request), "ok")
            self.assertEqual(seen[-1], target)
            request.url = "https://" + host + "/api/qa-snapshot-export"
            self.assertEqual((await worker.fetch(request))[1], 403)
            request.headers["X-QA-Object"] = "source"
            self.assertEqual(await worker.fetch(request), "ok")
            self.assertEqual(seen[-1], source)
            request.url = "https://" + host + "/api/state"
            request.headers = {}
            prior = len(seen)
            self.assertEqual((await worker.fetch(request))[1], 403)
            self.assertEqual(len(seen), prior)
            worker.env.B_WEB_QA_SOURCE_OBJECT_NAME = target
            self.assertEqual((await worker.fetch(request))[1], 503)

    async def test_fixture_object_rejects_wrong_version_before_processing(self):
        self.app.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME="creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev",
            CREATOR_VERSION=types.SimpleNamespace(
                id="11111111-1111-1111-1111-111111111111"
            ),
        )
        request = types.SimpleNamespace(
            url="https://creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev/api/qa-snapshot-staging",
            method="POST",
            headers={"X-QA-Expected-Version": "22222222-2222-2222-2222-222222222222"},
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            self.assertEqual((await self.app.fetch(request))[1], 409)
            request.headers.clear()
            self.assertEqual((await self.app.fetch(request))[1], 409)
            request.headers["X-QA-Expected-Version"] = (
                "11111111-1111-1111-1111-111111111111"
            )
            self.assertEqual((await self.app.fetch(request))[1], 403)

    async def test_fixture_rejects_worker_object_version_skew(self):
        host = "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev"
        worker = object.__new__(self.loaded.Default)
        worker.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME=host,
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_RESTORE_OBJECT_NAME="qa-isolated-restore-" + "a" * 32,
            B_WEB_QA_SOURCE_OBJECT_NAME="qa-isolated-restore-" + "b" * 32,
            OPERATOR_KEY="fixture-key",
            CREATOR_VERSION=types.SimpleNamespace(
                id="11111111-1111-1111-1111-111111111111"
            ),
            DATABASE=types.SimpleNamespace(getByName=MagicMock(return_value=self.app)),
        )
        self.app.env = types.SimpleNamespace(
            B_WEB_QA_HOSTNAME=host,
            CREATOR_VERSION=types.SimpleNamespace(
                id="22222222-2222-2222-2222-222222222222"
            ),
        )
        request = types.SimpleNamespace(
            url="https://" + host + "/api/qa-snapshot-staging",
            method="POST",
            headers={
                "X-Operator-Key": "fixture-key",
                "X-QA-Expected-Version": "11111111-1111-1111-1111-111111111111",
                "X-QA-Object": "target",
                "Origin": "https://" + host,
                "Content-Type": "application/json",
            },
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            body, status = await worker.fetch(request)
            self.assertEqual(
                (body["error"], status), ("Isolated QA object version mismatch", 409)
            )
            worker.env.DATABASE.getByName.assert_called_once()
            self.app.env.B_WEB_QA_HOSTNAME = ""
            self.assertEqual((await worker.fetch(request))[1], 503)
            self.app.env.B_WEB_QA_HOSTNAME = "unexpected.example"
            self.assertEqual((await worker.fetch(request))[1], 503)

    async def test_qa_worker_rejects_wrong_or_missing_host_before_object_lookup(self):
        worker = object.__new__(self.loaded.Default)
        lookup = MagicMock()
        worker.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_RESTORE_OBJECT_NAME="qa-isolated-restore-" + "c" * 32,
            DATABASE=types.SimpleNamespace(getByName=lookup),
        )
        request = types.SimpleNamespace(
            url="https://creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev/api/state"
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            _, status = await worker.fetch(request)
            self.assertEqual(status, 503)
            del worker.env.B_WEB_QA_RESTORE_OBJECT_NAME
            _, status = await worker.fetch(request)
            self.assertEqual(status, 503)
            worker.env.B_WEB_QA_HOSTNAME = "unexpected.example"
            _, status = await worker.fetch(request)
            self.assertEqual(status, 503)
            lookup.assert_not_called()
            worker.env.B_WEB_QA_RESTORE_OBJECT_NAME = "qa-isolated-restore-" + "c" * 32
            worker.env.B_WEB_QA_HOSTNAME = (
                "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev"
            )
            request.url = (
                "https://creator-loop-b-web.creator-loop-ai-beta.workers.dev/api/state"
            )
            _, status = await worker.fetch(request)
            self.assertEqual(status, 503)
            lookup.assert_not_called()

    def test_isolated_host_requires_exact_configured_allowlisted_https_host(self):
        from urllib.parse import urlsplit

        candidate = "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev"
        production = "creator-loop-b-web.creator-loop-ai-beta.workers.dev"
        qa19 = "creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
        for configured, requested, expected in (
            (candidate, "https://" + candidate + "/api/state", True),
            (qa19, "https://" + qa19 + "/api/state", True),
            ("", "https://" + candidate + "/", False),
            (production, "https://" + production + "/", False),
            (candidate, "https://" + qa19 + "/", False),
            (candidate, "https://" + candidate + ".evil.example/", False),
            (candidate, "http://" + candidate + "/", False),
            (candidate, "https://" + candidate + ":443/", False),
        ):
            with self.subTest(configured=configured, requested=requested):
                self.assertEqual(
                    self.loaded.valid_isolated_qa_host(
                        urlsplit(requested),
                        types.SimpleNamespace(B_WEB_QA_HOSTNAME=configured),
                    ),
                    expected,
                )

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
        control = sqlite3.connect(":memory:")
        self.addCleanup(control.close)
        control.execute("CREATE TABLE control(id TEXT PRIMARY KEY,record TEXT)")
        self.app.sql = types.SimpleNamespace(
            exec=lambda statement, *args: control.execute(statement, args)
        )
        self.app._restore_hold = lambda: False
        self.alarm_control = control
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

    async def test_observation_mode_parks_queue_and_rearms_after_exit(self):
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="1")
        pending = {
            "status": "QUEUED",
            "record": json.dumps({"id": "old", "status": "QUEUED"}),
        }
        self.app.rows = lambda *_: [pending]
        await self.app.recover_wakeup()
        await self.app.alarm()
        self.storage.getAlarm.assert_not_awaited()
        self.storage.setAlarm.assert_not_awaited()
        self.assertEqual(json.loads(pending["record"])["status"], "QUEUED")
        marker = self.alarm_control.execute(
            "SELECT record FROM control WHERE id LIKE 'observation-alarm-%'"
        ).fetchone()
        self.assertIsNone(json.loads(marker[0])["scheduled_at"])
        await self.app.alarm()
        self.assertEqual(
            self.alarm_control.execute("SELECT COUNT(*) FROM control").fetchone()[0], 2
        )
        self.app.env.B_WEB_OBSERVATION_ONLY = "0"
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_awaited_once()

    async def test_observation_mode_rearms_running_lease_after_consumed_alarm(self):
        lease = time.time() + 300
        record = {
            "id": "old",
            "status": "RUNNING",
            "lease_until": lease,
            "calls": [{"outcome": "UNKNOWN"}],
        }
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="1")
        self.app.rows = lambda *_: [{"status": "RUNNING", "record": json.dumps(record)}]
        self.storage.getAlarm.return_value = None
        await self.app.recover_wakeup()
        await self.app.alarm()
        self.storage.getAlarm.assert_not_awaited()
        self.storage.setAlarm.assert_not_awaited()
        self.assertEqual(record["calls"][0]["outcome"], "UNKNOWN")
        self.app.env.B_WEB_OBSERVATION_ONLY = "0"
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_awaited_once_with(int(lease * 1000))

    async def test_observation_alarm_marker_failure_does_not_silently_succeed(self):
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="1")

        def fail(*_):
            raise RuntimeError("fixture storage failure")

        self.app.sql = types.SimpleNamespace(exec=fail)
        with patch.object(self.loaded, "import_from_javascript") as sdk:
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                await self.app.alarm()
            sdk.assert_not_called()
        self.storage.setAlarm.assert_not_awaited()

    async def test_restore_hold_blocks_recovery_and_alarm_after_flag_unset(self):
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="0")
        self.alarm_control.execute(
            "INSERT INTO control VALUES('snapshot-restore-hold','{}')"
        )
        self.app._restore_hold = lambda: bool(
            self.alarm_control.execute(
                "SELECT 1 FROM control WHERE id='snapshot-restore-hold'"
            ).fetchone()
        )
        self.app.rows = lambda *_: [
            {"record": json.dumps({"id": "q", "status": "QUEUED"}), "status": "QUEUED"}
        ]
        with patch.object(self.loaded, "import_from_javascript") as sdk:
            await self.app.recover_wakeup()
            await self.app.alarm()
            sdk.assert_not_called()
        self.storage.setAlarm.assert_not_awaited()
        before = self.alarm_control.execute("SELECT COUNT(*) FROM control").fetchone()[
            0
        ]
        with patch.object(
            self.loaded, "reply", side_effect=lambda body, status=200: (body, status)
        ):
            for path in ("/api/jobs", "/api/source-jobs", "/api/restore"):
                _, status = await self.app.authorized(None, path, "POST", {})
                self.assertEqual(status, 503)
        self.assertEqual(
            self.alarm_control.execute("SELECT COUNT(*) FROM control").fetchone()[0],
            before,
        )
        self.storage.setAlarm.assert_not_awaited()

    async def test_restore_hold_blocks_login_before_session_or_attempt_write(self):
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="0")
        self.app._restore_hold = lambda: True
        request = types.SimpleNamespace(
            url="https://fixture/api/login",
            method="POST",
            headers={"Origin": "https://fixture", "Content-Type": "application/json"},
            text=AsyncMock(return_value='{"password":"fixture"}'),
        )
        with patch.object(
            self.loaded, "reply", side_effect=lambda body, status=200: (body, status)
        ):
            body, status = await self.app.fetch(request)
        self.assertEqual(status, 503)
        self.assertIn("held", body["error"])
        self.assertEqual(
            self.alarm_control.execute("SELECT COUNT(*) FROM control").fetchone()[0], 0
        )

    async def test_held_isolated_qa_can_login_readonly_but_other_hosts_cannot(self):
        self.alarm_control.row_factory = sqlite3.Row
        self.alarm_control.execute(
            "CREATE TABLE sessions(hash TEXT PRIMARY KEY,expires REAL)"
        )
        self.alarm_control.execute(
            "CREATE TABLE attempts(ip TEXT PRIMARY KEY,started REAL,count INTEGER)"
        )
        self.app.rows = lambda query, *args: [
            dict(row) for row in self.alarm_control.execute(query, args)
        ]
        self.app._restore_hold = lambda: True
        self.app.ctx.id = types.SimpleNamespace(name="qa-isolated-restore-" + "c" * 32)
        self.app.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_HOSTNAME="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
            LOGIN_PASSWORD_HASH=hashlib.sha256(b"fixture").hexdigest(),
        )
        host = "creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
        request = types.SimpleNamespace(
            url="https://other.example/api/login",
            method="POST",
            headers={
                "Origin": "https://other.example",
                "Content-Type": "application/json",
            },
            text=AsyncMock(return_value='{"password":"fixture"}'),
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 503)
            self.assertEqual(
                self.alarm_control.execute("SELECT COUNT(*) FROM sessions").fetchone()[
                    0
                ],
                0,
            )
            request.url = "https://" + host + "/api/login"
            request.headers["Origin"] = "https://" + host
            self.app.env.B_WEB_QA_RESTORE_OBJECT_NAME = self.app.ctx.id.name
            self.app._restore_hold = lambda: False
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 503)
            self.assertEqual(
                self.alarm_control.execute("SELECT COUNT(*) FROM attempts").fetchone()[
                    0
                ],
                0,
            )
            self.app._restore_hold = lambda: True
            with patch.object(self.loaded.secrets, "token_hex", return_value="e" * 64):
                body, status = await self.app.fetch(request)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(
                self.alarm_control.execute("SELECT COUNT(*) FROM sessions").fetchone()[
                    0
                ],
                1,
            )
            _, status = await self.app.authorized(None, "/api/jobs", "POST", {})
            self.assertEqual(status, 503)
        self.storage.setAlarm.assert_not_awaited()

    async def test_qa_snapshot_route_requires_isolated_operator_and_same_origin(self):
        name = "qa-isolated-restore-" + "c" * 32
        self.app.ctx.id = types.SimpleNamespace(name=name)
        self.app.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_HOSTNAME="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
            B_WEB_QA_RESTORE_OBJECT_NAME=name,
            OPERATOR_KEY="fixture-operator",
        )
        self.app._qa_snapshot_staging = lambda data: {"received": data["operation"]}
        host = "https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
        request = types.SimpleNamespace(
            url=host + "/api/qa-snapshot-staging",
            method="POST",
            headers={
                "Origin": host,
                "Content-Type": "application/json",
                "X-Operator-Key": "wrong",
            },
            text=AsyncMock(return_value='{"operation":"status"}'),
            body=FakeRequestBody([b'{"operation":"status"}']),
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 403)
            request.text.assert_not_awaited()
            request.headers["X-Operator-Key"] = "fixture-operator"
            request.headers["Origin"] = "https://other.example"
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 403)
            request.headers["Origin"] = host
            self.app.env.B_WEB_ISOLATED_RESTORE = "0"
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 403)
            self.app.env.B_WEB_ISOLATED_RESTORE = "1"
            self.app.env.B_WEB_OBSERVATION_ONLY = "unexpected"
            _, status = await self.app.fetch(request)
            self.assertEqual(status, 403)
            self.app.env.B_WEB_OBSERVATION_ONLY = "1"
            body, status = await self.app.fetch(request)
            self.assertEqual((body["received"], status), ("status", 200))
            self.app._qa_snapshot_export = lambda data: {"export": data["operation"]}
            request.url = host + "/api/qa-snapshot-export"
            request.body = FakeRequestBody([b'{"operation":"status"}'])
            body, status = await self.app.fetch(request)
            self.assertEqual((body["export"], status), ("status", 200))
            for route in ("qa-snapshot-staging", "qa-snapshot-export"):
                request.url = host + "/api/" + route
                for header in (None, "1"):
                    if header is None:
                        request.headers.pop("Content-Length", None)
                    else:
                        request.headers["Content-Length"] = header
                    request.body = FakeRequestBody(
                        [b"x" * (512 * 1024), b"x", b"never read"]
                    )
                    _, status = await self.app.fetch(request)
                    self.assertEqual(status, 413)
                    self.assertTrue(request.body.cancelled)
                    self.assertEqual(request.body.read_count, 2)
                    request.text.assert_not_awaited()
        self.storage.setAlarm.assert_not_awaited()

    async def test_qa_snapshot_route_stages_and_restores_real_fixture_through_worker(
        self,
    ):
        from creator_loop.backend.runtime.cloud_coordination_snapshot import (
            capture_parts,
        )

        from tests.backend import test_cloud_coordination_snapshot as fixtures

        source = fixtures.CoordinationSnapshotTests("runTest")
        source.setUp()
        self.addCleanup(source.doCleanups)
        parts = []
        with source.source:
            manifest = capture_parts(
                fixtures.row_reader(source.source), lambda i, part: parts.append(part)
            )
        target = fixtures.database()
        self.addCleanup(target.close)
        target.execute("CREATE TABLE sessions(hash TEXT PRIMARY KEY,expires REAL)")
        target.execute(
            "CREATE TABLE attempts(ip TEXT PRIMARY KEY,started REAL,count INTEGER)"
        )
        target.commit()
        self.app.rows = fixtures.row_reader(target)
        self.app.sql = types.SimpleNamespace(
            exec=lambda statement, *args: target.execute(statement, args)
        )
        self.app._restore_hold = self.loaded.Database._restore_hold.__get__(self.app)

        def transaction(callback):
            target.execute("BEGIN IMMEDIATE")
            try:
                value = callback()
            except Exception:
                target.rollback()
                raise
            target.commit()
            return value

        self.storage.transactionSync = transaction
        name, key = "qa-isolated-restore-" + "c" * 32, "a" * 32
        self.app.ctx.id = types.SimpleNamespace(name=name)
        manifest_hash = hashlib.sha256(
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        self.app.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_HOSTNAME="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
            B_WEB_QA_RESTORE_OBJECT_NAME=name,
            B_WEB_QA_RESTORE_SNAPSHOT_ID=key,
            B_WEB_QA_RESTORE_MANIFEST_SHA256=manifest_hash,
            OPERATOR_KEY="fixture-operator",
            LOGIN_PASSWORD_HASH=hashlib.sha256(b"fixture-password").hexdigest(),
        )
        endpoint = object.__new__(self.loaded.Default)
        endpoint.env = self.app.env
        endpoint.env.DATABASE = types.SimpleNamespace(
            getByName=lambda value: self.app if value == name else None
        )
        host = "https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
        request = types.SimpleNamespace(
            url=host + "/api/qa-snapshot-staging",
            method="POST",
            headers={
                "Origin": host,
                "Content-Type": "application/json",
                "X-Operator-Key": "wrong",
            },
            text=AsyncMock(),
        )

        async def submit(payload):
            request.body = FakeRequestBody([json.dumps(payload).encode()])
            return await endpoint.fetch(request)

        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            login_request = types.SimpleNamespace(
                url=host + "/api/login",
                method="POST",
                headers={"Origin": host, "Content-Type": "application/json"},
                text=AsyncMock(
                    return_value=json.dumps({"password": "fixture-password"})
                ),
            )
            body, status = await endpoint.fetch(login_request)
            self.assertEqual(
                (body["error"], status), ("Isolated restore is not complete", 503)
            )
            _, status = await submit({"operation": "status", "snapshot_id": key})
            self.assertEqual(status, 403)
            request.headers["X-Operator-Key"] = "fixture-operator"
            for i, part in enumerate(parts):
                body, status = await submit(
                    {
                        "operation": "put",
                        "snapshot_id": key,
                        "position": i,
                        "sha256": hashlib.sha256(part).hexdigest(),
                        "content_base64": base64.b64encode(part).decode(),
                    }
                )
                self.assertEqual((body["stored"], status), (True, 200))
            body, status = await submit(
                {"operation": "restore", "snapshot_id": key, "manifest": manifest}
            )
            self.assertEqual((body["restored"], status), (True, 200))
            body, status = await submit({"operation": "status", "snapshot_id": key})
            self.assertEqual((body["jobs_held"], status), (True, 200))
            export_id = "b" * 32
            self.app.env.B_WEB_QA_SNAPSHOT_EXPORT = "1"
            self.app.env.B_WEB_QA_EXPORT_SNAPSHOT_ID = export_id
            revision, checkpoint_hash = target.execute(
                "SELECT revision,sha256 FROM checkpoint WHERE id=1"
            ).fetchone()
            request.url = host + "/api/qa-snapshot-export"
            body, status = await submit(
                {
                    "operation": "capture",
                    "snapshot_id": export_id,
                    "revision": revision,
                    "checkpoint_sha256": checkpoint_hash,
                }
            )
            self.assertEqual(status, 200)
            captured = body["manifest"]
            body, status = await submit(
                {"operation": "manifest", "snapshot_id": export_id}
            )
            self.assertEqual((body["manifest"], status), (captured, 200))
            body, status = await submit(
                {"operation": "part", "snapshot_id": export_id, "position": 0}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                hashlib.sha256(base64.b64decode(body["content_base64"])).hexdigest(),
                captured["parts"][0]["sha256"],
            )
            body, status = await endpoint.fetch(login_request)
            self.assertEqual((body["ok"], status), (True, 200))
        self.assertEqual(target.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
        self.storage.setAlarm.assert_not_awaited()

    async def test_isolated_restore_routing_fails_closed(self):
        endpoint = object.__new__(self.loaded.Default)
        stub = types.SimpleNamespace(fetch=AsyncMock(return_value="ok"))
        names = []
        database = types.SimpleNamespace(
            getByName=lambda name: names.append(name) or stub
        )
        endpoint.env = types.SimpleNamespace(DATABASE=database)
        request = types.SimpleNamespace(
            url="https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev/api/state"
        )
        self.assertEqual(await endpoint.fetch(request), "ok")
        self.assertEqual(names, ["private-owner-v1"])
        names.clear()
        endpoint.env.B_WEB_QA_RESTORE_OBJECT_NAME = "qa-isolated-restore-" + "c" * 32
        with patch.object(
            self.loaded, "reply", side_effect=lambda body, status=200: (body, status)
        ):
            _, status = await endpoint.fetch(request)
            self.assertEqual(status, 503)
            self.assertEqual(names, [])
            endpoint.env.B_WEB_OBSERVATION_ONLY = "1"
            endpoint.env.B_WEB_ISOLATED_RESTORE = "1"
            endpoint.env.B_WEB_QA_READONLY_LOGIN = "1"
            endpoint.env.B_WEB_QA_HOSTNAME = (
                "creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
            )
            request.url = "https://production.example/api/state"
            _, status = await endpoint.fetch(request)
            self.assertEqual(status, 503)
            self.assertEqual(names, [])
            request.url = "https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev/api/state"
            endpoint.env.B_WEB_QA_RESTORE_OBJECT_NAME = "private-owner-v1"
            _, status = await endpoint.fetch(request)
            self.assertEqual(status, 503)
            self.assertEqual(names, [])
            endpoint.env.B_WEB_QA_RESTORE_OBJECT_NAME = (
                "qa-isolated-restore-" + "c" * 32
            )
            self.assertEqual(await endpoint.fetch(request), "ok")
            self.assertEqual(names, [endpoint.env.B_WEB_QA_RESTORE_OBJECT_NAME])

    async def test_held_qa_logout_only_changes_sessions(self):
        self.alarm_control.execute(
            "CREATE TABLE sessions(hash TEXT PRIMARY KEY,expires REAL)"
        )
        self.alarm_control.execute("INSERT INTO sessions VALUES('fixture', 9999999999)")
        self.app._restore_hold = lambda: True
        self.app.ctx.id = types.SimpleNamespace(name="qa-isolated-restore-" + "c" * 32)
        self.app.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_HOSTNAME="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
        )
        request = types.SimpleNamespace(
            url="https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev/api/logout"
        )
        with patch.object(
            self.loaded,
            "reply",
            side_effect=lambda body, status=200, **kwargs: (body, status),
        ):
            _, status = await self.app.authorized(request, "/api/logout", "POST", {})
            self.assertEqual(status, 200)
            self.assertEqual(
                self.alarm_control.execute("SELECT COUNT(*) FROM sessions").fetchone()[
                    0
                ],
                0,
            )
            _, status = await self.app.authorized(request, "/api/jobs", "POST", {})
            self.assertEqual(status, 503)
        self.storage.setAlarm.assert_not_awaited()

    async def test_qa_route_cookie_login_read_logout_rejects_old_cookie(self):
        self.alarm_control.row_factory = sqlite3.Row
        self.alarm_control.execute(
            "CREATE TABLE sessions(hash TEXT PRIMARY KEY,expires REAL)"
        )
        self.alarm_control.execute(
            "CREATE TABLE attempts(ip TEXT PRIMARY KEY,started REAL,count INTEGER)"
        )
        self.app.rows = lambda query, *args: [
            dict(row) for row in self.alarm_control.execute(query, args)
        ]
        self.app._restore_hold = lambda: True
        name = "qa-isolated-restore-" + "c" * 32
        self.app.ctx.id = types.SimpleNamespace(name=name)
        self.app.env = types.SimpleNamespace(
            B_WEB_OBSERVATION_ONLY="1",
            B_WEB_ISOLATED_RESTORE="1",
            B_WEB_QA_READONLY_LOGIN="1",
            B_WEB_QA_HOSTNAME="creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev",
            B_WEB_QA_RESTORE_OBJECT_NAME=name,
            LOGIN_PASSWORD_HASH=hashlib.sha256(b"fixture").hexdigest(),
        )
        endpoint = object.__new__(self.loaded.Default)
        endpoint.env = self.app.env
        endpoint.env.DATABASE = types.SimpleNamespace(
            getByName=lambda target: self.app if target == name else None
        )
        original_authorized = self.app.authorized

        async def fixture_read(request, path, method, data):
            if path == "/api/fixture-read" and method == "GET":
                return self.loaded.reply({"revision": "fixture"})
            return await original_authorized(request, path, method, data)

        self.app.authorized = fixture_read
        host = "https://creator-loop-b-web-qa19.creator-loop-ai-beta.workers.dev"
        request = types.SimpleNamespace(
            url=host + "/api/login",
            method="POST",
            headers={"Origin": host, "Content-Type": "application/json"},
            text=AsyncMock(return_value='{"password":"fixture"}'),
        )
        with (
            patch.object(
                self.loaded,
                "reply",
                side_effect=lambda body, status=200, headers=None: (
                    body,
                    status,
                    headers or {},
                ),
            ),
            patch.object(self.loaded.secrets, "token_hex", return_value="e" * 64),
        ):
            _, status, headers = await endpoint.fetch(request)
            self.assertEqual(status, 200)
            request.headers["Cookie"] = headers["Set-Cookie"].split(";", 1)[0]
            request.url = host + "/api/fixture-read"
            request.method = "GET"
            body, status, _ = await endpoint.fetch(request)
            self.assertEqual((body["revision"], status), ("fixture", 200))
            request.url = host + "/api/logout"
            request.method = "POST"
            _, status, _ = await endpoint.fetch(request)
            self.assertEqual(status, 200)
            request.url = host + "/api/fixture-read"
            request.method = "GET"
            _, status, _ = await endpoint.fetch(request)
            self.assertEqual(status, 401)
        self.storage.setAlarm.assert_not_awaited()

    async def test_expired_running_lease_stays_unknown_after_observation(self):
        record = {
            "id": "old",
            "status": "RUNNING",
            "lease_until": time.time() - 1,
            "calls": [{"outcome": "UNKNOWN"}],
        }
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="1")
        self.app.rows = lambda *_: [{"status": "RUNNING", "record": json.dumps(record)}]
        saved = []
        self.app.sync_run = saved.append
        await self.app.alarm()
        self.assertEqual(saved, [])
        self.app.env.B_WEB_OBSERVATION_ONLY = "0"
        with patch.object(self.loaded, "import_from_javascript") as sdk:
            await self.app.alarm()
            sdk.assert_not_called()
        self.assertEqual(saved[0]["status"], "FAILED")
        self.assertTrue(saved[0]["unknown_outcome"])
        self.assertEqual(saved[0]["calls"][0]["outcome"], "UNKNOWN")

    async def test_observation_mode_refuses_mutations_before_dispatch(self):
        self.app.env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY="1")
        with patch.object(
            self.loaded, "reply", side_effect=lambda body, status=200: (body, status)
        ):
            for path in (
                "/api/jobs",
                "/api/source-jobs",
                "/api/diagnostics/editing",
                "/api/restore",
            ):
                body, status = await self.app.authorized(None, path, "POST", {})
                self.assertEqual(status, 503)
                self.assertIn("Observation-only", body["error"])
        self.storage.setAlarm.assert_not_awaited()

    async def test_source_receipts_require_session_before_operator_route(self):
        self.app.authenticated = lambda _: False
        request = types.SimpleNamespace(
            url="https://fixture/api/source-receipts/" + "a" * 32,
            method="GET",
            headers={"X-Operator-Key": "fixture"},
        )
        with patch.object(
            self.loaded, "reply", side_effect=lambda body, status=200: (body, status)
        ):
            _, status = await self.app.fetch(request)
        self.assertEqual(status, 401)

    def test_observation_mode_constructor_does_not_schedule_recovery(self):
        from unittest.mock import MagicMock

        base = self.loaded.Database.__mro__[1]
        for flag, expected in (("1", False), ("bad", False), ("0", True), ("", True)):
            ctx = types.SimpleNamespace(
                storage=types.SimpleNamespace(
                    sql=types.SimpleNamespace(exec=MagicMock())
                ),
                blockConcurrencyWhile=MagicMock(),
            )
            env = types.SimpleNamespace(B_WEB_OBSERVATION_ONLY=flag)
            with patch.object(
                base,
                "__init__",
                lambda obj, context, environment: setattr(obj, "env", environment),
                create=True,
            ):
                self.loaded.Database(ctx, env)
            self.assertEqual(ctx.blockConcurrencyWhile.called, expected)

    def test_active_count_includes_old_and_diagnostic_jobs(self):
        import sqlite3

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE jobs(id TEXT, status TEXT, record TEXT)")
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?)",
            ("old-diagnostic", "QUEUED", json.dumps({"kind": "QA_EDITING_DIAGNOSTIC"})),
        )
        db.executemany(
            "INSERT INTO jobs VALUES(?,?,?)",
            [(str(i), "SUCCEEDED", "{}") for i in range(35)],
        )
        self.app.rows = lambda statement, *args: [
            dict(r) for r in db.execute(statement, args)
        ]
        visible = self.app.rows(
            "SELECT record FROM jobs WHERE COALESCE(json_extract(record,'$.kind'),'') != 'QA_EDITING_DIAGNOSTIC' ORDER BY rowid DESC LIMIT 30"
        )
        self.assertEqual(len(visible), 30)
        self.assertEqual(self.app.active_job_count(), 1)
        db.execute("UPDATE jobs SET status='CANCELLED' WHERE id='old-diagnostic'")
        self.assertEqual(self.app.active_job_count(), 0)
        db.execute("UPDATE jobs SET status='QUEUED' WHERE id='0'")
        self.assertEqual(self.app.active_job_count(), 1)
        visible = self.app.rows(
            "SELECT id FROM jobs WHERE COALESCE(json_extract(record,'$.kind'),'') != 'QA_EDITING_DIAGNOSTIC' ORDER BY rowid DESC LIMIT 30"
        )
        self.assertNotIn("0", [row["id"] for row in visible])

    async def test_cloud_backup_pins_revision_and_media_and_rejects_unsafe_snapshots(
        self,
    ):
        import base64
        import sqlite3

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        for sql in (
            "CREATE TABLE backups(id TEXT PRIMARY KEY,created_at REAL,record TEXT)",
            "CREATE TABLE checkpoint(id INTEGER,revision INTEGER,payload TEXT,sha256 TEXT)",
            "CREATE TABLE objects(sha256 TEXT,size INTEGER)",
            "CREATE TABLE jobs(status TEXT)",
        ):
            db.execute(sql)
        db.execute("INSERT INTO checkpoint VALUES(1,7,'fixture','fixture')")
        raw = "Ảnh gốc giữ trên cloud".encode()
        sha = hashlib.sha256(raw).hexdigest()
        db.execute("INSERT INTO objects VALUES(?,?)", (sha, len(raw)))
        db.commit()
        self.app.sql = types.SimpleNamespace(
            exec=lambda sql, *args: db.execute(sql, args)
        )
        self.app.rows = lambda sql, *args: [dict(row) for row in db.execute(sql, args)]
        self.app.object_bytes = lambda value: raw if value == sha else b"bad"

        def transaction(function):
            with db:
                return function()

        self.storage.transactionSync = transaction

        async def call(path, method="GET", data=None, key="a" * 32):
            request = types.SimpleNamespace(headers={"Idempotency-Key": key})
            with patch.object(
                self.loaded, "reply", side_effect=lambda value, *args: value
            ):
                return await self.app.authorized(request, path, method, data or {})

        created = await call("/api/backups", "POST", {"expected_revision": 7})
        self.assertTrue(created["saved_on_cloud"])
        self.assertEqual(
            await call("/api/backups", "POST", {"expected_revision": 7}), created
        )
        db.execute("UPDATE checkpoint SET revision=8")
        db.commit()
        exported = await call("/api/backups/" + created["id"])
        self.assertEqual(exported["checkpoint"][0]["revision"], 7)
        self.assertEqual(
            base64.b64decode(exported["objects"][0]["content_base64"]), raw
        )
        for revision, key in ((8, "a" * 32), (7, "b" * 32), (True, "b" * 32)):
            with self.assertRaises(self.loaded.WebInputError):
                await call("/api/backups", "POST", {"expected_revision": revision}, key)
        db.execute("INSERT INTO jobs VALUES('RUNNING')")
        db.commit()
        with self.assertRaises(self.loaded.WebInputError):
            await call("/api/backups", "POST", {"expected_revision": 8}, "b" * 32)
        db.execute("DELETE FROM jobs")
        db.commit()
        self.app.object_bytes = lambda _: b"corrupt"
        with self.assertRaises(self.loaded.WebInputError):
            await call("/api/backups", "POST", {"expected_revision": 8}, "b" * 32)
        self.app.object_bytes = lambda _: raw
        for key in ("b" * 32, "c" * 32):
            await call("/api/backups", "POST", {"expected_revision": 8}, key)
        with self.assertRaises(self.loaded.WebInputError):
            await call("/api/backups", "POST", {"expected_revision": 8}, "d" * 32)
        listing = await call("/api/backups")
        self.assertEqual(len(listing["backups"]), 3)
        self.assertNotIn("payload", json.dumps(listing))
        self.assertEqual(db.execute("SELECT revision FROM checkpoint").fetchone()[0], 8)
        self.storage.setAlarm.assert_not_awaited()

    async def test_grant_status_is_read_only_fresh_bounded_and_does_not_export_record(
        self,
    ):
        live = {
            "grant": "B-finish-20261017-free",
            "model": "fixture-model",
            "account_plan": "Free",
            "remaining_neurons": 6000,
            "estimated_chain_neurons": 5000,
            "remaining_calls": 4,
            "observed_at": 950,
            "expires_at": 1100,
            "private_extra": "fixture-not-exported",
        }
        self.app.env = types.SimpleNamespace(CREATOR_MODEL="fixture-model")
        for change, available in (
            (None, False),
            ({}, True),
            ({"remaining_calls": 3}, False),
            ({"remaining_calls": True}, False),
            ({"observed_at": 800}, False),
            ({"observed_at": 1001}, False),
            ({"expires_at": 999}, False),
        ):
            with self.subTest(change=change):
                grant = {**live, **(change or {})}
                self.app.rows = lambda *_: (
                    [] if change is None else [{"record": json.dumps(grant)}]
                )
                with (
                    patch.object(self.loaded.time, "time", return_value=1000),
                    patch.object(self.loaded, "reply", side_effect=lambda value: value),
                ):
                    result = await self.app.authorized(
                        None, "/api/grant-status", "GET", {}
                    )
                self.assertEqual(result["available"], available)
                self.assertEqual(set(result), {"available", "valid_until", "message"})
                self.assertNotIn("fixture-not-exported", json.dumps(result))
                self.assertEqual(result["valid_until"], 1070 if available else None)
        self.storage.setAlarm.assert_not_awaited()

    async def test_existing_alarm_does_not_duplicate_or_replace_schedule(self):
        self.app.rows = lambda *_: [{"status": "QUEUED", "record": "{}"}]
        self.storage.getAlarm.return_value = 1000
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_not_awaited()

    async def test_hold_arriving_during_get_alarm_prevents_rearm(self):
        held = {"value": False}
        self.app._restore_hold = lambda: held["value"]
        self.app.rows = lambda *_: [{"status": "QUEUED", "record": "{}"}]

        async def install_hold():
            held["value"] = True
            return None

        self.storage.getAlarm.side_effect = install_hold
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_not_awaited()

    async def test_job_finishing_during_get_alarm_does_not_rearm_stale_job(self):
        pending = {"status": "QUEUED", "record": json.dumps({"id": "q"})}
        self.app.rows = MagicMock(side_effect=[[pending], []])
        await self.app.recover_wakeup()
        self.storage.setAlarm.assert_not_awaited()

    def test_paid_grant_retains_included_only_ceiling_and_full_forecast(self):
        grant = dict(
            grant="B-finish-20261017-free",
            model="fixture-model",
            account_plan="Paid",
            included_only=True,
            max_extra_charge_usd=0,
            remaining_calls=4,
            observed_at=950,
            expires_at=1100,
            remaining_neurons=6000,
            estimated_chain_neurons=5000,
        )
        self.assertTrue(self.loaded.valid_included_grant(grant, "fixture-model", 1000))
        for change in (
            {"included_only": False},
            {"max_extra_charge_usd": 1},
            {"max_extra_charge_usd": False},
            {"remaining_neurons": 4999},
            {"remaining_neurons": 10001},
            {"estimated_chain_neurons": 1},
            {"observed_at": 1001},
            {"account_plan": "Unlimited"},
        ):
            self.assertFalse(
                self.loaded.valid_included_grant(
                    {**grant, **change}, "fixture-model", 1000
                )
            )

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

    async def test_hold_arriving_during_alarm_write_prevents_dispatch(self):
        original = {"id": "q", "status": "QUEUED", "kind": "SOURCE_TRANSCRIPTION"}
        records = {"q": original}
        self.app.rows = lambda *_: [{"record": json.dumps(records["q"])}]
        self.app.sync_run = lambda job: records.update(q=copy.deepcopy(job))
        self.app.extract_source = AsyncMock()
        held = {"value": False}
        self.app._restore_hold = lambda: held["value"]

        async def install_hold(_when):
            held["value"] = True

        self.storage.setAlarm.side_effect = install_hold
        await self.app.alarm()
        self.storage.setAlarm.assert_awaited_once()
        self.app.extract_source.assert_not_awaited()
        self.assertEqual(records["q"]["status"], "RUNNING")
        self.assertGreater(records["q"]["lease_until"], time.time())
        self.assertEqual(records["q"]["stage_outputs"], [])

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
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == "mutate"
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
        records[run_id] = copy.deepcopy(job)

        held = {"value": False}

        async def hold_after_first_stage(*_):
            held["value"] = True
            return json.dumps(
                {
                    **envelopes[0],
                    "ok": True,
                    "model": "fixture-model",
                    "worker_version": "fixture-version",
                    "model_calls": 1,
                }
            )

        sdk.stage = AsyncMock(side_effect=hold_after_first_stage)
        with (
            patch.object(self.app, "_restore_hold", side_effect=lambda: held["value"]),
            patch.object(self.loaded, "import_from_javascript", return_value=sdk),
        ):
            await self.app.alarm()
        self.assertEqual(sdk.stage.await_count, 1)
        self.assertEqual(records[run_id]["status"], "RUNNING")
        self.assertEqual(records[run_id]["calls"][0]["outcome"], "RECEIVED")
        self.assertEqual(len(records[run_id]["stage_outputs"]), 1)
        records[run_id] = copy.deepcopy(job)

        async def reassigned_stage(*_):
            records[run_id]["lease_until"] += 1000
            records[run_id]["new_owner_marker"] = "fixture-owner"
            return json.dumps(
                {
                    **envelopes[0],
                    "ok": True,
                    "model": "fixture-model",
                    "worker_version": "fixture-version",
                    "model_calls": 1,
                }
            )

        sdk.stage = AsyncMock(side_effect=reassigned_stage)
        with patch.object(self.loaded, "import_from_javascript", return_value=sdk):
            await self.app.alarm()
        self.assertEqual(records[run_id]["new_owner_marker"], "fixture-owner")
        self.assertEqual(records[run_id]["calls"][0]["outcome"], "UNKNOWN")
        late = self.alarm_control.execute(
            "SELECT record FROM control WHERE id LIKE 'late-source-%'"
        ).fetchall()
        self.assertEqual(len(late), 1)
        self.assertEqual(json.loads(late[0][0])["response"]["model_calls"], 1)
        self.assertEqual(sdk.stage.await_count, 1)
        records[run_id] = copy.deepcopy(job)

        async def invalid_reassigned_stage(*_):
            records[run_id]["lease_until"] += 2000
            records[run_id]["new_owner_marker"] = "fixture-invalid-owner"
            return '{"ok":true}'

        sdk.stage = AsyncMock(side_effect=invalid_reassigned_stage)
        with patch.object(self.loaded, "import_from_javascript", return_value=sdk):
            await self.app.alarm()
        self.assertEqual(records[run_id]["new_owner_marker"], "fixture-invalid-owner")
        late = self.alarm_control.execute(
            "SELECT record FROM control WHERE id LIKE 'late-source-%'"
        ).fetchall()
        invalid = [
            json.loads(row[0])
            for row in late
            if json.loads(row[0]).get("outcome") == "UNKNOWN"
        ]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(
            invalid[0]["raw_sha256"], hashlib.sha256(b'{"ok":true}').hexdigest()
        )
