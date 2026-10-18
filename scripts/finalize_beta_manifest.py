"""Finalize metadata only after exact beta QA; never rebuild/relabel ZIP bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_release_gate import check_release_gate


def finalize(
    archive: Path, manifest: Path, evidence: Path, tag: str, output: Path
) -> None:
    body = json.loads(manifest.read_text(encoding="utf-8"))
    receipt = json.loads(evidence.read_text(encoding="utf-8"))
    if (
        receipt.get("development_manifest_sha256")
        != hashlib.sha256(manifest.read_bytes()).hexdigest()
    ):
        raise ValueError("QA receipt does not bind original CI manifest")
    body.update(
        release_channel="beta",
        release_tag=tag,
        component_compatibility=receipt.get("component_compatibility"),
        release_notes=(
            "B-only beta: analysis, editorial direction and versioned TEXT/IMAGE_CAPTION/VIDEO Drafts; Cloudflare TEXT integration. A new scan, C render/voice quality, full beta and stable V1 are NOT ACCEPTED. "
            if receipt.get("scope") == "B"
            else "Beta: local Facebook collection and immutable research snapshots. "
        )
        + "Physical 8 GB RAM/disk pressure, representative Vietnamese ASR quality and external PUBLISHED/Observation are NOT ACCEPTED and deferred to stable.",
        recovery_notes=f"Keep prior installation and consistent pre-migration SQLite backup. Schema {body['schema_to']}; readable schema range {body['schema_read_min']}–{body['schema_read_max']}. Never open a migrated DB with an incompatible app; restore compatible backup plus prior installation only after confirming loss of newer DB changes. DB backup excludes media; retain Original/media separately. Credentials stay in private data root. Stable scopes are NOT ACCEPTED.",
    )
    if receipt.get("scope") == "B":
        body["release_scope"] = "B"
    temporary = output.with_suffix(".validation.tmp")
    temporary.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    try:
        check_release_gate(
            archive,
            temporary,
            Path("docs/PRODUCT_ACCEPTANCE.md"),
            expected_commit=body["git_commit"],
            expected_run_url=body["provenance"]["ci_run"],
            tag=tag,
            beta_evidence=evidence,
        )
        temporary.replace(output)
        output.with_name("BETA-NOTES.md").write_text(
            f"# Creator Loop {tag}\n\nBeta, not stable V1.\n\nSource: `{body['git_commit']}`\nBuild: {body['provenance']['ci_run']}\nZIP SHA256: `{body['artifact_sha256']}`\n\n{body['release_notes']}\n\n{body['recovery_notes']}\n",
            encoding="utf-8",
        )
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("artifact", "manifest", "evidence", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    finalize(args.artifact, args.manifest, args.evidence, args.tag, args.output)
