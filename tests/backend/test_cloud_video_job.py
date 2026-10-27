"""Actual durable video handler/alarm with protocol doubles; no real decoding."""

import ast
import asyncio
import base64
import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from creator_loop.backend.creator.web_domain import WebDomain, WebInputError
from creator_loop.backend.knowledge.cloud_video import (
    VIDEO_MODEL,
    original_video,
    store_cloud_video,
)

from tests.backend import test_cloud_source_job
from tests.backend.test_cloud_audio import wav
from tests.backend.test_cloud_image import png


class CloudVideoJobTests(unittest.TestCase):
    def setUp(self):
        h = test_cloud_source_job.CloudSourceJobTests("runTest")
        h.setUp()
        self.addCleanup(h.doCleanups)
        self.h = h
        self.api = h.api
        self.fixture = h.fixture
        ns = self.api.authorized.__func__.__globals__
        ns.update(
            VIDEO_MODEL=VIDEO_MODEL,
            original_video=original_video,
            store_cloud_video=store_cloud_video,
        )
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        for node in ast.parse(source.read_text("utf8")).body:
            if isinstance(node, ast.FunctionDef) and node.name == "valid_video_grant":
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]), str(source), "exec"
                    ),
                    ns,
                )
            if isinstance(node, ast.ClassDef) and node.name == "Database":
                for method in node.body:
                    if (
                        isinstance(method, ast.AsyncFunctionDef)
                        and method.name == "extract_video"
                    ):
                        exec(
                            compile(
                                ast.Module(body=[method], type_ignores=[]),
                                str(source),
                                "exec",
                            ),
                            ns,
                        )
                        setattr(type(self.api), "extract_video", ns["extract_video"])
        self.api.env.BROWSER = object()
        self.calls = []
        self.cancel_decode = False
        self.cancel_audio = False
        self.fail_audio = False
        self.fail_decode_close = False
        raw = b"\0\0\0\x18ftypisom" + b"\0" * 32
        self.original_sha = hashlib.sha256(raw).hexdigest()
        self.file = self.fixture.call(
            {
                "project_id": h.project,
                "display_name": "Video protocol.mp4",
                "media_type": "VIDEO",
                "content_base64": base64.b64encode(raw).decode(),
            },
            path="/api/materials",
            key="d" * 32,
        ).value["file_id"]
        self.request = {"project_id": h.project, "file_id": self.file}
        self.key = "f" * 32

        async def cancel():
            await self.api.authorized(
                SimpleNamespace(headers={}), "/api/cancel", "POST", {"job_id": self.key}
            )

        async def decode(binding, encoded):
            self.assertEqual(self.job()["browser"]["outcome"], "UNKNOWN")
            self.assertEqual(json.loads(encoded)["browser_reserved_seconds"], 60)
            if self.cancel_decode:
                await cancel()
            return json.dumps(
                {
                    "ok": not self.fail_decode_close,
                    "browser_lifecycle": {
                        "phase": "DECODE",
                        "close_status": "UNKNOWN"
                        if self.fail_decode_close
                        else "CLOSED",
                        "session_id": "fixture-owned-session",
                    },
                    "input_sha256": self.original_sha,
                    "input_bytes": len(raw),
                    "duration_ms": 2000,
                    "audio_duration_ms": 2000,
                    "audio": base64.b64encode(wav()).decode(),
                    "frames": [
                        {
                            "at_ms": 0,
                            "end_ms": 1,
                            "image": base64.b64encode(png()).decode(),
                        }
                    ],
                    "browser_session_id": "fixture-owned-session",
                    "browser_reserved_seconds": 60,
                    "visual_sampling_ms": 2000,
                }
            )

        async def audio(binding, encoded, deployment):
            self.calls.append("audio")
            p = json.loads(encoded)
            self.assertEqual(self.job()["calls"][-1]["outcome"], "UNKNOWN")
            if self.cancel_audio:
                await cancel()
            return json.dumps(
                {
                    "ok": not self.fail_audio,
                    "model": h.api.authorized.__func__.__globals__["WHISPER_MODEL"],
                    "request_id": p["request_id"],
                    "worker_version": deployment,
                    "input_sha256": p["input_sha256"],
                    "model_calls": 1,
                    "native_usage": None if self.fail_audio else {"neurons": 1},
                    "unknown_outcome": self.fail_audio,
                    "native_response": h.response,
                }
            )

        async def vision(binding, encoded, deployment):
            self.calls.append("vision")
            p = json.loads(encoded)
            return json.dumps(
                {
                    "ok": True,
                    "model": ns["IMAGE_MODEL"],
                    "request_id": p["request_id"],
                    "worker_version": deployment,
                    "input_sha256": p["input_sha256"],
                    "model_calls": 1,
                    "native_usage": {"neurons": 2},
                    "native_response": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "frames": [
                                                {
                                                    "at_ms": 0,
                                                    "description": "Mảng màu xanh.",
                                                    "visible_text": "",
                                                    "uncertain": ["Fixture"],
                                                    "unread": [],
                                                }
                                            ]
                                        }
                                    )
                                },
                            }
                        ]
                    },
                }
            )

        ns["import_from_javascript"] = lambda name: SimpleNamespace(
            decodeSourceVideo=decode, transcribeSource=audio, analyzeVideoFrames=vision
        )

    def grant(self, **changes):
        now = time.time()
        data = {
            "grant": "B-finish-20261017-free",
            "observed_at": now,
            "expires_at": now + 240,
            "remaining_calls": 2,
            "model": self.api.authorized.__func__.__globals__["WHISPER_MODEL"],
            "account_plan": "Paid",
            "included_only": True,
            "max_extra_charge_usd": 0,
            "remaining_neurons": 8000,
            "estimated_chain_neurons": 5000,
            "source_kind": "VIDEO",
            "vision_model": self.api.authorized.__func__.__globals__["IMAGE_MODEL"],
            "browser_included_only": True,
            "browser_observed_at": now,
            "browser_remaining_seconds": 1000,
            "browser_forecast_seconds": 600,
            **changes,
        }
        return self.fixture.call(data, path="/api/grant")

    def enqueue(self):
        return self.fixture.call(self.request, path="/api/source-jobs", key=self.key)

    def job(self):
        return json.loads(
            self.api.rows("SELECT record FROM jobs WHERE id=?", self.key)[0]["record"]
        )

    def test_real_handler_two_fixed_calls_and_durable_immutable_result(self):
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertEqual(self.calls, ["audio", "vision"])
        self.assertEqual([c["measured_usage"]["neurons"] for c in job["calls"]], [1, 2])
        self.assertNotIn('"image":', json.dumps(job))
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, ["audio", "vision"])
        root, _ = self.api.load()
        try:
            review = WebDomain(root).source_review(self.h.project, self.file)
            self.assertEqual(len(review["evidence_versions"]), 2)
            self.assertIsNone(review["source_use"])
        finally:
            self.api.discard_projection(root)

    def test_cancel_during_decoder_never_resurrects_job_or_calls_model(self):
        self.cancel_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "CANCELLED")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job()["browser"]["outcome"], "RECEIVED")

    def test_cancel_during_audio_keeps_receipt_without_vision_or_evidence(self):
        self.cancel_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "CANCELLED")
        self.assertEqual(self.calls, ["audio"])
        self.assertEqual(len(self.job()["stage_outputs"]), 1)

    def test_unknown_audio_stops_vision_without_refund(self):
        self.fail_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.calls, ["audio"])
        self.assertEqual(self.job()["calls"][0]["outcome"], "UNKNOWN")
        self.assertIsNone(self.job()["calls"][0]["measured_usage"])

    def test_decoder_unconfirmed_close_persists_receipt_and_never_calls_models(self):
        self.fail_decode_close = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        job = self.job()
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(self.calls, [])
        self.assertEqual(job["browser"]["outcome"], "UNKNOWN")
        self.assertEqual(job["browser"]["lifecycle"]["close_status"], "UNKNOWN")
        self.assertEqual(job["browser"]["reserved_seconds"], 600)
        self.assertEqual(self.job()["calls"], [])

    def test_missing_browser_headroom_and_wrong_scope_refuse_grant(self):
        for change in (
            {"browser_remaining_seconds": 599},
            {"browser_included_only": False},
            {"vision_model": "other-model"},
            {"source_kind": "AUDIO"},
        ):
            with self.subTest(change=change), self.assertRaises(WebInputError):
                self.grant(**change)
        self.assertEqual(self.api.rows("SELECT * FROM jobs"), [])
        self.assertEqual(self.calls, [])
