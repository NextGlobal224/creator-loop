"""Explicit B provider choice; opening never downloads, connects or infers."""

import json
from pathlib import Path
from uuid import uuid4

from creator_loop.backend.analysis.codex_creator import CodexCreator
from creator_loop.backend.analysis.creator_llm import LocalCreatorLLM
from creator_loop.backend.analysis.workers_creator import WorkersCreator
from creator_loop.backend.runtime.local_components import (
    component_manifest_bytes,
    load_component_manifest,
)


def select_component_provider(root: Path, manifest: Path) -> str:
    """Validate and persist an explicit component choice, without inference/network."""
    if manifest.stat().st_size <= 4096:
        selected = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(selected, dict) and "endpoint" in selected:
            cloud = WorkersCreator(root, manifest)
            _persist_provider_config(
                root,
                "creator-cloud.json",
                json.dumps(cloud.config).encode(),
                cloud.name,
            )
            return cloud.name
    specs = load_component_manifest(manifest)
    codex = len(specs) == 1 and specs[0].component_id == "creator-codex-engine"
    provider = (
        CodexCreator(root, manifest) if codex else LocalCreatorLLM(root, manifest)
    )
    _persist_provider_config(
        root,
        "creator-codex-components.json" if codex else "creator-llm-components.json",
        component_manifest_bytes(provider.specs),
        provider.name,
    )
    return provider.name


def _persist_provider_config(
    root: Path, filename: str, content: bytes, name: str
) -> None:
    folder = root / "manifests"
    folder.mkdir(exist_ok=True)
    destination = folder / filename
    if destination.exists():
        destination.with_name(
            destination.stem + "-previous-" + uuid4().hex + ".json"
        ).write_bytes(destination.read_bytes())
    temporary = destination.with_name(destination.stem + "-" + uuid4().hex + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)
    selector = folder / "creator-provider.json"
    if selector.exists():
        selector.with_name(
            "creator-provider-previous-" + uuid4().hex + ".json"
        ).write_bytes(selector.read_bytes())
    selected = folder / ("creator-provider-" + uuid4().hex + ".tmp")
    selected.write_text(json.dumps({"provider": name}), encoding="utf-8")
    selected.replace(selector)


def provider_name(root: Path) -> str:
    selector = root / "manifests/creator-provider.json"
    if not selector.exists():
        return "LOCAL_LLAMA_CPP"
    if selector.stat().st_size > 1024:
        raise ValueError("Provider choice exceeds budget")
    value = json.loads(selector.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"provider"}
        or value["provider"]
        not in ("LOCAL_LLAMA_CPP", "CODEX_CLI_TEXT", "CLOUDFLARE_WORKERS_AI")
    ):
        raise ValueError("Invalid explicit B provider")
    return str(value["provider"])


def provider_ready(root: Path, name_override: str | None = None) -> bool:
    name = name_override or provider_name(root)
    if name == "CLOUDFLARE_WORKERS_AI":
        return (root / "manifests/creator-cloud.json").is_file() and (
            root / "secrets/creator-cloud-key"
        ).is_file()
    return (
        root
        / "manifests"
        / (
            "creator-codex-components.json"
            if name == "CODEX_CLI_TEXT"
            else "creator-llm-components.json"
        )
    ).is_file()


def selected_provider(
    root: Path, name_override: str | None = None
) -> LocalCreatorLLM | CodexCreator | WorkersCreator:
    name = name_override or provider_name(root)
    if name == "CLOUDFLARE_WORKERS_AI":
        return WorkersCreator(root)
    return CodexCreator(root) if name == "CODEX_CLI_TEXT" else LocalCreatorLLM(root)
