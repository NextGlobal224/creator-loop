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
        self.hold_decode = False
        self.hold_audio = False
        self.hold_vision = False
        self.fail_audio = False
        self.fail_decode_close = False
        self.reassign_decode = False
        self.reassign_audio = False
        self.delete_during_decode = False
        self.corrupt_during_audio = False
        self.invalid_decode = False
        self.invalid_audio = False
        self.invalid_lifecycle = False
        self.binary_decode = False
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
            if self.hold_decode:
                self.api._restore_hold = lambda: True
            if self.reassign_decode:
                current = self.job()
                current["lease_until"] += 1000
                current["new_owner_marker"] = "decode"
                self.api.save_job(current)
            if self.delete_during_decode:
                self.api.sql.exec("DELETE FROM jobs WHERE id=?", self.key)
            if self.invalid_decode:
                return "{bad"
            if self.invalid_lifecycle:
                return '{"ok":true,"browser_lifecycle":null}'
            if self.binary_decode:
                return b'{"ok":true,"browser_lifecycle":null}'
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
            if self.hold_audio:
                self.api._restore_hold = lambda: True
            if self.reassign_audio:
                current = self.job()
                current["lease_until"] += 1000
                current["new_owner_marker"] = "audio"
                self.api.save_job(current)
            if self.corrupt_during_audio:
                self.api.sql.exec(
                    "UPDATE jobs SET record=? WHERE id=?", "{bad", self.key
                )
            if self.invalid_audio:
                return "{bad"
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
            if self.hold_vision:
                self.api._restore_hold = lambda: True
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

    def test_hold_during_decode_keeps_receipt_without_model_calls(self):
        self.hold_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(self.job()["browser"]["outcome"], "RECEIVED")
        self.assertEqual(self.calls, [])

    def test_hold_during_audio_preserves_receipt_and_stops_vision(self):
        self.hold_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(self.calls, ["audio"])
        self.assertEqual(self.job()["calls"][0]["outcome"], "RECEIVED")
        self.assertEqual(len(self.job()["stage_outputs"]), 1)

    def test_hold_during_vision_preserves_both_receipts_without_finalizing(self):
        self.hold_vision = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "RUNNING")
        self.assertEqual(self.calls, ["audio", "vision"])
        self.assertEqual(len(self.job()["stage_outputs"]), 2)
        self.assertEqual(self.job()["calls"][-1]["outcome"], "RECEIVED")
        self.assertNotIn("evidence_version_ids", self.job())

    def test_unknown_audio_stops_vision_without_refund(self):
        self.fail_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertEqual(self.calls, ["audio"])
        self.assertEqual(self.job()["calls"][0]["outcome"], "UNKNOWN")
        self.assertIsNone(self.job()["calls"][0]["measured_usage"])

    def test_reassigned_owner_during_decode_keeps_late_browser_receipt(self):
        self.reassign_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        current = self.job()
        self.assertEqual(current["new_owner_marker"], "decode")
        self.assertEqual(current["browser"]["outcome"], "UNKNOWN")
        self.assertEqual(self.calls, [])
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        self.assertEqual(
            json.loads(late[0]["record"])["browser"]["outcome"], "RECEIVED"
        )

    def test_reassigned_owner_during_audio_keeps_late_native_receipt(self):
        self.reassign_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        current = self.job()
        self.assertEqual(current["new_owner_marker"], "audio")
        self.assertEqual(current["calls"][0]["outcome"], "UNKNOWN")
        self.assertEqual(self.calls, ["audio"])
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        self.assertEqual(
            json.loads(late[0]["record"])["response"]["native_usage"], {"neurons": 1}
        )

    def test_missing_job_during_decode_preserves_browser_receipt(self):
        self.delete_during_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, [])
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        self.assertEqual(
            json.loads(late[0]["record"])["browser"]["outcome"], "RECEIVED"
        )

    def test_corrupt_job_during_audio_preserves_native_receipt(self):
        self.corrupt_during_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, ["audio"])
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        self.assertEqual(
            json.loads(late[0]["record"])["response"]["native_usage"], {"neurons": 1}
        )

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

    def test_same_lease_metadata_change_before_decode_is_not_overwritten(self):
        self.grant()
        self.enqueue()

        async def changed_alarm(_):
            current = self.job()
            current["new_owner_marker"] = "same-lease"
            self.api.save_job(current)

        self.api.ctx.storage.setAlarm = changed_alarm
        asyncio.run(self.api.alarm())
        self.assertEqual(self.job()["new_owner_marker"], "same-lease")
        self.assertNotIn("browser", self.job())
        self.assertEqual(self.calls, [])

    def test_invalid_audio_response_retains_unknown_hash(self):
        self.invalid_audio = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        receipt = json.loads(late[0]["record"])
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertEqual(receipt["raw_sha256"], hashlib.sha256(b"{bad").hexdigest())
        self.assertEqual(self.calls, ["audio"])

    def test_invalid_decode_response_retains_unknown_hash(self):
        self.invalid_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        receipt = json.loads(late[0]["record"])
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertEqual(receipt["raw_sha256"], hashlib.sha256(b"{bad").hexdigest())
        self.assertEqual(self.calls, [])

    def test_invalid_lifecycle_after_job_deletion_retains_unknown(self):
        self.invalid_lifecycle = True
        self.delete_during_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        self.assertEqual(len(late), 1)
        receipt = json.loads(late[0]["record"])
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertEqual(
            receipt["raw_sha256"],
            hashlib.sha256(b'{"ok":true,"browser_lifecycle":null}').hexdigest(),
        )
        self.assertEqual(self.calls, [])

    def test_non_string_decoder_envelope_retains_unknown_type(self):
        self.binary_decode = True
        self.delete_during_decode = True
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        late = self.api.rows("SELECT record FROM control WHERE id LIKE 'late-source-%'")
        receipt = json.loads(late[0]["record"])
        self.assertEqual(
            (receipt["outcome"], receipt["raw_type"]), ("UNKNOWN", "bytes")
        )

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
