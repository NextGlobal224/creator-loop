"""Record the already-tested ZIP and its packaged files without rebuilding bytes."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop import __version__
from creator_loop.database import MIGRATIONS, SCHEMA_VERSION, _migration_sql
from creator_loop.installation_stage import load_release_manifest


def write_manifest(
    zip_path: Path,
    package: Path,
    output: Path,
    commit: str,
    run_url: str,
    *,
    build_method: str = "github-actions",
) -> None:
    if build_method not in ("github-actions", "local-checkpoint"):
        raise ValueError("Unknown build provenance method")
    if (
        platform.system() != "Windows"
        or platform.machine().upper() not in ("AMD64", "X86_64")
        or sys.maxsize <= 2**32
    ):
        raise ValueError("Windows x64 build host is required for this manifest")
    files = {}
    for path in sorted(package.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked package file")
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[f"CreatorLoop/{path.relative_to(package).as_posix()}"] = {
                "size": path.stat().st_size,
                "sha256": digest,
            }
    with zip_path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "manifest_version": 1,
        "app_version": __version__,
        "git_commit": commit,
        "schema_from": 0,
        "schema_to": SCHEMA_VERSION,
        "schema_read_min": SCHEMA_VERSION,
        "schema_read_max": SCHEMA_VERSION,
        "target": "win-x64",
        "artifact_sha256": digest,
        "tested_artifact": True,
        "build_time": datetime.now(timezone.utc).isoformat(),
        "minimum_os": "Windows 10 x64 build 19045 (oldest tested local host)",
        "runtime": f"Bundled CPython {platform.python_version()} x64 and Qt runtime",
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("PySide6", "PyInstaller")
        },
        "component_compatibility": {
            "status": "External engine/model compatibility unverified; components are not bundled; smoke does not run inference"
        },
        "migration_ids": list(MIGRATIONS),
        "migration_checksums": {
            migration: _migration_sql(migration)[1] for migration in MIGRATIONS
        },
        "files": files,
        "provenance": {
            "repository": "NextGlobal224/creator-loop",
            "ci_run": run_url,
            "method": (
                "GitHub Actions Windows build; signatures/attestation not enabled"
                if build_method == "github-actions"
                else "Local Windows development checkpoint; not CI/tag/release; signatures/attestation not enabled"
            ),
        },
        "release_notes": "Development checkpoint; Library through manual Observation, DB-only backup and verified staging. V1 release gate remains incomplete.",
        "recovery_notes": "Keep the prior installation and validated DB backup. Do not run an app outside its readable schema range. Confirm loss of changes after backup before any DB restore; DB backup does not include media. Use Maintenance for compatible activation or confirmed restore; interrupted/guarded restore requires fresh review, explicit loss/media consent and separate candidate health before activation. Retain journals/raw evidence/current bytes; never copy a backup over an open DB. Final recovery/hardware/release validation remains required.",
    }
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    load_release_manifest(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument(
        "--build-method",
        choices=("github-actions", "local-checkpoint"),
        default="github-actions",
    )
    args = parser.parse_args()
    write_manifest(
        args.zip,
        args.package,
        args.output,
        args.commit,
        args.run_url,
        build_method=args.build_method,
    )
