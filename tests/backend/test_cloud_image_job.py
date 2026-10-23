"""Actual durable image handler with fake provider, not real cloud/vision QA."""

import asyncio
import base64
import json
import time
import unittest
from types import SimpleNamespace

from creator_loop.backend.creator.web_domain import WebDomain, WebInputError
from creator_loop.backend.knowledge.cloud_image import IMAGE_MODEL

from tests.backend import test_cloud_source_job
from tests.backend.test_cloud_image import png, response


class CloudImageJobTests(unittest.TestCase):
    enqueue = test_cloud_source_job.CloudSourceJobTests.enqueue
    job = test_cloud_source_job.CloudSourceJobTests.job

    def setUp(self):
        test_cloud_source_job.CloudSourceJobTests.setUp(self)
        self.raw = png()
        self.file = self.fixture.call(
            {
                "project_id": self.project,
                "media_type": "IMAGE",
                "display_name": "Ảnh fixture.png",
                "content_base64": base64.b64encode(self.raw).decode(),
            },
            path="/api/materials",
            key="d" * 32,
        ).value["file_id"]
        self.request = {"project_id": self.project, "file_id": self.file}
        self.response = response()

        async def analyze(binding, encoded, deployment):
            self.calls += 1
            packet = json.loads(encoded)
            self.assertEqual(packet["model"], IMAGE_MODEL)
            self.assertEqual(base64.b64decode(packet["image"]), self.raw)
            return json.dumps(
                {
                    "ok": True,
                    "model": IMAGE_MODEL,
                    "worker_version": deployment,
                    "request_id": packet["request_id"],
                    "input_sha256": packet["input_sha256"],
                    "model_calls": 1,
                    "native_usage": None,
                    "native_response": self.response,
                }
            )

        self.native_image = analyze
        self.api.authorized.__func__.__globals__["import_from_javascript"] = (
            lambda name: SimpleNamespace(analyzeSourceImage=analyze)
        )

    def grant(self):
        now = time.time()
        return self.fixture.call(
            {
                "grant": "B-finish-20261017-free",
                "observed_at": now,
                "expires_at": now + 240,
                "remaining_calls": 1,
                "model": IMAGE_MODEL,
                "account_plan": "Paid",
                "included_only": True,
                "max_extra_charge_usd": 0,
                "remaining_neurons": 9000,
                "estimated_chain_neurons": 5000,
            },
            path="/api/grant",
        )

    def test_image_job_reserves_one_call_binds_native_and_reuses_result(self):
        self.grant()
        self.assertEqual(self.enqueue().value["kind"], "SOURCE_IMAGE_ANALYSIS")
        self.assertNotIn('"image":', json.dumps(self.job()))
        self.assertEqual(self.enqueue().value["id"], "c" * 32)
        asyncio.run(self.api.alarm())
        j = self.job()
        self.assertEqual((j["status"], self.calls), ("SUCCEEDED", 1))
        self.assertEqual(j["calls"][0]["stage"], "image_analysis")
        self.assertIsNone(j["calls"][0]["measured_usage"])
        self.assertEqual(j["stage_outputs"][0]["native_response"], self.response)
        asyncio.run(self.api.alarm())
        self.assertEqual(self.calls, 1)
        self.grant()
        with self.assertRaisesRegex(WebInputError, "Đã có phân tích"):
            self.fixture.call(dict(self.request), path="/api/source-jobs", key="e" * 32)
        review = asyncio.run(
            self.api.authorized(
                SimpleNamespace(
                    headers={},
                    url=f"https://fixture/api/source-review?project_id={self.project}&file_id={self.file}",
                ),
                "/api/source-review",
                "GET",
                {},
            )
        ).value
        self.assertEqual(review["source_extraction"]["model"], IMAGE_MODEL)
        self.assertEqual(len(review["evidence_versions"]), 2)
        self.assertTrue(
            all(v["review_state"] == "PENDING" for v in review["evidence_versions"])
        )
        self.assertIsNone(review["source_use"])
        self.assertFalse(review["factual_approval"] or review["publication_approval"])

    def test_wrong_model_and_cross_UTC_day_never_infer(self):
        test_cloud_source_job.CloudSourceJobTests.grant(self)
        with self.assertRaisesRegex(WebInputError, "operator đọc nguồn"):
            self.enqueue()
        self.assertEqual(self.calls, 0)
        validate = self.api.authorized.__func__.__globals__["valid_included_grant"]
        data = {
            "grant": "B-finish-20261017-free",
            "model": IMAGE_MODEL,
            "account_plan": "Paid",
            "included_only": True,
            "max_extra_charge_usd": 0,
            "remaining_calls": 1,
            "observed_at": 86399.0,
            "expires_at": 86641.0,
            "remaining_neurons": 9000,
            "estimated_chain_neurons": 5000,
        }
        self.assertFalse(validate(data, IMAGE_MODEL, 86401.0))

    def test_cancelled_image_call_keeps_native_without_evidence_or_retry(self):
        async def cancelled(binding, packet, deployment):
            value = await self.native_image(binding, packet, deployment)
            await self.api.authorized(
                SimpleNamespace(headers={}), "/api/cancel", "POST", {"job_id": "c" * 32}
            )
            return value

        self.api.authorized.__func__.__globals__["import_from_javascript"] = (
            lambda name: SimpleNamespace(analyzeSourceImage=cancelled)
        )
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        self.assertEqual((self.job()["status"], self.calls), ("CANCELLED", 1))
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

    def test_image_parser_failure_keeps_received_original_and_consumed_grant(self):
        self.response["choices"][0]["message"]["content"] = "not structured JSON"
        self.grant()
        self.enqueue()
        asyncio.run(self.api.alarm())
        j = self.job()
        self.assertEqual((j["status"], self.calls), ("FAILED", 1))
        self.assertEqual(j["calls"][0]["outcome"], "RECEIVED")
        self.assertEqual(j["stage_outputs"][0]["native_response"], self.response)
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
