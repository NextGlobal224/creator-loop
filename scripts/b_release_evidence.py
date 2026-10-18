"""Separate B beta eligibility; never grants full beta or stable acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

from scripts.beta_release_evidence import DEFERRED, digest, exact

B_SCOPES = frozenset(
    {
        "analysis-direction",
        "text-quality",
        "image-caption-quality",
        "video-draft-quality",
        "automatic-manual",
        "snapshot-reopen",
        "source-citations",
        "review-publication-guards",
        "cloud-live-compatibility",
        "repository-integration",
        "native-b-ui",
        "native-lifecycle",
        "native-upgrade-backup-restore",
        "package-privacy",
        "independent-review",
        "required-ci",
    }
)
NATIVE = {
    "native-b-ui",
    "native-lifecycle",
    "native-upgrade-backup-restore",
    "snapshot-reopen",
    "package-privacy",
}


def check_b_evidence(
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    tag: str,
    *,
    cloud_config: bytes | None = None,
) -> None:
    exact(
        receipt,
        {
            "format",
            "channel",
            "scope",
            "artifact",
            "tested_packaged_artifact",
            "development_manifest_sha256",
            "deferred",
            "excluded",
            "component_compatibility",
            "scopes",
            "cloud",
            "outputs",
        },
    )
    if (
        type(receipt["format"]) is not int
        or receipt["format"] != 1
        or receipt["channel"] != "beta"
        or receipt["scope"] != "B"
        or manifest.get("release_channel") != "beta"
        or manifest.get("release_scope") != "B"
        or manifest.get("release_tag") != tag
        or not re.fullmatch(
            r"v" + re.escape(manifest["app_version"]) + r"-beta\.[1-9][0-9]*", tag
        )
    ):
        raise ValueError("B beta scope/channel/tag mismatch")
    if (
        receipt["tested_packaged_artifact"] is not True
        or not digest(receipt["development_manifest_sha256"])
        or receipt["artifact"]
        != {
            "commit": manifest["git_commit"],
            "ci_run": manifest["provenance"]["ci_run"],
            "zip_sha256": manifest["artifact_sha256"],
            "exe_sha256": manifest["files"]["CreatorLoop/CreatorLoop.exe"]["sha256"],
        }
    ):
        raise ValueError("B eligibility must bind exact tested ZIP/EXE/build")
    if receipt["deferred"] != sorted(DEFERRED) or receipt["excluded"] != [
        "A-new-scan",
        "C-render-voice-quality",
        "full-beta",
        "stable-V1",
    ]:
        raise ValueError("B release must preserve full project boundaries")
    if receipt["component_compatibility"] != manifest[
        "component_compatibility"
    ] or receipt["component_compatibility"] != {
        "status": "VERIFIED_B_RUNTIME",
        "evidence_sha256": receipt["cloud"].get("runtime_evidence_sha256"),
    }:
        raise ValueError("B runtime compatibility binding mismatch")
    scopes = receipt["scopes"]
    if not isinstance(scopes, list) or len(scopes) != len(B_SCOPES):
        raise ValueError("All B release criteria are required")
    ids = []
    for entry in scopes:
        exact(entry, {"id", "status", "method", "evidence_sha256"})
        ids.append(entry["id"])
        if (
            entry["status"] != "PASS"
            or not digest(entry["evidence_sha256"])
            or entry["method"] not in {"NATIVE_ARTIFACT", "REUSED_COMPATIBLE_EVIDENCE"}
            or (entry["id"] in NATIVE and entry["method"] != "NATIVE_ARTIFACT")
        ):
            raise ValueError("B quality/artifact evidence missing or unverified")
    if set(ids) != B_SCOPES or len(set(ids)) != len(ids):
        raise ValueError("Missing or duplicate B release criteria")
    cloud = exact(
        receipt["cloud"],
        {
            "provider",
            "model",
            "worker_version",
            "config_sha256",
            "live_evidence_sha256",
            "runtime_evidence_sha256",
            "compatibility_evidence_sha256",
            "source_files",
            "format_map",
        },
    )
    if cloud_config is None or len(cloud_config) > 4096:
        raise ValueError("Actual packaged cloud configuration required")
    config = json.loads(cloud_config)
    if hashlib.sha256(cloud_config).hexdigest() != cloud["config_sha256"]:
        raise ValueError("Cloud config bytes differ from evidence digest")
    if (
        not isinstance(config, dict)
        or set(config) != {"endpoint", "model", "worker_version", "key_file"}
        or config["endpoint"]
        != "https://creator-loop-ai-beta.creator-loop-ai-beta.workers.dev/b/generate"
        or config["key_file"] != "secrets/creator-cloud-key"
        or config["model"] != cloud["model"]
        or config["worker_version"] != cloud["worker_version"]
    ):
        raise ValueError(
            "Cloud receipt identity differs from actual package configuration"
        )
    if (
        cloud["provider"] != "CLOUDFLARE_WORKERS_AI"
        or cloud["model"] != "@cf/openai/gpt-oss-120b"
        or str(UUID(cloud["worker_version"])) != cloud["worker_version"]
        or cloud["source_files"] != manifest.get("source_hashes")
        or not cloud["source_files"]
        or not all(
            digest(cloud[k])
            for k in (
                "config_sha256",
                "live_evidence_sha256",
                "runtime_evidence_sha256",
                "compatibility_evidence_sha256",
            )
        )
        or cloud["config_sha256"]
        != manifest["files"]
        .get("CreatorLoop/examples/B/creator-cloud.json", {})
        .get("sha256")
        or cloud["format_map"]
        != {
            "TEXT": "Workers AI cloud; local persistence/review",
            "IMAGE_CAPTION": "configured Codex/local backend; local image association",
            "VIDEO": "configured Codex/local backend; B script only, C separate",
        }
    ):
        raise ValueError("Exact B cloud package/live/source mapping required")
    outputs = receipt["outputs"]
    if not isinstance(outputs, list) or len(outputs) != 3:
        raise ValueError("Real representative B output bundle required")
    seen = set()
    for item in outputs:
        exact(
            item,
            {
                "format",
                "version",
                "body_sha256",
                "package_path",
                "quality_evidence_sha256",
            },
        )
        kind = item["format"]
        path = {
            "TEXT": "TEXT.md",
            "IMAGE_CAPTION": "IMAGE_CAPTION.md",
            "VIDEO": "VIDEO.md",
        }.get(kind)
        if (
            path is None
            or kind in seen
            or not re.fullmatch(r"[0-9a-f]{32}", str(item["version"]))
            or item["package_path"] != "CreatorLoop/examples/B/" + path
            or not digest(item["quality_evidence_sha256"])
            or item["body_sha256"]
            != manifest["files"].get(item["package_path"], {}).get("sha256")
        ):
            raise ValueError("B output does not bind packaged original Draft bytes")
        seen.add(kind)
    if (
        not {
            "CreatorLoop/examples/B/image.png",
            "CreatorLoop/examples/B/outputs.json",
            "CreatorLoop/docs/B_BETA_GUIDE.html",
        }
        <= manifest["files"].keys()
    ):
        raise ValueError("B image/provenance/offline guide missing")
