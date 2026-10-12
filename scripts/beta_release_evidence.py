"""Fail-closed beta evidence; stable requirements and historical FAIL stay intact."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

DEFERRED = frozenset(
    {
        "physical_8gb_ram_disk",
        "representative_vi_quality",
        "external_published_observation",
    }
)
REQUIRED = frozenset(
    {f"requirement-{i:02d}-beta" for i in range(1, 24)} | {"facebook-native"}
)
STEPS = frozenset(
    {
        "keyword_search",
        "real_post_link",
        "source_saved",
        "reopened",
        "rescan_deduplicated",
        "cancel_owned_live_browser",
        "normal_close",
        "responsive_ui",
        "auth_captcha_stop",
    }
)


ARTIFACT_SCOPES = frozenset(
    {"facebook-native", *(f"requirement-{i:02d}-beta" for i in (1, 18, 19, 21, 22, 23))}
)
REUSE_FILES = {
    2: {"library.py", "originals.py", "source_association.py"},
    3: {"text_intake.py"},
    4: {"media_intake.py"},
    5: {"storage_relocation.py", "storage_paths.py", "storage_roots.py"},
    6: {"library.py", "processing_witness.py", "processing_recovery.py"},
    7: {"image_thumbnail.py"},
    8: {
        "text_evidence.py",
        "image_evidence.py",
        "video_evidence.py",
        "audio_evidence.py",
    },
    9: {"locator.py"},
    10: {
        "evidence_review.py",
        "evidence_correction.py",
        "claims.py",
        "claim_review.py",
    },
    11: {"projects.py"},
    12: {"drafts.py"},
    13: {"selections.py"},
    14: {"publication_media.py"},
    15: {"packages.py"},
    16: {"publication_policy.py", "packages.py"},
    17: {"observations.py"},
    20: {"transcription_task.py", "transcript_evidence.py", "whisper_adapter.py"},
}


def exact(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Unexpected beta receipt fields; private payload refused")
    return value


def digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def check_beta_evidence(
    receipt: dict[str, Any], manifest: dict[str, Any], tag: str
) -> None:
    exact(
        receipt,
        {
            "format",
            "channel",
            "artifact",
            "tested_packaged_artifact",
            "deferred",
            "scopes",
            "facebook",
            "component_compatibility",
            "development_manifest_sha256",
        },
    )
    if not digest(receipt["development_manifest_sha256"]):
        raise ValueError("Original CI manifest identity required")
    if receipt["component_compatibility"] != manifest["component_compatibility"]:
        raise ValueError("Component receipt/manifest mismatch")
    if (
        receipt.get("format") != 1
        or type(receipt.get("format")) is not int
        or receipt.get("channel") != "beta"
        or manifest.get("release_channel") != "beta"
        or manifest.get("release_tag") != tag
        or not re.fullmatch(
            r"v" + re.escape(manifest["app_version"]) + r"-beta\.[1-9][0-9]*", tag
        )
    ):
        raise ValueError("Beta channel/tag/version mismatch")
    expected = {
        "commit": manifest["git_commit"],
        "ci_run": manifest["provenance"]["ci_run"],
        "zip_sha256": manifest["artifact_sha256"],
        "exe_sha256": manifest["files"]["CreatorLoop/CreatorLoop.exe"]["sha256"],
    }
    if (
        receipt.get("artifact") != expected
        or receipt.get("tested_packaged_artifact") is not True
    ):
        raise ValueError(
            "Beta evidence does not bind the exact final packaged artifact"
        )
    deferred = receipt.get("deferred")
    if (
        not isinstance(deferred, list)
        or len(deferred) != 3
        or set(deferred) != DEFERRED
    ):
        raise ValueError("Only the three approved stable deferrals are allowed")
    scopes = receipt.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != len(REQUIRED):
        raise ValueError("All beta requirement scopes must remain represented")
    ids = []
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or scope.get("status") != "PASS"
            or not digest(scope.get("evidence_sha256"))
        ):
            raise ValueError("Required beta scope lacks PASS evidence")
        ids.append(scope.get("id"))
        if scope.get("method") not in ("NATIVE_ARTIFACT", "REUSED_UNCHANGED_SOURCE"):
            raise ValueError("Unverified beta evidence method")
        if scope["method"] == "NATIVE_ARTIFACT":
            exact(scope, {"id", "status", "method", "evidence_sha256"})
        else:
            exact(scope, {"id", "status", "method", "evidence_sha256", "source_files"})
            if scope["id"] in ARTIFACT_SCOPES:
                raise ValueError(
                    "Final artifact scope cannot reuse source-only evidence"
                )
            number = int(scope["id"].split("-")[1])
            needed = {
                "app/creator_loop/" + name for name in REUSE_FILES.get(number, set())
            }
            source_files = scope["source_files"]
            if (
                not needed
                or not isinstance(source_files, dict)
                or set(source_files) != needed
            ):
                raise ValueError("Exact reusable module source set required")
            if any(
                not digest(sha) or manifest.get("source_hashes", {}).get(path) != sha
                for path, sha in source_files.items()
            ):
                raise ValueError("Reused source differs from packaged source identity")
    if len(set(ids)) != len(REQUIRED) or set(ids) != REQUIRED:
        raise ValueError("Missing/duplicate beta scope IDs")
    fb = exact(
        receipt.get("facebook"),
        {
            "method",
            "tested_artifact",
            "no_meta_api_or_service",
            "real_post_url_sha256",
            "browser_pin_sha256",
            "steps",
        },
    )
    if (
        not isinstance(fb, dict)
        or fb.get("method") != "NATIVE_ARTIFACT"
        or fb.get("tested_artifact") is not True
        or fb.get("no_meta_api_or_service") is not True
        or not digest(fb.get("real_post_url_sha256"))
        or not digest(fb.get("browser_pin_sha256"))
        or fb["browser_pin_sha256"]
        != manifest["files"]
        .get(
            "CreatorLoop/_internal/creator_loop/resources/facebook-browser-pin.json", {}
        )
        .get("sha256")
        or set(fb.get("steps", {})) != STEPS
        or any(v is not True for v in fb["steps"].values())
    ):
        raise ValueError("Real Facebook final-artifact flow is incomplete")
    exact(manifest["component_compatibility"], {"status", "engine", "model"})
    if (
        manifest["component_compatibility"].get("status")
        != "VERIFIED_BETA_LOCAL_RUNTIME"
    ):
        raise ValueError(
            "Beta local runtime status required; V1 quality is not accepted"
        )
    for kind in ("engine", "model"):
        component = exact(
            manifest["component_compatibility"].get(kind),
            {
                "name",
                "version",
                "source",
                "license",
                "sha256",
                "local_use_allowed",
                "native_runtime_evidence_sha256",
            },
        )
        source = urlparse(component["source"])
        allowed_hosts = {
            "engine": {"github.com"},
            "model": {"huggingface.co", "github.com"},
        }
        if (
            source.scheme != "https"
            or source.hostname not in allowed_hosts[kind]
            or source.username
            or source.password
            or source.query
            or source.fragment
        ):
            raise ValueError("Only canonical public component source URLs allowed")
        if (
            component["license"] not in ("MIT", "Apache-2.0", "BSD-3-Clause")
            or not digest(component["sha256"])
            or component["local_use_allowed"] is not True
        ):
            raise ValueError("Verified component permission/license/hash required")
        if any(
            not isinstance(component[k], str)
            or not re.fullmatch(r"[\w .+/-]{1,200}", component[k])
            for k in ("name", "version")
        ):
            raise ValueError("Invalid public component identity")
        if not isinstance(component, dict) or not digest(
            component.get("native_runtime_evidence_sha256")
        ):
            raise ValueError("Beta component native runtime evidence required")
