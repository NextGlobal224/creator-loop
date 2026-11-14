"""Actual durable handler/alarm with SQLite and fake provider; not cloud ASR QA."""

import ast
import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from creator_loop.backend.creator.web_domain import WebDomain, WebInputError
from creator_loop.backend.knowledge.audio_chunk_journal import (
    create_chunk_journal,
    reserve_next_chunk,
    store_completed_chunk_journal,
)
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
from tests.backend.test_audio_chunk_journal import fake_response, pcm
from tests.backend.test_audio_playback import m4a
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
            # Exercises the legacy fake-provider path only; this is not a
            # quota proof and the deployed function always fails closed.
            source_audio_quota_verified=lambda env, job: True,
        )
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        tree = ast.parse(source.read_text("utf8"))
        names = {
            "alarm",
            "extract_source",
            "save_job",
            "sync_run",
            "fetch",
            "authenticated",
            "retain_audio_chunk_response",
        }
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
        original_version = result.value["evidence_versions"][0]["evidence_version_id"]
        corrected = self.fixture.call(
            {
                "project_id": self.project,
                "file_id": self.file,
                "evidence_version_id": original_version,
                "start_ms": 100,
                "end_ms": 1900,
                "content": "Đã nghe đối chiếu; tên riêng chưa rõ.",
                "reason": "Nghe lại bản gốc",
            },
            path="/api/source-audio-ranges",
            key="f" * 32,
        )
        self.assertEqual(corrected.value["review_state"], "PENDING")
        root, _ = self.api.load()
        try:
            reopened = WebDomain(root).source_review(self.project, self.file)
            versions = reopened["evidence_versions"]
            self.assertEqual([v["producer_type"] for v in versions], ["MODEL", "HUMAN"])
            self.assertEqual(versions[0]["content"], self.response["text"])
            self.assertEqual(
                versions[1]["content"], "Đã nghe đối chiếu; tên riêng chưa rõ."
            )
            self.assertIsNone(reopened["source_use"])
        finally:
            self.api.discard_projection(root)
        self.assertEqual(self.calls, 1)

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

    def test_unverified_audio_quota_waits_without_call_or_automatic_retry(self):
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        tree = ast.parse(source.read_text("utf8"))
        verifier = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "source_audio_quota_verified"
        )
        local = {}
        exec(
            compile(ast.Module(body=[verifier], type_ignores=[]), str(source), "exec"),
            local,
        )
        self.assertFalse(
            local["source_audio_quota_verified"](
                SimpleNamespace(), {"remaining": 10**9}
            )
        )
        self.grant()
        namespace = self.api.authorized.__func__.__globals__
        namespace["source_audio_quota_verified"] = lambda env, job: False
        alarms_before = list(self.alarms)
        rejected = self.enqueue()
        self.assertEqual(rejected.status, 409)
        self.assertEqual(rejected.value["code"], "AUDIO_QUOTA_UNVERIFIED")
        self.assertEqual(self.alarms, alarms_before)
        self.assertEqual(self.api.rows("SELECT * FROM jobs"), [])
        namespace["source_audio_quota_verified"] = lambda env, job: True
        self.enqueue()
        namespace["source_audio_quota_verified"] = lambda env, job: False
        asyncio.run(self.api.alarm())
        waiting = self.job()
        self.assertEqual(waiting["status"], "WAITING_QUOTA")
        self.assertEqual(waiting["calls"], [])
        self.assertEqual(waiting["grant"]["reserved_pipeline"], "c" * 32)
        self.assertEqual(self.calls, 0)
        alarms = list(self.alarms)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job(), waiting)
        self.assertEqual(self.alarms, alarms)
        rejected = self.fixture.call(
            dict(self.request), path="/api/source-jobs", key="d" * 32
        )
        self.assertEqual(rejected.value["code"], "AUDIO_QUOTA_UNVERIFIED")
        cancelled = self.fixture.call({"job_id": "c" * 32}, path="/api/cancel")
        self.assertEqual(cancelled.value["status"], "CANCELLED")
        self.assertEqual(self.calls, 0)

    def test_actual_worker_provider_callback_retains_reserved_chunk_after_wait(self):
        """Fake binding; actual alarm, provider callback and DO receipt adapter."""
        self.grant()
        self.enqueue()
        job = self.job()
        raw = wav()
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE control(id TEXT PRIMARY KEY,record TEXT NOT NULL)")
            create_chunk_journal(
                db,
                run_id=job["id"],
                source=raw,
                decoded_chunks=(raw,),
                duration_ms=probe_pcm_wav(raw).duration_ms,
                decoder="fixture WAV identity",
                model=WHISPER_MODEL,
                deployment="fixture-deployment",
            )
            packet = reserve_next_chunk(db, run_id=job["id"], source=raw, chunk=raw)
            journal = db.execute(
                "SELECT record FROM control WHERE id=?", ("audio-chunks-" + job["id"],)
            ).fetchone()[0]
        job["audio_chunk_receipt"] = packet
        job["sources"][0]["request_id"] = packet["request_id"]
        with self.fixture.coord:
            self.api.sql.exec(
                "INSERT INTO control VALUES(?,?)", "audio-chunks-" + job["id"], journal
            )
            self.api.save_job(job)
        namespace = self.api.authorized.__func__.__globals__
        original_provider = namespace["import_from_javascript"](
            "creator_ai.mjs"
        ).transcribeSource

        async def fake_provider(binding, encoded, deployment):
            result = await original_provider(binding, encoded, deployment)
            waiting = self.job()
            waiting["status"] = "WAITING_QUOTA"
            self.api.save_job(waiting)
            return result

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            transcribeSource=fake_provider
        )
        asyncio.run(self.api.alarm())
        journal = json.loads(
            self.api.rows(
                "SELECT record FROM control WHERE id=?", "audio-chunks-" + job["id"]
            )[0]["record"]
        )
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.job()["status"], "WAITING_QUOTA")
        self.assertEqual(journal["chunks"][0]["state"], "RECEIVED")
        self.assertTrue(
            journal["chunks"][0]["partial_artifact"]["evidence_version_ids"]
        )

    def test_partial_compressed_artifact_edit_and_relogin_through_worker(self):
        """Real Worker handlers; synthetic job/receipts, no cloud or AI."""
        self.fixture.coord.executescript(
            "CREATE TABLE sessions(hash TEXT PRIMARY KEY,expires REAL NOT NULL);"
            "CREATE TABLE attempts(ip TEXT PRIMARY KEY,started REAL NOT NULL,count INTEGER NOT NULL);"
        )
        namespace = self.api.authorized.__func__.__globals__
        namespace.update(
            secrets=secrets,
            MAX_BODY=16 * 1024 * 1024,
            HEADERS={},
            reply=lambda value, status=200, headers=None: SimpleNamespace(
                value=value, status=status, headers=headers or {}
            ),
        )
        self.api.env.LOGIN_PASSWORD_HASH = hashlib.sha256(
            b"fixture-password"
        ).hexdigest()
        source = m4a()
        item = self.fixture.call(
            {
                "project_id": self.project,
                "media_type": "AUDIO",
                "display_name": "sample.m4a",
                "content_base64": base64.b64encode(source).decode(),
            },
            path="/api/materials",
            key="1" * 32,
        ).value
        file_id, run_id = item["file_id"], "e" * 32
        chunk = pcm(2)

        def response(packet):
            value = fake_response(packet)
            value["native_response"]["transcription_info"]["duration"] = 2
            value["native_response"]["segments"][0]["end"] = 2
            return value

        root, revision = self.api.load()
        try:
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                db.execute(
                    "CREATE TEMP TABLE control(id TEXT PRIMARY KEY,record TEXT NOT NULL)"
                )
                db.execute(
                    """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,
                         status,tool_name,tool_version,model_name,started_at,created_at)
                       VALUES(?,?,?,'SOURCE_TRANSCRIPTION','RUNNING',
                         'CLOUDFLARE_WORKERS_AI_SOURCE','fixture',?,
                         '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')""",
                    (run_id, item["asset_id"], file_id, WHISPER_MODEL),
                )
                db.commit()
                create_chunk_journal(
                    db,
                    run_id=run_id,
                    source=source,
                    decoded_chunks=(chunk, chunk),
                    duration_ms=4000,
                    decoder="fixture",
                    model=WHISPER_MODEL,
                    deployment="fixture-deployment",
                )
                first = reserve_next_chunk(
                    db, run_id=run_id, source=source, chunk=chunk
                )
                journal = db.execute(
                    "SELECT record FROM control WHERE id=?", ("audio-chunks-" + run_id,)
                ).fetchone()[0]

            def commit_fixture():
                self.api.commit(root, revision)
                self.api.sql.exec(
                    "INSERT INTO control VALUES(?,?)", "audio-chunks-" + run_id, journal
                )
                self.api.save_job(
                    {
                        "id": run_id,
                        "kind": "SOURCE_TRANSCRIPTION",
                        "status": "RUNNING",
                        "request": {"project_id": self.project, "file_id": file_id},
                        "sources": [{"file_id": file_id}],
                    }
                )

            self.api.ctx.storage.transactionSync(commit_fixture)
        finally:
            self.api.discard_projection(root)

        # Fake-provider response goes through the real Worker internal callback.
        put_object = self.api.put_object
        self.api.put_object = lambda *_: (_ for _ in ()).throw(
            ValueError("fixture capacity")
        )
        try:
            with self.assertRaisesRegex(ValueError, "fixture capacity"):
                self.api.retain_audio_chunk_response(run_id, response(first))
        finally:
            self.api.put_object = put_object
        retained_journal = json.loads(
            self.api.rows(
                "SELECT record FROM control WHERE id=?", "audio-chunks-" + run_id
            )[0]["record"]
        )
        self.assertEqual(retained_journal["chunks"][0]["state"], "RECEIVED")
        self.assertNotIn("partial_artifact", retained_journal["chunks"][0])
        # Replay a retained receipt locally, never the provider request.
        received = self.api.retain_audio_chunk_response(run_id, response(first))
        self.assertEqual(received["outcome"], "RECEIVED")
        partial = received["partial"]
        # The future chunk-reservation sequencer is not wired yet; establish a
        # synthetic already-dispatched request without calling a real provider.
        journal = self.api.rows(
            "SELECT record FROM control WHERE id=?", "audio-chunks-" + run_id
        )[0]["record"]
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE control(id TEXT PRIMARY KEY,record TEXT NOT NULL)")
            db.execute(
                "INSERT INTO control VALUES(?,?)", ("audio-chunks-" + run_id, journal)
            )
            db.commit()
            second = reserve_next_chunk(db, run_id=run_id, source=source, chunk=chunk)
            journal = db.execute(
                "SELECT record FROM control WHERE id=?", ("audio-chunks-" + run_id,)
            ).fetchone()[0]
        with self.fixture.coord:
            self.fixture.coord.execute(
                "UPDATE control SET record=? WHERE id=?",
                (journal, "audio-chunks-" + run_id),
            )

        class Request:
            def __init__(self, path, method="GET", data=None, cookie=""):
                self.url = "https://fixture" + path
                self.method = method
                self.headers = {
                    "Origin": "https://fixture",
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "Idempotency-Key": "f" * 32,
                }
                self.data = data or {}

            async def text(self):
                return json.dumps(self.data)

        async def fetch(path, method="GET", data=None, cookie=""):
            return await self.api.fetch(Request(path, method, data, cookie))

        query = f"/api/source-review?project_id={self.project}&file_id={file_id}"
        self.assertEqual(asyncio.run(fetch(query)).status, 401)
        login = asyncio.run(
            fetch("/api/login", "POST", {"password": "fixture-password"})
        )
        self.assertEqual(login.status, 200)
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        before = asyncio.run(fetch(query, cookie=cookie))
        version = partial["evidence_version_ids"][0]
        self.assertTrue(
            any(
                v["evidence_version_id"] == version
                for v in before.value["evidence_versions"]
            )
        )
        waiting_job = json.loads(
            self.api.rows("SELECT record FROM jobs WHERE id=?", run_id)[0]["record"]
        )
        # Synthetic sequencer leaves an in-flight UNKNOWN second request;
        # the actual alarm must move this job into a non-retrying quota wait.
        waiting_job["status"] = "QUEUED"
        with self.fixture.coord:
            self.api.save_job(waiting_job)
        namespace["source_audio_quota_verified"] = lambda env, job: False
        alarms_before_wait = list(self.alarms)
        asyncio.run(self.api.alarm())
        self.assertEqual(
            self.api.rows("SELECT status FROM jobs WHERE id=?", run_id)[0]["status"],
            "WAITING_QUOTA",
        )
        self.assertEqual(self.alarms, alarms_before_wait)
        edit_data = {
            "project_id": self.project,
            "file_id": file_id,
            "evidence_version_id": version,
            "start_ms": 100,
            "end_ms": 1900,
            "content": "Đã sửa",
            "reason": "Nghe đối chiếu",
        }
        with self.fixture.coord:
            self.fixture.coord.execute(
                "UPDATE jobs SET status='RUNNING' WHERE id=?", (run_id,)
            )
        running_edit = asyncio.run(
            fetch("/api/source-audio-ranges", "POST", edit_data, cookie)
        )
        self.assertEqual(running_edit.status, 409)
        with self.fixture.coord:
            self.fixture.coord.execute(
                "UPDATE jobs SET status='WAITING_QUOTA' WHERE id=?", (run_id,)
            )
        blocked = asyncio.run(
            fetch(
                "/api/source-use",
                "POST",
                {"project_id": self.project, "file_id": file_id},
                cookie,
            )
        )
        self.assertEqual(blocked.status, 409)
        stale_root, stale_revision = self.api.load()
        edited = asyncio.run(
            fetch(
                "/api/source-audio-ranges",
                "POST",
                edit_data,
                cookie,
            )
        )
        self.assertEqual(edited.status, 200, edited.value)
        self.assertEqual(
            self.api.rows("SELECT status FROM jobs WHERE id=?", run_id)[0]["status"],
            "WAITING_QUOTA",
        )
        # A late writer that read before the correction cannot publish its
        # stale projection. It must load the latest state before reconciling.
        try:
            with self.assertRaisesRegex(
                RuntimeError, "Concurrent durable revision changed"
            ):
                self.api.ctx.storage.transactionSync(
                    lambda: self.api.commit(stale_root, stale_revision)
                )
        finally:
            self.api.discard_projection(stale_root)
        late = self.api.retain_audio_chunk_response(run_id, response(second))
        self.assertEqual(late["outcome"], "RECEIVED")
        duplicate = self.api.retain_audio_chunk_response(run_id, response(second))
        self.assertEqual(duplicate["partial"]["sha256"], late["partial"]["sha256"])
        conflict = response(second)
        conflict["native_response"]["text"] = "conflicting result"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.api.retain_audio_chunk_response(run_id, conflict)
        root, revision = self.api.load()
        try:
            # Finalization remains synthetic; the real adapter must hydrate
            # the immutable artifact before calling this journal function.
            retained = root / ("storage/derived/" + partial["sha256"])
            retained.parent.mkdir(parents=True, exist_ok=True)
            retained.write_bytes(self.api.object_bytes(partial["sha256"]))
            (root / ("storage/derived/" + late["partial"]["sha256"])).write_bytes(
                self.api.object_bytes(late["partial"]["sha256"])
            )
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                db.execute(
                    "CREATE TEMP TABLE control(id TEXT PRIMARY KEY,record TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO control VALUES(?,?)",
                    (
                        "audio-chunks-" + run_id,
                        self.api.rows(
                            "SELECT record FROM control WHERE id=?",
                            "audio-chunks-" + run_id,
                        )[0]["record"],
                    ),
                )
                db.commit()
                summary = store_completed_chunk_journal(
                    db, data_root=root, run_id=run_id, file_id=file_id
                )

            def commit_late():
                self.api.put_object(summary["sha256"], summary["content"])
                self.api.commit(root, revision)

            self.api.ctx.storage.transactionSync(commit_late)
        finally:
            self.api.discard_projection(root)
        login_again = asyncio.run(
            fetch("/api/login", "POST", {"password": "fixture-password"})
        )
        cookie_again = login_again.headers["Set-Cookie"].split(";", 1)[0]
        after = asyncio.run(fetch(query, cookie=cookie_again))
        versions = {
            v["evidence_version_id"]: v for v in after.value["evidence_versions"]
        }
        self.assertEqual(
            versions[edited.value["evidence_version_id"]]["content"], "Đã sửa"
        )
        corrected = versions[edited.value["evidence_version_id"]]
        self.assertEqual(
            max(
                (
                    v
                    for v in versions.values()
                    if v["evidence_id"] == corrected["evidence_id"]
                ),
                key=lambda v: v["version_no"],
            )["evidence_version_id"],
            corrected["evidence_version_id"],
        )
        self.assertEqual(versions[version]["producer_type"], "MODEL")
        self.assertEqual(len(versions), 3)
        self.assertEqual(
            after.value["source_extraction"]["artifact_sha256"], summary["sha256"]
        )
        self.assertEqual(self.calls, 0)

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

    def test_hold_during_call_retains_receipt_without_finalizing(self):
        namespace = self.api.authorized.__func__.__globals__
        original = namespace["import_from_javascript"](
            "creator_ai.mjs"
        ).transcribeSource

        async def held(binding, packet, deployment):
            response = await original(binding, packet, deployment)
            self.api._restore_hold = lambda: True
            return response

        namespace["import_from_javascript"] = lambda _: SimpleNamespace(
            transcribeSource=held
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "RUNNING")
        self.assertEqual(self.calls, 1)
        self.assertEqual(job["calls"][0]["outcome"], "RECEIVED")
        self.assertEqual(job["stage_outputs"][0]["native_response"], self.response)
        self.assertNotIn("evidence_version_ids", job)

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
