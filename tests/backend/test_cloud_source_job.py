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
        names = {"alarm", "transcribe_source", "save_job", "sync_run"}
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
        with self.assertRaisesRegex(WebInputError, "operator ASR"):
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
        self.assertEqual(
            self.job()["stage_outputs"][0]["native_response"], self.response
        )
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
