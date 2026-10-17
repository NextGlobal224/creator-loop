"""Explicit reuse of one compatible, unchanged analysis; never editorial output.

The caller supplies a compatibility receipt. This is generated data, not source
evidence, human review or a factuality certificate. Normal generation has no reuse.
"""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Analysis reuse artifact exceeds its byte budget")
    return raw


def receipt_fingerprint(root: Path, path: Path) -> str:
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to((root / "runtime").resolve())
        or not path.is_file()
        or path.stat().st_size > 8192
    ):
        raise ValueError("Analysis reuse receipt must be bounded app runtime data")
    return hashlib.sha256(_read(path, 8192)).hexdigest()


def reuse_analysis(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    endpoint: str,
    model: str,
    worker_version: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt_hash = receipt_fingerprint(root, path)
    raw = _read(path, 8192)
    if hashlib.sha256(raw).hexdigest() != receipt_hash:
        raise ValueError("Analysis reuse receipt changed while reading")
    receipt = json.loads(raw)
    if (
        not isinstance(receipt, dict)
        or receipt.get("format") != "WORKERS_ANALYSIS_REUSE_V1"
        or receipt.get("stage") != "analysis"
        or receipt.get("target_worker_version") != worker_version
        or receipt.get("model") != model
        or receipt.get("endpoint_sha256")
        != hashlib.sha256(endpoint.encode()).hexdigest()
        or receipt.get("request_sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise ValueError("Analysis reuse input, origin or target does not match")
    artifacts = {}
    for key, filename, limit in (
        ("request_sha256", "request.json", 64 * 1024),
        ("response_sha256", "response.json", 64 * 1024),
        ("compatibility_sha256", "compatibility.json", 16 * 1024),
    ):
        file = path.parent / filename
        if (
            file.is_symlink()
            or not file.resolve().is_relative_to(path.parent.resolve())
            or not file.is_file()
            or file.stat().st_size > limit
        ):
            raise ValueError("Analysis reuse artifact missing, changed or oversized")
        raw = _read(file, limit)
        if hashlib.sha256(raw).hexdigest() != receipt.get(key):
            raise ValueError("Analysis reuse artifact changed")
        artifacts[filename] = raw
    origin = receipt.get("origin_worker_version")
    if not isinstance(origin, str) or str(UUID(origin)) != origin:
        raise ValueError("Analysis reuse lost original Worker identity")
    compatibility = json.loads(artifacts["compatibility.json"])
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("stage") != "analysis"
        or compatibility.get("origin_worker_version") != origin
        or compatibility.get("target_worker_version") != worker_version
        or compatibility.get("model") != model
        or compatibility.get("request_sha256") != receipt["request_sha256"]
        or compatibility.get("analysis_contract_sha256")
        != receipt.get("analysis_contract_sha256")
        or not isinstance(receipt.get("analysis_contract_sha256"), str)
        or len(receipt["analysis_contract_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in receipt["analysis_contract_sha256"])
    ):
        raise ValueError("Analysis reuse compatibility evidence does not match")
    reply = json.loads(artifacts["response.json"])
    item = reply.get("stage_result") if isinstance(reply, dict) else None
    if (
        not isinstance(reply, dict)
        or reply.get("model") != model
        or reply.get("worker_version") != origin
        or not isinstance(item, dict)
        or item.get("stage") != "analysis"
        or not isinstance(item.get("value"), dict)
        or set(item["value"]) != {"observations", "inferences", "direction"}
        or not isinstance(item["value"]["observations"], list)
        or not item["value"]["observations"]
        or not isinstance(item["value"]["inferences"], list)
        or not isinstance(item["value"]["direction"], str)
    ):
        raise ValueError("Analysis reuse response identity or stage is invalid")
    return item, {
        "execution": "reused_analysis",
        "origin_worker_version": origin,
        "target_worker_version": worker_version,
        "input_sha256": receipt["request_sha256"],
        "output_sha256": receipt["response_sha256"],
        "compatibility_sha256": receipt["compatibility_sha256"],
        "receipt_sha256": receipt_hash,
        "new_model_calls": 0,
    }
