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
from creator_loop.database import MIGRATIONS, SCHEMA_VERSION
from creator_loop.installation_stage import load_release_manifest


def write_manifest(
    zip_path: Path, package: Path, output: Path, commit: str, run_url: str
) -> None:
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
            "status": "Engine/model selection unresolved; no external engine required for smoke"
        },
        "migration_ids": list(MIGRATIONS),
        "files": files,
        "provenance": {
            "repository": "NextGlobal224/creator-loop",
            "ci_run": run_url,
            "method": "GitHub Actions Windows build; signatures/attestation not enabled",
        },
        "release_notes": "Development checkpoint; Library through manual Observation, DB-only backup and verified staging. V1 release gate remains incomplete.",
        "recovery_notes": "Keep the prior installation and validated DB backup. Do not run an app outside its readable schema range. Confirm loss of changes after backup before any DB restore; media is not included. Activation/restore UI remains pending.",
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
    args = parser.parse_args()
    write_manifest(args.zip, args.package, args.output, args.commit, args.run_url)
