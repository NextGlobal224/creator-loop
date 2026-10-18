import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.analysis.workers_analysis_reuse import (
    receipt_fingerprint,
    reuse_analysis,
)


def make_analysis_receipt(root, payload, model, target, origin, value):
    folder = root / "runtime" / "analysis-reuse-fixture"
    folder.mkdir(parents=True)
    (folder / "request.json").write_bytes(payload)
    (folder / "response.json").write_text(
        json.dumps(
            {
                "model": model,
                "worker_version": origin,
                "stage_result": {
                    "stage": "analysis",
                    "value": value,
                    "usage": {"total_tokens": 4},
                },
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(payload).hexdigest()
    compatibility = {
        "stage": "analysis",
        "origin_worker_version": origin,
        "target_worker_version": target,
        "model": model,
        "request_sha256": digest,
        "analysis_contract_sha256": "c" * 64,
    }
    (folder / "compatibility.json").write_text(
        json.dumps(compatibility), encoding="utf-8"
    )
    receipt = {
        "format": "WORKERS_ANALYSIS_REUSE_V1",
        **compatibility,
        "endpoint_sha256": hashlib.sha256(
            b"https://fixture.workers.dev/b/generate"
        ).hexdigest(),
        "response_sha256": hashlib.sha256(
            (folder / "response.json").read_bytes()
        ).hexdigest(),
        "compatibility_sha256": hashlib.sha256(
            (folder / "compatibility.json").read_bytes()
        ).hexdigest(),
    }
    path = folder / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


class AnalysisReuseTests(unittest.TestCase):
    def test_original_identity_is_retained_and_only_analysis_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b'{"stage":"analysis","previous":[]}'
            origin = "00000000-0000-4000-8000-000000000002"
            target = "00000000-0000-4000-8000-000000000001"
            value = {
                "observations": [{"quote": "fixture"}],
                "inferences": [],
                "direction": "Unverified proposal",
            }
            path = make_analysis_receipt(root, payload, "model", target, origin, value)
            before = (path.parent / "response.json").read_bytes()
            item, provenance = reuse_analysis(
                root,
                path,
                payload,
                endpoint="https://fixture.workers.dev/b/generate",
                model="model",
                worker_version=target,
            )
            self.assertEqual(item["value"], value)
            self.assertEqual(provenance["origin_worker_version"], origin)
            self.assertEqual(provenance["target_worker_version"], target)
            self.assertEqual(provenance["new_model_calls"], 0)
            self.assertEqual((path.parent / "response.json").read_bytes(), before)
            for changed in [b"changed-input"]:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    reuse_analysis(
                        root,
                        path,
                        changed,
                        endpoint="https://fixture.workers.dev/b/generate",
                        model="model",
                        worker_version=target,
                    )
            receipt = json.loads(path.read_text())
            for changes in [
                {"model": "other"},
                {"target_worker_version": origin},
                {"stage": "editing"},
            ]:
                path.write_text(json.dumps({**receipt, **changes}))
                with self.assertRaises(ValueError):
                    reuse_analysis(
                        root,
                        path,
                        payload,
                        endpoint="https://fixture.workers.dev/b/generate",
                        model="model",
                        worker_version=target,
                    )
            path.write_text(json.dumps(receipt))
            (path.parent / "response.json").write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                reuse_analysis(
                    root,
                    path,
                    payload,
                    endpoint="https://fixture.workers.dev/b/generate",
                    model="model",
                    worker_version=target,
                )

    def test_cache_paths_and_byte_budgets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "receipt.json"
            outside.write_text("{}")
            with self.assertRaises(ValueError):
                receipt_fingerprint(root, outside)
            path = root / "runtime/oversized.json"
            path.parent.mkdir()
            path.write_bytes(b"x" * 8193)
            with self.assertRaises(ValueError):
                receipt_fingerprint(root, path)
