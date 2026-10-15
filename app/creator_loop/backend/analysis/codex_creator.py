"""Explicit Codex CLI text provider; native auth stays with Codex, never copied."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.analysis.creator_llm import (
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_CHARACTERS,
    response_schema,
    validate_response,
)
from creator_loop.backend.analysis.generation_prompt import generation_prompt
from creator_loop.backend.runtime.local_components import (
    component_review_fingerprint,
    hold_verified_components,
    load_component_manifest,
)
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
from creator_loop.backend.runtime.runtime_ownership import (
    bind_workspace_child,
    create_workspace_marker,
)
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial


@dataclass(frozen=True)
class RemoteModel:
    component_id: str = "gpt-6.1-sol"
    version: str = "high"


class CodexCreator:
    name = "CODEX_CLI_TEXT"

    def __init__(self, root: Path, manifest: Path | None = None) -> None:
        self.root = root.resolve(strict=True)
        self.specs = load_component_manifest(
            manifest or self.root / "manifests/creator-codex-components.json"
        )
        if (
            len(self.specs) != 1
            or self.specs[0].kind != "ENGINE"
            or self.specs[0].component_id != "creator-codex-engine"
        ):
            raise ValueError("Select an explicit verified Codex CLI engine")
        self.engine = self.specs[0]
        self.model = RemoteModel()
        self.version = (
            component_review_fingerprint(self.specs)[:20]
            + "/gpt-6.1-sol/high/prompt-v3-tools-disabled"
        )

    def generate(
        self,
        request: CreatorRequest,
        materials: Sequence[ResearchMaterial],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        if (
            not materials
            or sum(len(m.text) for m in materials) > MAX_SOURCE_CHARACTERS
            or any(not m.text.strip() for m in materials)
        ):
            raise ValueError("Select bounded, processed text for B")
        workspace = self.root / "runtime" / ("creator-codex-" + uuid4().hex)
        workspace.mkdir()
        component = "creator-codex/" + self.version
        marker = create_workspace_marker(
            self.root, workspace, component, kind="CREATOR_LLM_WORKSPACE"
        )
        schema = workspace / "response-schema.json"
        schema.write_text(
            json.dumps(
                response_schema([m.material_id for m in materials]), ensure_ascii=False
            ),
            encoding="utf-8",
        )
        prompt = generation_prompt(request, materials)
        input_file = workspace / "prompt.txt"
        input_file.write_text(prompt, encoding="utf-8")
        answer = workspace / "answer.json"
        args = [
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-6.1-sol",
            "-c",
            'model_reasoning_effort="high"',
            "--color",
            "never",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(answer),
            "-",
        ]
        # Disable capabilities rather than rely on untrusted input's no-tools prose.
        for override in (
            "features.shell_tool=false",
            "features.apps=false",
            "features.browser_use=false",
            "features.browser_use_external=false",
            "features.browser_use_full_cdp_access=false",
            "features.computer_use=false",
            "features.in_app_browser=false",
            "features.view_image=false",
            "features.code_mode=false",
            "features.code_mode_host=false",
            "apps._default.enabled=false",
            "plugins._default.enabled=false",
            "skills.include_instructions=false",
            "features.skip_host_skill_discovery=true",
            'web_search="disabled"',
        ):
            args[args.index("--color") : args.index("--color")] = ["-c", override]
        deadline = time.monotonic() + 360
        with (
            hold_verified_components(
                self.specs,
                worker_memory_limit=self.engine.worker_memory_bytes,
                cancelled=cancelled,
                timeout_seconds=30,
            ),
            input_file.open("rb") as source,
        ):
            with OwnedWindowsProcess(
                self.engine.path,
                args,
                workspace / "process",
                component_version=component,
                cwd=workspace,
                stdin_source=source,
                memory_limit_bytes=self.engine.worker_memory_bytes,
                before_resume=lambda child: bind_workspace_child(
                    workspace, marker, child
                ),
            ) as process:
                while (outcome := process.poll()) is None:
                    if cancelled():
                        raise InterruptedError("Đã hủy B; chưa lưu workflow/Draft")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Codex B worker exceeded deadline")
                    if answer.exists() and answer.stat().st_size > MAX_RESPONSE_BYTES:
                        raise ValueError("Codex response exceeds budget")
                    time.sleep(0.05)
                if outcome.exit_code != 0:
                    raise RuntimeError(
                        f"Codex B exited {outcome.exit_code}; no fallback"
                    )
        if not answer.is_file() or answer.stat().st_size > MAX_RESPONSE_BYTES:
            raise ValueError("Codex B response missing/oversized")
        result = validate_response(
            json.loads(answer.read_text(encoding="utf-8")), materials
        )
        result["provenance"] = {
            "backend": self.name,
            "version": self.version,
            "model": "gpt-6.1-sol",
            "reasoning": "high",
            "engine_sha256": self.engine.sha256,
            "response_sha256": hashlib.sha256(answer.read_bytes()).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
            "run": workspace.name,
        }
        return result
