"""Validate and stage an explicitly supplied Windows ZIP without activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_FILES = 10000
MAX_MANIFEST_BYTES = 16 * 1024**2


def _safe_path(name: str) -> tuple[str, ...]:
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        len(parts) < 2
        or parts[0] != "CreatorLoop"
        or any(
            not part
            or part in (".", "..")
            or part.endswith((" ", "."))
            or part.split(".")[0].upper() in reserved
            for part in parts
        )
        or any(ord(char) < 32 or char in '\\:<>"|?*' for char in name)
    ):
        raise ValueError("Unsafe installation member path")
    return tuple(parts)


def load_release_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Release manifest exceeds metadata budget")
    manifest = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ValueError("Unsupported release manifest")
    for field, pattern in (
        ("app_version", r"[0-9]+\.[0-9]+\.[0-9]+"),
        ("git_commit", r"[0-9a-f]{40}"),
        ("artifact_sha256", r"[0-9a-f]{64}"),
    ):
        if not isinstance(manifest.get(field), str) or not re.fullmatch(
            pattern, manifest[field]
        ):
            raise ValueError(f"Invalid release {field}")
    if (
        manifest.get("target") != "win-x64"
        or manifest.get("tested_artifact") is not True
    ):
        raise ValueError("A tested Windows x64 artifact is required")
    for field in ("schema_from", "schema_to", "schema_read_min", "schema_read_max"):
        if type(manifest.get(field)) is not int or manifest[field] < 0:
            raise ValueError("Invalid schema compatibility")
    if not (
        manifest["schema_from"] <= manifest["schema_to"]
        and manifest["schema_read_min"]
        <= manifest["schema_to"]
        <= manifest["schema_read_max"]
    ):
        raise ValueError("Inconsistent schema compatibility")
    for field in (
        "build_time",
        "minimum_os",
        "runtime",
        "release_notes",
        "recovery_notes",
    ):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ValueError(f"Missing release {field}")
    for field in ("dependencies", "component_compatibility", "provenance"):
        if not isinstance(manifest.get(field), dict) or not manifest[field]:
            raise ValueError(f"Missing release {field}")
    if (
        not isinstance(manifest.get("migration_ids"), list)
        or not manifest["migration_ids"]
    ):
        raise ValueError("Missing migration IDs")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid release file inventory")
    total = 0
    folded: set[str] = set()
    prefixes: dict[str, str] = {}
    for name, entry in files.items():
        parts = _safe_path(name)
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            folded_prefix = prefix.casefold()
            if folded_prefix in prefixes and prefixes[folded_prefix] != prefix:
                raise ValueError("Case-colliding installation paths")
            prefixes[folded_prefix] = prefix
        if name.casefold() in folded:
            raise ValueError("Case-colliding installation members")
        folded.add(name.casefold())
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size"}
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or type(entry["size"]) is not int
            or entry["size"] < 0
        ):
            raise ValueError("Invalid release file metadata")
        total += entry["size"]
    for name in files:
        if any(
            "/".join(name.split("/")[:length]) in files
            for length in range(1, len(name.split("/")))
        ):
            raise ValueError("Installation file conflicts with a directory")
    if total > MAX_EXPANDED_BYTES or "CreatorLoop/CreatorLoop.exe" not in files:
        raise ValueError("Installation exceeds budget or lacks launcher")
    return manifest


def _installation_root(installation_root: Path, user_data_root: Path) -> Path:
    root = installation_root.resolve(strict=True)
    data = user_data_root.resolve()
    if (
        not root.is_dir()
        or installation_root.is_symlink()
        or installation_root.is_junction()
        or root.is_relative_to(data)
        or data.is_relative_to(root)
    ):
        raise ValueError(
            "Installation and user data roots must be separate real directories"
        )
    if not getattr(sys, "frozen", False):
        checkout = Path(__file__).resolve().parents[2]
        if root.is_relative_to(checkout) or checkout.is_relative_to(root):
            raise ValueError("Installation must be separate from the source checkout")
    return root


def preflight_installation(
    installation_root: Path, user_data_root: Path, manifest: dict[str, Any]
) -> None:
    root = _installation_root(installation_root, user_data_root)
    required = sum(entry["size"] for entry in manifest["files"].values()) + 1024**2
    if shutil.disk_usage(root).free < required:
        raise OSError("Insufficient installation staging space")
    probe = root / f".{uuid4().hex}.write-probe"
    created = False
    try:
        with probe.open("xb") as stream:
            created = True
            stream.write(b"Creator Loop installation preflight\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if created:
            probe.unlink(missing_ok=True)


def stage_installation(
    artifact: Path,
    manifest_path: Path,
    installation_root: Path,
    *,
    user_data_root: Path,
) -> Path:
    """Publish a verified candidate; retain every previous installation and DB.

    The supplied manifest is from the user's chosen distribution source. Hashes
    verify bytes against it; they do not independently authenticate that source.
    No candidate executable runs here. Updater orchestration/health/activation
    must happen separately, after the user-data backup and compatibility gate.
    """
    root = _installation_root(installation_root, user_data_root)
    manifest = load_release_manifest(manifest_path)
    files = manifest["files"]
    assert isinstance(files, dict)
    if (
        shutil.disk_usage(root).free
        < sum(entry["size"] for entry in files.values()) + 1024**2
    ):
        raise OSError("Insufficient installation staging space")
    stage_id = uuid4().hex
    temporary = root / f".{stage_id}.staging"
    completed = (
        root / f"{manifest['app_version']}-{manifest['git_commit'][:12]}-{stage_id}"
    )
    with artifact.open("rb") as source:
        if (
            hashlib.file_digest(source, "sha256").hexdigest()
            != manifest["artifact_sha256"]
        ):
            raise ValueError("Artifact digest mismatch")
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES * 2:
                raise ValueError("Archive entry budget exceeded")
            names: set[str] = set()
            actual_files: set[str] = set()
            for member in members:
                name = (
                    member.filename.rstrip("/") if member.is_dir() else member.filename
                )
                if member.is_dir() and name == "CreatorLoop":
                    pass
                else:
                    _safe_path(name)
                if name.casefold() in names or member.flag_bits & 1:
                    raise ValueError("Duplicate or encrypted archive member")
                names.add(name.casefold())
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise ValueError("Linked or special archive member")
                if not member.is_dir():
                    if name not in files or member.file_size != files[name]["size"]:
                        raise ValueError("Archive file inventory mismatch")
                    actual_files.add(name)
            if actual_files != set(files):
                raise ValueError("Archive lacks required inventory files")
            temporary.mkdir()
            try:
                for member in members:
                    if member.is_dir():
                        continue
                    destination = temporary.joinpath(*_safe_path(member.filename))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with (
                        archive.open(member) as compressed,
                        destination.open("xb") as output,
                    ):
                        for chunk in iter(lambda: compressed.read(1024**2), b""):
                            written += len(chunk)
                            if written > files[member.filename]["size"]:
                                raise ValueError("Expanded file exceeds declared size")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if (
                        digest.hexdigest() != files[member.filename]["sha256"]
                        or written != member.file_size
                    ):
                        raise ValueError("Installation file digest mismatch")
                with (temporary / "release-manifest.json").open(
                    "x", encoding="utf-8"
                ) as output_manifest:
                    json.dump(manifest, output_manifest, sort_keys=True, indent=2)
                    output_manifest.flush()
                    os.fsync(output_manifest.fileno())
                source.seek(0)
                if (
                    hashlib.file_digest(source, "sha256").hexdigest()
                    != manifest["artifact_sha256"]
                ):
                    raise ValueError("Artifact changed during staging")
                temporary.rename(completed)
                return completed
            except BaseException:
                if (
                    temporary.is_symlink()
                    or temporary.is_junction()
                    or temporary.resolve(strict=True).parent != root
                ):
                    raise OSError("Staging path changed; cleanup refused")
                shutil.rmtree(temporary)
                raise
