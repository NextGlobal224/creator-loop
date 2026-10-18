"""Synthetic receipt/provenance checks, never a source-quality certificate."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.analysis.workers_predecessor_reuse import reuse_predecessor
from creator_loop.backend.analysis.workers_quality import ARTICLE_REQUIREMENT_IDS
from creator_loop.backend.creator.research_pipeline import ResearchPipeline


def make_predecessor_receipt(root, stage, payload, response, model, target, origin):
    folder = root / "runtime" / (stage + "-reuse-fixture")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "request.json").write_bytes(payload)
    (folder / "response.json").write_bytes(response)
    request_hash = hashlib.sha256(payload).hexdigest()
    proof = {
        "format": "WORKERS_STAGE_COMPATIBILITY_V1",
        "stage": stage,
        "model": model,
        "origin_worker_version": origin,
        "request_sha256": request_hash,
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "native_inputs_equal": True,
        "native_input_sha256": "c" * 64,
        "scope": "Synthetic protocol fixture, not actual native/cloud execution",
    }
    (folder / "proof.json").write_text(json.dumps(proof))
    compatibility = {
        "stage": stage,
        "origin_worker_version": origin,
        "target_worker_version": target,
        "model": model,
        "request_sha256": request_hash,
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "stage_contract_sha256": "c" * 64,
        "proof_sha256": hashlib.sha256(
            (folder / "proof.json").read_bytes()
        ).hexdigest(),
    }
    (folder / "compatibility.json").write_text(json.dumps(compatibility))
    receipt = {
        "format": "WORKERS_PREDECESSOR_REUSE_V1",
        **compatibility,
        "endpoint_sha256": hashlib.sha256(
            b"https://fixture.workers.dev/b/generate"
        ).hexdigest(),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "compatibility_sha256": hashlib.sha256(
            (folder / "compatibility.json").read_bytes()
        ).hexdigest(),
    }
    path = folder / "receipt.json"
    path.write_text(json.dumps(receipt))
    return path


class PredecessorReuseTests(unittest.TestCase):
    def test_original_response_is_not_relabelled_and_bindings_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = "00000000-0000-4000-8000-000000000002"
            target = "00000000-0000-4000-8000-000000000001"
            for stage in ("writing", "editing"):
                payload = json.dumps({"stage": stage, "previous": []}).encode()
                item = {"stage": stage, "value": {"source_issues": [], "edits": []}}
                if stage == "editing":
                    item["task_requirements"] = ARTICLE_REQUIREMENT_IDS
                response = json.dumps(
                    {"model": "model", "worker_version": origin, "stage_result": item}
                ).encode()
                path = make_predecessor_receipt(
                    root, stage, payload, response, "model", target, origin
                )
                options = {
                    "stage": stage,
                    "endpoint": "https://fixture.workers.dev/b/generate",
                    "model": "model",
                    "worker_version": target,
                }
                cached, provenance = reuse_predecessor(root, path, payload, **options)
                self.assertEqual(cached, item)
                self.assertEqual(provenance["origin_worker_version"], origin)
                self.assertEqual(provenance["new_model_calls"], 0)
                self.assertEqual((path.parent / "response.json").read_bytes(), response)
                for changes in (
                    {"model": "other"},
                    {"worker_version": origin},
                    {"endpoint": "https://other.workers.dev/b/generate"},
                    {"stage": "source_audit"},
                ):
                    with self.assertRaises(ValueError):
                        reuse_predecessor(root, path, payload, **{**options, **changes})
                with self.assertRaises(ValueError):
                    reuse_predecessor(root, path, payload + b" ", **options)
                receipt = json.loads(path.read_text())
                changed = json.loads(response)
                changed["stage_result"]["value"]["changed"] = True
                changed_raw = json.dumps(changed).encode()
                (path.parent / "response.json").write_bytes(changed_raw)
                path.write_text(
                    json.dumps(
                        {
                            **receipt,
                            "response_sha256": hashlib.sha256(changed_raw).hexdigest(),
                        }
                    )
                )
                with self.assertRaisesRegex(ValueError, "compatibility"):
                    reuse_predecessor(root, path, payload, **options)
                (path.parent / "response.json").write_bytes(response)
                path.write_text(json.dumps(receipt))
                (path.parent / "proof.json").write_text("{}")
                with self.assertRaisesRegex(ValueError, "changed"):
                    reuse_predecessor(root, path, payload, **options)
                path.write_text(json.dumps(receipt))

    def test_audit_reuse_and_noncontiguous_or_implicit_prefixes_are_forbidden(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for receipts, provider, analysis in (
                ({"editing": root / "e"}, "CLOUDFLARE_WORKERS_AI", root / "a"),
                ({"source_audit": root / "e"}, "CLOUDFLARE_WORKERS_AI", root / "a"),
                ({"writing": root / "w"}, "CODEX_CLI_TEXT", root / "a"),
                ({"writing": root / "w"}, "CLOUDFLARE_WORKERS_AI", None),
            ):
                with self.assertRaises(ValueError):
                    ResearchPipeline(
                        root,
                        provider_override=provider,
                        workers_analysis_receipt=analysis,
                        workers_predecessor_receipts=receipts,
                    )
