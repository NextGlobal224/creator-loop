"""Actual durable handler/alarm with SQLite and fake provider; not cloud ASR QA."""

import ast
import asyncio
import base64
import json
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from creator_loop.backend.creator.web_domain import WebDomain, WebInputError
from creator_loop.backend.knowledge.cloud_audio import (
    WHISPER_MODEL,
    probe_pcm_wav,
    store_cloud_transcript,
)
from creator_loop.backend.knowledge.cloud_image import (
    IMAGE_MODEL,
    probe_image,
    store_cloud_image,
)
from creator_loop.backend.runtime.database import _connect_write

from tests.backend import test_cloud_import_api
from tests.backend.test_cloud_audio import wav


class CloudSourceJobTests(unittest.TestCase):
    def setUp(self):
        fixture = test_cloud_import_api.CloudImportAPITests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.api = fixture, fixture.api
        namespace = self.api.authorized.__func__.__globals__
        namespace.update(
            WHISPER_MODEL=WHISPER_MODEL,
            IMAGE_MODEL=IMAGE_MODEL,
            probe_image=probe_image,
            store_cloud_image=store_cloud_image,
            probe_pcm_wav=probe_pcm_wav,
            store_cloud_transcript=store_cloud_transcript,
            python_to_rpc=lambda value: value,
            parse_qs=parse_qs,
            urlsplit=urlsplit,
        )
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        tree = ast.parse(source.read_text("utf8"))
        names = {"alarm", "extract_source", "save_job", "sync_run"}
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "valid_included_grant"
            ):
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]), str(source), "exec"
                    ),
                    namespace,
                )
            if isinstance(node, ast.ClassDef) and node.name == "Database":
                for method in node.body:
                    if (
                        isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name in names
                    ):
                        exec(
                            compile(
                                ast.Module(body=[method], type_ignores=[]),
                                str(source),
                                "exec",
                            ),
                            namespace,
                        )
                        setattr(type(self.api), method.name, namespace[method.name])
        self.alarms = []

        async def set_alarm(value):
            self.alarms.append(value)

        self.api.ctx.storage.setAlarm = set_alarm
        self.api.env.CREATOR_MODEL = "text-model"
        self.api.env.CREATOR_VERSION = SimpleNamespace(id="fixture-deployment")
        self.api.env.AI = object()
        self.project = fixture.call(
            {"title": "Isolated source job"}, path="/api/projects", key="a" * 32
        ).value["project_id"]
        self.file = fixture.call(
            {
                "project_id": self.project,
                "media_type": "AUDIO",
                "display_name": "Ghi âm.wav",
                "content_base64": base64.b64encode(wav()).decode(),
            },
            path="/api/materials",
            key="b" * 32,
        ).value["file_id"]
        self.request = {"project_id": self.project, "file_id": self.file}
        self.calls = 0
        self.response = {
            "text": "Đây là đoạn tiếng Việt.",
            "transcription_info": {"duration": 2, "language": "vi"},
            "segments": [
                {
                    "start": 0.1,
                    "end": 1.9,
                    "text": "Đây là đoạn tiếng Việt.",
                    "avg_logprob": -0.2,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.1,
                }
            ],
        }

        async def transcribe(binding, encoded, deployment):
            self.calls += 1
            packet = json.loads(encoded)
            self.assertEqual(deployment, "fixture-deployment")
            self.assertEqual(packet["model"], WHISPER_MODEL)
            self.assertEqual(base64.b64decode(packet["audio"]), wav())
            return json.dumps(
                {
                    "ok": True,
                    "model": WHISPER_MODEL,
                    "worker_version": deployment,
                    "request_id": packet["request_id"],
                    "input_sha256": packet["input_sha256"],
                    "model_calls": 1,
                    "native_usage": None,
                    "native_response": self.response,
                }
            )

        namespace["import_from_javascript"] = lambda name: SimpleNamespace(
            transcribeSource=transcribe
        )

    def grant(self):
        now = time.time()
        return self.fixture.call(
            {
                "grant": "B-finish-20261017-free",
                "observed_at": now,
                "expires_at": now + 240,
                "remaining_calls": 1,
                "model": WHISPER_MODEL,
                "account_plan": "Paid",
                "included_only": True,
                "max_extra_charge_usd": 0,
                "remaining_neurons": 9000,
                "estimated_chain_neurons": 5000,
            },
            path="/api/grant",
        )

    def enqueue(self):
        return self.fixture.call(
            dict(self.request), path="/api/source-jobs", key="c" * 32
        )

    def job(self):
        return json.loads(
            self.api.rows("SELECT record FROM jobs WHERE id=?", "c" * 32)[0]["record"]
        )

    def test_missing_grant_and_wrong_project_never_enqueue_or_call(self):
        with self.assertRaisesRegex(WebInputError, "operator đọc nguồn"):
            self.enqueue()
        self.grant()
        other = self.fixture.call(
            {"title": "Other Project"}, path="/api/projects", key="e" * 32
        ).value["project_id"]
        with self.assertRaisesRegex(WebInputError, "liên kết"):
            self.fixture.call(
                self.request | {"project_id": other},
                path="/api/source-jobs",
                key="d" * 32,
            )
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.api.rows("SELECT * FROM jobs"), [])

    def test_durable_source_run_finishes_once_and_keeps_unreviewed_native_artifact(
        self,
    ):
        self.grant()
        queued = self.enqueue()
        self.assertEqual(queued.value["status"], "QUEUED")
        self.assertTrue(self.alarms)
        self.assertNotIn('"audio":', json.dumps(self.job()))
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertEqual(self.calls, 1)
        self.assertIsNone(job["calls"][0]["measured_usage"])
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 1)
        self.grant()
        with self.assertRaisesRegex(WebInputError, "Đã có transcript"):
            self.fixture.call(dict(self.request), path="/api/source-jobs", key="d" * 32)
        root, _ = self.api.load()
        try:
            review = WebDomain(root).source_review(self.project, self.file)
            self.assertEqual(review["evidence_versions"][0]["review_state"], "PENDING")
            self.assertIsNone(review["source_use"])
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                sha = db.execute(
                    "SELECT sha256 FROM asset_files WHERE role='OTHER'"
                ).fetchone()[0]
            artifact = json.loads(self.api.object_bytes(sha))
            self.assertEqual(artifact["native_response"], self.response)
            self.assertEqual(review["latest_transcript_artifact"]["sha256"], sha)
        finally:
            self.api.discard_projection(root)
        result = asyncio.run(
            self.api.authorized(
                SimpleNamespace(
                    headers={},
                    url=f"https://fixture/api/source-review?project_id={self.project}&file_id={self.file}",
                ),
                "/api/source-review",
                "GET",
                {},
            )
        )
        self.assertEqual(
            result.value["source_extraction"]["parsed"]["text"], self.response["text"]
        )
        self.assertEqual(result.value["source_extraction"]["artifact_sha256"], sha)

    def test_expired_running_lease_is_unknown_and_never_retried(self):
        self.grant()
        self.enqueue()
        job = self.job()
        job.update(status="RUNNING", lease_until=time.time() - 1)
        self.api.save_job(job)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertTrue(self.job()["unknown_outcome"])

    def test_provider_unknown_keeps_call_reservation_and_never_retries(self):
        async def failure(binding, packet, deployment):
            self.calls += 1
            p = json.loads(packet)
            return json.dumps(
                {
                    "ok": False,
                    "model": WHISPER_MODEL,
                    "worker_version": deployment,
                    "request_id": p["request_id"],
                    "input_sha256": p["input_sha256"],
                    "unknown_outcome": True,
                    "model_calls": 1,
                    "native_usage": None,
                }
            )

        self.api.authorized.__func__.__globals__["import_from_javascript"] = (
            lambda name: SimpleNamespace(transcribeSource=failure)
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "FAILED")
        self.assertTrue(job["unknown_outcome"])
        self.assertEqual(job["calls"][0]["outcome"], "UNKNOWN")
        self.assertEqual(
            json.loads(
                self.api.rows("SELECT record FROM control WHERE id='grant'")[0][
                    "record"
                ]
            )["remaining_calls"],
            0,
        )
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 1)

    def test_cancel_at_alarm_await_never_dispatches_source(self):
        self.grant()
        self.enqueue()

        async def cancel_during_alarm(_):
            await self.api.authorized(
                SimpleNamespace(headers={}), "/api/cancel", "POST", {"job_id": "c" * 32}
            )

        self.api.ctx.storage.setAlarm = cancel_during_alarm
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(self.calls, 0)
        self.assertEqual(job["calls"], [])
        grant = json.loads(
            self.api.rows("SELECT record FROM control WHERE id='grant'")[0]["record"]
        )
        self.assertEqual(grant["remaining_calls"], 0)
        self.assertEqual(grant["reserved_pipeline"], job["id"])

    def test_changed_owner_at_alarm_await_does_not_overwrite_or_dispatch(self):
        self.grant()
        self.enqueue()

        async def replace_lease(_):
            current = self.job()
            current["lease_until"] += 30
            self.api.save_job(current)

        self.api.ctx.storage.setAlarm = replace_lease
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(self.job()["calls"], [])

    def test_changed_grant_at_alarm_await_does_not_dispatch_or_refund(self):
        self.grant()
        self.enqueue()

        async def replace_grant(_):
            grant = json.loads(
                self.api.rows("SELECT record FROM control WHERE id='grant'")[0][
                    "record"
                ]
            )
            grant["reserved_pipeline"] = "d" * 32
            self.api.sql.exec(
                "UPDATE control SET record=? WHERE id='grant'", json.dumps(grant)
            )

        self.api.ctx.storage.setAlarm = replace_grant
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.job()["status"], "FAILED")
        current = json.loads(
            self.api.rows("SELECT record FROM control WHERE id='grant'")[0]["record"]
        )
        self.assertEqual(current["reserved_pipeline"], "d" * 32)
        self.assertEqual(current["remaining_calls"], 0)

    def test_owner_change_during_native_call_preserves_late_receipt(self):
        namespace = self.api.authorized.__func__.__globals__
        image = (
            self.job_kind_is_image() if hasattr(self, "job_kind_is_image") else False
        )
        name = "analyzeSourceImage" if image else "transcribeSource"
        original = getattr(namespace["import_from_javascript"]("creator_ai.mjs"), name)

        async def changed_owner(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            current = self.job()
            current["lease_until"] += 30
            self.api.save_job(current)
            return response

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            **{name: changed_owner}
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual((job["status"], self.calls), ("RUNNING", 1))
        self.assertEqual(job["calls"][0]["outcome"], "UNKNOWN")
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        receipt = json.loads(late[0]["record"])
        self.assertEqual(receipt["response"]["native_response"], self.response)
        self.assertEqual(receipt["call"]["outcome"], "RECEIVED")
        base = "/api/source-receipts/" + job["id"]
        denied = asyncio.run(
            self.api.authorized(SimpleNamespace(headers={}), base, "GET", {})
        )
        self.assertEqual(denied.status, 403)
        operator = SimpleNamespace(
            headers={"X-Operator-Key": "fixture"}, url="https://fixture" + base
        )
        listing = asyncio.run(self.api.authorized(operator, base, "GET", {})).value
        self.assertIsNone(listing["next_cursor"])
        self.assertEqual(len(listing["ids"]), 1)
        reopened = asyncio.run(
            self.api.authorized(operator, base + "/" + listing["ids"][0], "GET", {})
        ).value
        self.assertEqual(reopened["response"]["native_response"], self.response)
        for i in range(51):
            self.api.sql.exec(
                "INSERT INTO control VALUES(?,?)",
                "late-source-" + job["id"] + "-" + format(i, "064x"),
                "{}",
            )
        page = asyncio.run(self.api.authorized(operator, base, "GET", {})).value
        self.assertEqual(len(page["ids"]), 50)
        operator.url = "https://fixture" + base + "?after=" + page["next_cursor"]
        last = asyncio.run(self.api.authorized(operator, base, "GET", {})).value
        self.assertEqual(len(last["ids"]), 2)
        self.assertIsNone(last["next_cursor"])
        self.assertFalse(set(page["ids"]) & set(last["ids"]))

    def test_operator_can_discover_late_receipts_across_unknown_jobs(self):
        path = "/api/source-receipts"
        denied = asyncio.run(
            self.api.authorized(SimpleNamespace(headers={}), path, "GET", {})
        )
        self.assertEqual(denied.status, 403)
        for i in range(51):
            key = "late-source-" + format(i, "032x") + "-" + format(i, "064x")
            self.api.sql.exec("INSERT INTO control VALUES(?,?)", key, "{}")
        operator = SimpleNamespace(
            headers={"X-Operator-Key": "fixture"}, url="https://fixture" + path
        )
        first = asyncio.run(self.api.authorized(operator, path, "GET", {})).value
        self.assertEqual(len(first["ids"]), 50)
        operator.url += "?after=" + first["next_cursor"]
        last = asyncio.run(self.api.authorized(operator, path, "GET", {})).value
        self.assertEqual(len(last["ids"]), 1)
        self.assertIsNone(last["next_cursor"])
        self.assertFalse(set(first["ids"]) & set(last["ids"]))
        operator.url = "https://fixture" + path + "?after=invalid"
        invalid = asyncio.run(self.api.authorized(operator, path, "GET", {}))
        self.assertEqual(invalid.status, 400)

        job_id = "f" * 32
        self.api.sql.exec(
            "INSERT INTO control VALUES(?,?)", "late-source-" + job_id + "-bad", "{}"
        )
        per_job = path + "/" + job_id
        operator.url = "https://fixture" + per_job
        malformed = asyncio.run(self.api.authorized(operator, per_job, "GET", {}))
        self.assertEqual(malformed.status, 409)

    def test_changed_grant_during_native_error_preserves_current_record(self):
        namespace = self.api.authorized.__func__.__globals__
        image = (
            self.job_kind_is_image() if hasattr(self, "job_kind_is_image") else False
        )
        name = "analyzeSourceImage" if image else "transcribeSource"

        async def changed_owner_then_error(*_):
            self.calls += 1
            current = self.job()
            current["grant"]["reserved_pipeline"] = "d" * 32
            self.api.save_job(current)
            raise RuntimeError("fixture provider failure")

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            **{name: changed_owner_then_error}
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        current = self.job()
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["grant"]["reserved_pipeline"], "d" * 32)
        self.assertEqual(current["calls"][0]["outcome"], "UNKNOWN")
        self.assertEqual(self.calls, 1)

    def test_metadata_change_during_native_call_keeps_new_record_and_late_receipt(self):
        namespace = self.api.authorized.__func__.__globals__
        image = (
            self.job_kind_is_image() if hasattr(self, "job_kind_is_image") else False
        )
        name = "analyzeSourceImage" if image else "transcribeSource"
        original = getattr(namespace["import_from_javascript"]("creator_ai.mjs"), name)

        async def changed_metadata(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            current = self.job()
            current["operator_note"] = "preserve-new-metadata"
            self.api.save_job(current)
            return response

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            **{name: changed_metadata}
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["operator_note"], "preserve-new-metadata")
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(
            len(self.api.rows("SELECT id FROM control WHERE id LIKE 'late-source-%'")),
            1,
        )

    def test_missing_status_during_native_call_keeps_receipt_separately(self):
        namespace = self.api.authorized.__func__.__globals__
        image = (
            self.job_kind_is_image() if hasattr(self, "job_kind_is_image") else False
        )
        name = "analyzeSourceImage" if image else "transcribeSource"
        original = getattr(namespace["import_from_javascript"]("creator_ai.mjs"), name)

        async def malformed_record(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            current = self.job()
            current.pop("status")
            self.api.sql.exec(
                "UPDATE jobs SET record=? WHERE id=?",
                json.dumps(current),
                current["id"],
            )
            return response

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            **{name: malformed_record}
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertNotIn("status", self.job())
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(
            json.loads(late[0]["record"])["response"]["native_response"], self.response
        )

    def test_owner_change_and_projection_failure_does_not_overwrite_new_owner(self):
        self.grant()
        self.enqueue()

        async def replace_lease_then_break_projection(_):
            current = self.job()
            current["lease_until"] += 30
            self.api.save_job(current)

            def fail_load():
                raise RuntimeError("fixture projection failed")

            self.api.load = fail_load

        self.api.ctx.storage.setAlarm = replace_lease_then_break_projection
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(self.job()["calls"], [])

    def test_cancel_during_call_retains_native_receipt_but_creates_no_evidence(self):
        namespace = self.api.authorized.__func__.__globals__
        original = namespace["import_from_javascript"](
            "creator_ai.mjs"
        ).transcribeSource

        async def cancelled(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            await self.api.authorized(
                SimpleNamespace(headers={}), "/api/cancel", "POST", {"job_id": "c" * 32}
            )
            return response

        namespace["import_from_javascript"] = lambda name: SimpleNamespace(
            transcribeSource=cancelled
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(self.calls, 1)
        self.assertEqual(job["stage_outputs"][0]["native_response"], self.response)
        root, _ = self.api.load()
        try:
            self.assertEqual(
                WebDomain(root).source_review(self.project, self.file)[
                    "evidence_versions"
                ],
                [],
            )
        finally:
            self.api.discard_projection(root)

    def test_cancelled_unknown_response_keeps_unknown_and_reservation(self):
        namespace = self.api.authorized.__func__.__globals__
        image = (
            self.job_kind_is_image() if hasattr(self, "job_kind_is_image") else False
        )
        name = "analyzeSourceImage" if image else "transcribeSource"

        async def cancelled_unknown(*_):
            self.calls += 1
            await self.api.authorized(
                SimpleNamespace(headers={}), "/api/cancel", "POST", {"job_id": "c" * 32}
            )
            return json.dumps(
                {"unknown_outcome": True, "model_calls": 1, "native_usage": None}
            )

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            **{name: cancelled_unknown}
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "CANCELLED")
        self.assertTrue(job["unknown_outcome"])
        self.assertEqual(job["calls"][0]["outcome"], "UNKNOWN")
        grant = json.loads(
            self.api.rows("SELECT record FROM control WHERE id='grant'")[0]["record"]
        )
        self.assertEqual(grant["remaining_calls"], 0)

    def test_enqueue_idempotency_and_single_active_job(self):
        self.grant()
        self.enqueue()
        self.assertEqual(self.enqueue().value["id"], "c" * 32)
        with self.assertRaisesRegex(WebInputError, "đang chạy"):
            self.fixture.call(dict(self.request), path="/api/source-jobs", key="d" * 32)
        self.assertEqual(self.calls, 0)
        self.assertEqual(len(self.api.rows("SELECT * FROM jobs")), 1)

    def test_late_response_after_expired_lease_keeps_receipt_without_reviving_job(self):
        namespace = self.api.authorized.__func__.__globals__
        original = namespace["import_from_javascript"](
            "creator_ai.mjs"
        ).transcribeSource

        async def late(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            job = self.job()
            job["lease_until"] = time.time() - 1
            self.api.save_job(job)
            await self.api.alarm()
            return response

        namespace["import_from_javascript"] = lambda name: SimpleNamespace(
            transcribeSource=late
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.calls, 1)
        late_records = self.api.rows(
            "SELECT record FROM control WHERE id LIKE 'late-source-%'"
        )
        self.assertEqual(len(late_records), 1)
        self.assertEqual(
            json.loads(late_records[0]["record"])["response"]["native_response"],
            self.response,
        )
        self.assertTrue(self.job()["unknown_outcome"])
        root, _ = self.api.load()
        try:
            self.assertEqual(
                WebDomain(root).source_review(self.project, self.file)[
                    "evidence_versions"
                ],
                [],
            )
        finally:
            self.api.discard_projection(root)

    def test_delayed_queued_alarm_cannot_use_expired_included_grant(self):
        self.grant()
        self.enqueue()
        job = self.job()
        job["grant"]["observed_at"] = time.time() - 121
        self.api.save_job(job)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertIn("hết hạn", self.job()["message"])
        self.assertEqual(self.job()["calls"], [])
        self.assertEqual(self.job()["grant"]["remaining_calls"], 0)

    def test_restore_interrupted_source_run_cannot_leave_ownerless_pending_work(self):
        self.grant()
        self.enqueue()
        backup = self.fixture.call(path="/api/backup").value
        self.fixture.call({"job_id": "c" * 32}, path="/api/cancel")
        revision = self.api.rows("SELECT revision FROM checkpoint")[0]["revision"]
        self.fixture.call(
            {
                "backup": backup,
                "expected_revision": revision,
                "confirm_data_after_backup_may_be_lost": True,
            },
            path="/api/restore",
            key="e" * 32,
        )
        root, _ = self.api.load()
        try:
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                run = db.execute(
                    "SELECT status,error_code FROM processing_runs WHERE run_id=?",
                    ("c" * 32,),
                ).fetchone()
                self.assertEqual(run, ("FAILED", "RESTORED_INTERRUPTED_WEB_RUN"))
        finally:
            self.api.discard_projection(root)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 0)
