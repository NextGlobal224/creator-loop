"""Bounded, explicit Workers AI provider. No secrets or requests on import."""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from creator_loop.backend.analysis.creator_llm import (
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_CHARACTERS,
    response_schema,
    validate_response,
)
from creator_loop.backend.analysis.generation_prompt import generation_prompt
from creator_loop.backend.analysis.workers_quality import validate_quality_audit
from creator_loop.backend.analysis.workers_transport import post_owned
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial


@dataclass(frozen=True)
class WorkersModel:
    component_id: str = "@cf/openai/gpt-oss-120b"
    version: str = "cloudflare-hosted"


class WorkersCreator:
    name = "CLOUDFLARE_WORKERS_AI"
    model = WorkersModel()

    def __init__(self, root: Path, manifest: Path | None = None) -> None:
        self.root = root.resolve(strict=True)
        path = manifest or self.root / "manifests/creator-cloud.json"
        if path.stat().st_size > 4096 or path.is_symlink():
            raise ValueError("Invalid cloud config")
        config = json.loads(path.read_text(encoding="utf-8"))
        allowed_models = {
            "@cf/openai/gpt-oss-120b",
            "@cf/aisingapore/gemma-sea-lion-v4-27b-it",
            "@cf/qwen/qwen3-30b-a3b-fp8",
        }
        if (
            not isinstance(config, dict)
            or set(config) != {"endpoint", "model", "worker_version", "key_file"}
            or config["model"] not in allowed_models
            or not isinstance(config["worker_version"], str)
            or config["key_file"] != "secrets/creator-cloud-key"
        ):
            raise ValueError("Unknown cloud provider configuration")
        if str(UUID(config["worker_version"])) != config["worker_version"]:
            raise ValueError("Pin an exact deployed Worker Version UUID")
        self.worker_version = config["worker_version"]
        self.config = config
        url = urlsplit(config["endpoint"])
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".workers.dev")
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.query
            or url.fragment
            or url.path != "/b/generate"
        ):
            raise ValueError("Untrusted Worker endpoint")
        self.endpoint = config["endpoint"]
        self.model = WorkersModel(component_id=config["model"])
        self.key_file = self.root / "secrets/creator-cloud-key"
        self.binding_file = self.root / "secrets/creator-cloud-key.binding.json"
        if self.key_file.exists():
            self._credential_binding()
        self.version = (
            "worker-"
            + self.worker_version
            + "/"
            + self.model.component_id
            + "/prompt-v25-sdk4-ai7-editorial-findings/"
            + hashlib.sha256(self.endpoint.encode()).hexdigest()[:12]
        )

    def _credential_binding(self) -> dict[str, str]:
        if self.binding_file.is_symlink() or self.binding_file.stat().st_size > 1024:
            raise ValueError("Invalid scoped Worker credential binding")
        binding = json.loads(self.binding_file.read_text(encoding="utf-8"))
        if (
            not isinstance(binding, dict)
            or set(binding) != {"endpoint", "key_sha256"}
            or binding["endpoint"] != self.endpoint
        ):
            raise ValueError(
                "App credential is scoped to a different Worker; configure a new credential"
            )
        return binding

    def generate(
        self,
        request: CreatorRequest,
        materials: Sequence[ResearchMaterial],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if request.output_format != "TEXT":
            raise ValueError(
                "Workers B pilot supports TEXT; other formats require a separately verified provider"
            )
        if (
            not materials
            or sum(len(m.text) for m in materials) > MAX_SOURCE_CHARACTERS
            or any(not m.text.strip() for m in materials)
        ):
            raise ValueError("Selected cloud input must be bounded processed text")
        if cancelled():
            raise InterruptedError("Cancelled before upload")
        if self.key_file.is_symlink() or self.key_file.stat().st_size > 128:
            raise ValueError("Invalid app credential")
        key = self.key_file.read_text(encoding="utf-8").strip()
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid app credential")
        if (
            hashlib.sha256(key.encode()).hexdigest()
            != self._credential_binding()["key_sha256"]
        ):
            raise ValueError(
                "Worker credential changed; update its explicit origin binding"
            )
        payload = json.dumps(
            {
                "prompt": generation_prompt(request, materials),
                "writing_prompt": generation_prompt(
                    request, materials, only_draft=True
                ),
                "schema": response_schema([m.material_id for m in materials]),
            },
            ensure_ascii=False,
        ).encode()
        if len(payload) > 64 * 1024:
            raise ValueError("Cloud request exceeds budget")
        workspace = self.root / "runtime" / ("creator-workers-" + uuid4().hex)
        workspace.mkdir()
        create_workspace_marker(
            self.root,
            workspace,
            "creator-workers/" + self.version,
            kind="CREATOR_LLM_WORKSPACE",
        )
        (workspace / "request.json").write_bytes(payload)
        start = time.monotonic()
        packet = json.loads(payload)
        previous: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []
        upload_bytes = download_bytes = 0
        for stage in ("analysis", "writing", "editing", "source_audit"):
            if cancelled():
                raise InterruptedError("Cancelled before next cloud stage")
            stage_payload = json.dumps(
                {**packet, "stage": stage, "previous": previous}, ensure_ascii=False
            ).encode()
            if len(stage_payload) > 64 * 1024:
                raise ValueError("Cloud stage request exceeds budget")
            stage_workspace = (
                self.root / "runtime" / ("creator-workers-stage-" + uuid4().hex)
            )
            stage_workspace.mkdir()
            stage_marker = create_workspace_marker(
                self.root,
                stage_workspace,
                "creator-workers/" + self.version + "/" + stage,
                kind="CREATOR_LLM_WORKSPACE",
            )
            (stage_workspace / "request.json").write_bytes(stage_payload)
            (workspace / "stage-progress.json").write_text(
                json.dumps(
                    {
                        "active_stage": stage,
                        "active_run": stage_workspace.name,
                        "completed_requests": requests,
                    }
                ),
                encoding="utf-8",
            )
            stage_start = time.monotonic()
            raw = post_owned(
                stage_workspace,
                stage_marker,
                self.endpoint,
                stage_payload,
                key,
                cancelled,
            )
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Workers stage response exceeds budget")
            (stage_workspace / "response.json").write_bytes(raw)
            reply = json.loads(raw)
            if (
                not isinstance(reply, dict)
                or reply.get("model") != self.model.component_id
                or reply.get("worker_version") != self.worker_version
            ):
                raise ValueError("Worker stage model/version identity mismatch")
            upload_bytes += len(stage_payload)
            download_bytes += len(raw)
            requests.append(
                {
                    "stage": stage,
                    "run": stage_workspace.name,
                    "elapsed_seconds": round(time.monotonic() - stage_start, 3),
                    "upload_bytes": len(stage_payload),
                    "download_bytes": len(raw),
                    "input_sha256": hashlib.sha256(stage_payload).hexdigest(),
                    "output_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            if stage != "source_audit":
                item = reply.get("stage_result")
                if (
                    not isinstance(item, dict)
                    or item.get("stage") != stage
                    or not isinstance(item.get("value"), dict)
                ):
                    raise ValueError("Worker stage chain is incomplete or reordered")
                previous.append(item)
        if cancelled():
            raise InterruptedError(
                "Cancelled; remote response ignored, no Draft committed"
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Workers AI response exceeds budget")
        (workspace / "response.json").write_bytes(raw)
        (workspace / "stage-progress.json").write_text(
            json.dumps({"active_stage": None, "completed_requests": requests}),
            encoding="utf-8",
        )
        envelope = json.loads(raw)
        if envelope.get("model") != self.model.component_id:
            raise ValueError("Worker model identity mismatch")
        if envelope.get("worker_version") != self.worker_version:
            raise ValueError("Worker version identity mismatch")
        completion = envelope["result"]
        content = (
            completion["choices"][0]["message"]["content"]
            if "choices" in completion
            else completion["response"]
        )
        if isinstance(content, str):
            content = json.loads(content)
        stages = envelope.get("stages")
        if (
            not isinstance(stages, list)
            or len(stages) != 4
            or any(not isinstance(stage, dict) for stage in stages)
            or [stage.get("stage") for stage in stages]
            != ["analysis", "writing", "editing", "source_audit"]
            or not isinstance(stages[2].get("body"), str)
            or not isinstance(content, dict)
            or content.get("draft_body") != stages[2]["body"]
        ):
            raise ValueError("Worker annotation changed the exact edited article")
        result = validate_response(content, materials)
        validate_quality_audit(result, envelope.get("quality_audit"))
        result["provenance"] = {
            "backend": self.name,
            "version": self.version,
            "model": self.model.component_id,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "upload_bytes": upload_bytes,
            "download_bytes": download_bytes,
            "usage": completion.get("usage"),
            "worker_version": self.worker_version,
            "stages": envelope.get("stages"),
            "requests": requests,
            "quality_audit": envelope["quality_audit"],
            "prompt_sha256": hashlib.sha256(payload).hexdigest(),
            "run": workspace.name,
            "remote_cancel": "Cannot guarantee GPU cancellation; no local Draft commit after cancellation",
        }
        return result
