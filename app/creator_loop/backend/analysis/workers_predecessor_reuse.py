"""Explicit, compatible writing/editing predecessors; audit is always live.

Receipts preserve generated data and its original identity, not source authority,
factuality or human review. The target Worker revalidates all predecessors before
auditing. Normal app generation has no predecessor receipts.
"""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from creator_loop.backend.analysis.workers_analysis_reuse import (
    _read,
    receipt_fingerprint,
)
from creator_loop.backend.analysis.workers_quality import worker_audit_findings


def reuse_predecessor(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    stage: str,
    endpoint: str,
    model: str,
    worker_version: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if stage not in ("writing", "editing"):
        raise ValueError("Only writing/editing predecessors may be reused")
    fingerprint = receipt_fingerprint(root, path)
    receipt_raw = _read(path, 8192)
    if hashlib.sha256(receipt_raw).hexdigest() != fingerprint:
        raise ValueError("Predecessor receipt changed while reading")
    receipt = json.loads(receipt_raw)
    if (
        not isinstance(receipt, dict)
        or receipt.get("format") != "WORKERS_PREDECESSOR_REUSE_V1"
        or receipt.get("stage") != stage
        or receipt.get("target_worker_version") != worker_version
        or receipt.get("model") != model
        or receipt.get("endpoint_sha256")
        != hashlib.sha256(endpoint.encode()).hexdigest()
        or receipt.get("request_sha256") != hashlib.sha256(payload).hexdigest()
        or receipt_fingerprint(root, path) != fingerprint
    ):
        raise ValueError("Predecessor reuse input, origin or target does not match")
    artifacts = {}
    for key, filename, limit in (
        ("request_sha256", "request.json", 64 * 1024),
        ("response_sha256", "response.json", 64 * 1024),
        ("compatibility_sha256", "compatibility.json", 16 * 1024),
        ("proof_sha256", "proof.json", 16 * 1024),
    ):
        file = path.parent / filename
        if (
            file.is_symlink()
            or not file.resolve().is_relative_to(path.parent.resolve())
            or not file.is_file()
            or file.stat().st_size > limit
        ):
            raise ValueError("Predecessor artifact missing or oversized")
        raw = _read(file, limit)
        if hashlib.sha256(raw).hexdigest() != receipt.get(key):
            raise ValueError("Predecessor artifact changed")
        artifacts[filename] = raw
    origin = receipt.get("origin_worker_version")
    contract = receipt.get("stage_contract_sha256")
    if (
        not isinstance(origin, str)
        or str(UUID(origin)) != origin
        or not isinstance(contract, str)
        or len(contract) != 64
        or any(c not in "0123456789abcdef" for c in contract)
    ):
        raise ValueError("Predecessor identity or contract fingerprint is invalid")
    compatibility = json.loads(artifacts["compatibility.json"])
    if not isinstance(compatibility, dict) or any(
        compatibility.get(key) != receipt.get(key)
        for key in (
            "stage",
            "origin_worker_version",
            "target_worker_version",
            "model",
            "request_sha256",
            "response_sha256",
            "stage_contract_sha256",
            "proof_sha256",
        )
    ):
        raise ValueError("Predecessor compatibility evidence does not match")
    proof = json.loads(artifacts["proof.json"])
    if (
        not isinstance(proof, dict)
        or proof.get("format") != "WORKERS_STAGE_COMPATIBILITY_V1"
        or proof.get("stage") != stage
        or proof.get("model") != model
        or proof.get("origin_worker_version") != origin
        or proof.get("request_sha256") != receipt["request_sha256"]
        or proof.get("response_sha256") != receipt["response_sha256"]
        or proof.get("native_inputs_equal") is not True
        or proof.get("native_input_sha256") != contract
    ):
        raise ValueError("Predecessor native-input compatibility proof is invalid")
    reply = json.loads(artifacts["response.json"])
    item = reply.get("stage_result") if isinstance(reply, dict) else None
    if (
        not isinstance(reply, dict)
        or reply.get("model") != model
        or reply.get("worker_version") != origin
        or not isinstance(item, dict)
        or item.get("stage") != stage
        or not isinstance(item.get("value"), dict)
    ):
        raise ValueError("Predecessor response identity or stage is invalid")
    if stage == "editing":
        worker_audit_findings(item)
    return item, {
        "execution": "reused_predecessor",
        "origin_worker_version": origin,
        "target_worker_version": worker_version,
        "input_sha256": receipt["request_sha256"],
        "output_sha256": receipt["response_sha256"],
        "compatibility_sha256": receipt["compatibility_sha256"],
        "stage_contract_sha256": contract,
        "proof_sha256": receipt["proof_sha256"],
        "receipt_sha256": fingerprint,
        "new_model_calls": 0,
    }
