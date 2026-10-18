"""Hold inert, hash-bound B reuse receipts without granting factual authority."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Callable
from uuid import UUID

from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_recovery import _identity_dead
from creator_loop.backend.runtime.windows_paths import file_io_path


def hold_reuse_evidence(
    root: Path, directories: list[Path], held: ExitStack
) -> Callable[[], None]:
    """No executors may exist here. Require dead parent, exact schema and hashes.

    These folders may precede a generation/preflight, so a terminal generation DB
    row is not inferred. They contain inert selected data only. Hold native leases
    through backup; preserve all bytes and original identity, never delete/execute.
    """
    namespaces = {}
    for directory in directories:
        match = re.fullmatch(
            r"analysis-reuse-([0-9a-f]{32})|predecessor-reuse-(writing|editing)-([0-9a-f]{32})",
            directory.name,
        )
        if match is None:
            raise RuntimeError("Unknown B reuse directory")
        stage = match[2] or "analysis"
        held.enter_context(RuntimeHandle(directory, directory=True))
        expected = {
            "ownership.json",
            "receipt.json",
            "request.json",
            "response.json",
            "compatibility.json",
        }
        if stage != "analysis":
            expected.add("proof.json")
        if {p.name for p in file_io_path(directory).iterdir()} != expected:
            raise RuntimeError("Unknown or incomplete B reuse files")
        namespaces[directory] = expected
        handles = {
            name: held.enter_context(RuntimeHandle(directory / name, read_only=True))
            for name in expected
        }
        marker = handles["ownership.json"].read_json(reject_duplicates=True)
        receipt = handles["receipt.json"].read_json(reject_duplicates=True)
        compatibility = handles["compatibility.json"].read_json(reject_duplicates=True)
        if (
            type(marker.get("format")) is not int
            or marker["format"] != 1
            or marker.get("workspace") != directory.name
            or marker.get("data_root") != str(root)
            or marker.get("kind") != "CREATOR_LLM_WORKSPACE"
            or not isinstance(marker.get("parent"), dict)
            or not _identity_dead(marker["parent"])
        ):
            raise RuntimeError("B reuse owner is live or unbound")
        if (
            receipt.get("stage") != stage
            or receipt.get("format")
            != (
                "WORKERS_ANALYSIS_REUSE_V1"
                if stage == "analysis"
                else "WORKERS_PREDECESSOR_REUSE_V1"
            )
            or receipt.get("model")
            not in {
                "@cf/openai/gpt-oss-120b",
                "@cf/aisingapore/gemma-sea-lion-v4-27b-it",
                "@cf/qwen/qwen3-30b-a3b-fp8",
            }
        ):
            raise RuntimeError("B reuse receipt identity invalid")
        for key in ("origin_worker_version", "target_worker_version"):
            if (
                not isinstance(receipt.get(key), str)
                or str(UUID(receipt[key])) != receipt[key]
            ):
                raise RuntimeError("B reuse lost original Worker identity")
        component = marker.get("component_version")
        if (
            not isinstance(component, str)
            or not component.startswith(
                "creator-workers/worker-"
                + receipt["target_worker_version"]
                + "/"
                + receipt["model"]
                + "/"
            )
            or not component.endswith("/" + stage + "-reuse")
        ):
            raise RuntimeError("B reuse component does not match receipt")
        for filename in (
            "request.json",
            "response.json",
            "compatibility.json",
            *(["proof.json"] if stage != "analysis" else []),
        ):
            limit = (
                64 * 1024
                if filename in {"request.json", "response.json"}
                else 16 * 1024
            )
            with file_io_path(directory / filename).open("rb") as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit or hashlib.sha256(raw).hexdigest() != receipt.get(
                filename.removesuffix(".json") + "_sha256"
            ):
                raise RuntimeError("B reuse artifact digest changed")
        contract_key = (
            "analysis_contract_sha256"
            if stage == "analysis"
            else "stage_contract_sha256"
        )
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(contract_key, ""))):
            raise RuntimeError("B reuse contract digest missing")
        for key in (
            "stage",
            "origin_worker_version",
            "target_worker_version",
            "model",
            "request_sha256",
            contract_key,
        ):
            if compatibility.get(key) != receipt.get(key):
                raise RuntimeError("B reuse compatibility binding mismatch")
        reply = json.loads(file_io_path(directory / "response.json").read_bytes())
        if (
            not isinstance(reply, dict)
            or reply.get("worker_version") != receipt["origin_worker_version"]
            or reply.get("model") != receipt["model"]
            or reply.get("stage_result", {}).get("stage") != stage
        ):
            raise RuntimeError("B reuse actual response origin mismatch")
        if stage != "analysis":
            proof = handles["proof.json"].read_json(reject_duplicates=True)
            if (
                proof.get("format") != "WORKERS_STAGE_COMPATIBILITY_V1"
                or proof.get("native_inputs_equal") is not True
                or proof.get("native_input_sha256") != receipt[contract_key]
                or any(
                    proof.get(key) != receipt[key]
                    for key in (
                        "stage",
                        "origin_worker_version",
                        "model",
                        "request_sha256",
                        "response_sha256",
                    )
                )
            ):
                raise RuntimeError("B predecessor proof mismatch")

    def recheck() -> None:
        for directory, expected in namespaces.items():
            if {p.name for p in file_io_path(directory).iterdir()} != expected:
                raise RuntimeError("B reuse namespace changed during backup")

    recheck()
    return recheck
