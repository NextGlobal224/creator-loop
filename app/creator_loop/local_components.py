"""Read-only checks of explicitly selected external engine/model artifacts.

No execution, download, activation, registry mutation or ownership acquisition.
The caller runs hashing outside the UI and retains the lease through use.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from creator_loop.publication_media import _open_read_lock

MANIFEST_LIMIT = 64 * 1024
HASH_CHUNK = 1024 * 1024
DEFAULT_WORKER_MEMORY = 512 * 1024 * 1024
_FIELDS = {
    "component_id",
    "kind",
    "path",
    "version",
    "sha256",
    "byte_size",
    "source_url",
    "license",
    "license_url",
    "local_use_allowed",
    "worker_memory_bytes",
    "owned",
}


class ComponentPreflightError(ValueError):
    pass


@dataclass(frozen=True)
class ComponentSpec:
    component_id: str
    kind: str
    path: Path
    version: str
    sha256: str
    byte_size: int
    source_url: str
    license: str
    license_url: str
    worker_memory_bytes: int
    local_use_allowed: bool
    owned: bool


@dataclass(frozen=True)
class VerifiedComponent:
    spec: ComponentSpec
    stream: BinaryIO


def _text(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= limit
        and not any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    )


def _source_url(value: Any) -> bool:
    if not _text(value, 2048):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and url.port in (None, 443)
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ComponentPreflightError("Duplicate component metadata field")
        result[key] = value
    return result


def parse_component_manifest(raw: bytes) -> tuple[ComponentSpec, ...]:
    """Validate declarations; version/license are declarations, not probes."""
    if len(raw) > MANIFEST_LIMIT:
        raise ComponentPreflightError("Component metadata exceeds 64 KiB")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ComponentPreflightError("Malformed component metadata") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"manifest_version", "components"}
        or type(payload["manifest_version"]) is not int
        or payload["manifest_version"] != 1
        or not isinstance(payload["components"], list)
        or not 1 <= len(payload["components"]) <= 16
    ):
        raise ComponentPreflightError("Component manifest version/list invalid")
    specs: list[ComponentSpec] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for entry in payload["components"]:
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise ComponentPreflightError("Component fields incomplete or unknown")
        component_id = entry["component_id"]
        if (
            not isinstance(component_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", component_id) is None
            or component_id in ids
            or entry["kind"] not in ("ENGINE", "MODEL")
            or not _text(entry["path"], 4096)
            or not Path(entry["path"]).is_absolute()
            or not _text(entry["version"], 128)
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
            or type(entry["byte_size"]) is not int
            or not 0 < entry["byte_size"] < 2**63
            or not _source_url(entry["source_url"])
            or not _source_url(entry["license_url"])
            or not _text(entry["license"], 256)
            or entry["local_use_allowed"] is not True
            or entry["owned"] is not False
            or type(entry["worker_memory_bytes"]) is not int
            or not 64 * 1024**2 <= entry["worker_memory_bytes"] <= 4 * 1024**3
        ):
            raise ComponentPreflightError(
                "Invalid, unlicensed or duplicate component declaration"
            )
        path = Path(entry["path"])
        path_key = os.path.normcase(str(path.absolute()))
        if path_key in paths:
            raise ComponentPreflightError("Duplicate component path")
        ids.add(component_id)
        paths.add(path_key)
        specs.append(
            ComponentSpec(
                component_id,
                entry["kind"],
                path,
                entry["version"],
                entry["sha256"],
                entry["byte_size"],
                entry["source_url"],
                entry["license"],
                entry["license_url"],
                entry["worker_memory_bytes"],
                entry["local_use_allowed"],
                entry["owned"],
            )
        )
    return tuple(specs)


def load_component_manifest(path: Path) -> tuple[ComponentSpec, ...]:
    try:
        with _open_read_lock(path) as stream:
            return parse_component_manifest(stream.read(MANIFEST_LIMIT + 1))
    except OSError as exc:
        raise ComponentPreflightError("Component metadata unavailable") from exc


def component_manifest_bytes(specs: tuple[ComponentSpec, ...]) -> bytes:
    """Stable declarations for review; excludes timestamps and observed state."""
    raw = json.dumps(
        {
            "manifest_version": 1,
            "components": [
                {
                    **{field: getattr(spec, field) for field in _FIELDS - {"path"}},
                    "path": str(spec.path),
                }
                for spec in specs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    parse_component_manifest(raw)
    return raw


def component_review_fingerprint(specs: tuple[ComponentSpec, ...]) -> str:
    return hashlib.sha256(component_manifest_bytes(specs)).hexdigest()


def _canonical_file(path: Path) -> Path:
    canonical = path.resolve(strict=True)
    if os.path.normcase(str(path.absolute())) != os.path.normcase(str(canonical)):
        raise ComponentPreflightError(
            "Component path must be canonical without aliases"
        )
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ComponentPreflightError("Linked/reparse component path refused")
    return canonical


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@contextmanager
def hold_verified_components(
    specs: tuple[ComponentSpec, ...],
    *,
    worker_memory_limit: int = DEFAULT_WORKER_MEMORY,
    timeout_seconds: float = 120,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[tuple[VerifiedComponent, ...]]:
    """Retain read-only handles; Windows denies write/delete through aliases.

    A sequential worker's declared commit budget must fit its intended Job cap.
    This does not measure RAM or establish engine/model format compatibility.
    Cooperative deadline/cancel checks bound hashing between file reads; run
    this service in an owned timeout worker for unresponsive storage.
    """
    if (
        type(worker_memory_limit) is not int
        or not 64 * 1024**2 <= worker_memory_limit <= 4 * 1024**3
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
        or not 1 <= len(specs) <= 16
    ):
        raise ComponentPreflightError("Invalid preflight budget")
    # Validate programmatic callers through the same strict boundary as JSON.
    specs = parse_component_manifest(component_manifest_bytes(specs))
    deadline = time.monotonic() + timeout_seconds

    def check_interruption() -> None:
        if cancelled():
            raise InterruptedError("Component verification cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Component verification deadline exceeded")

    verified: list[VerifiedComponent] = []
    fingerprints: list[tuple[int, int, int, int]] = []
    identities: set[tuple[int, int]] = set()
    with ExitStack() as handles:
        for spec in specs:
            check_interruption()
            if spec.worker_memory_bytes > worker_memory_limit:
                raise ComponentPreflightError(
                    "Declared worker memory exceeds selected Job budget"
                )
            try:
                canonical = _canonical_file(spec.path)
                selected = canonical.stat()
                if not stat.S_ISREG(selected.st_mode) or selected.st_nlink != 1:
                    raise ComponentPreflightError(
                        "A regular unaliased component file is required"
                    )
                stream = handles.enter_context(_open_read_lock(canonical))
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size != spec.byte_size
                    or _identity(canonical.stat()) != _identity(before)
                    or (before.st_dev, before.st_ino) in identities
                ):
                    raise ComponentPreflightError(
                        "Component file type/size/identity mismatch"
                    )
                digest = hashlib.sha256()
                total = 0
                while chunk := stream.read(min(HASH_CHUNK, spec.byte_size - total + 1)):
                    check_interruption()
                    total += len(chunk)
                    if total > spec.byte_size:
                        raise ComponentPreflightError(
                            "Component grew during verification"
                        )
                    digest.update(chunk)
                check_interruption()
                if (
                    total != spec.byte_size
                    or digest.hexdigest() != spec.sha256
                    or _identity(os.fstat(stream.fileno())) != _identity(before)
                    or _canonical_file(spec.path) != canonical
                    or _identity(canonical.stat()) != _identity(before)
                ):
                    raise ComponentPreflightError("Component digest/identity mismatch")
                stream.seek(0)
                identities.add((before.st_dev, before.st_ino))
                verified.append(VerifiedComponent(spec, stream))
                fingerprints.append(_identity(before))
            except (InterruptedError, TimeoutError):
                raise
            except OSError as exc:
                raise ComponentPreflightError("Selected component unavailable") from exc
        for item, expected in zip(verified, fingerprints, strict=True):
            check_interruption()
            try:
                held = os.fstat(item.stream.fileno())
                if (
                    held.st_nlink != 1
                    or _identity(held) != expected
                    or _canonical_file(item.spec.path) != item.spec.path
                    or _identity(item.spec.path.stat()) != expected
                ):
                    raise ComponentPreflightError(
                        "Component changed before lease completion"
                    )
            except OSError as exc:
                raise ComponentPreflightError(
                    "Selected component unavailable before lease completion"
                ) from exc
        check_interruption()
        yield tuple(verified)


def check_components_cli(path: Path) -> int:
    """Artifact-only summary, without initializing a data root or executing files."""
    try:
        specs = load_component_manifest(path)
        with hold_verified_components(specs) as verified:
            print(
                json.dumps(
                    {
                        "check": "LOCAL_ARTIFACTS_VERIFIED",
                        "runtime_compatibility_verified": False,
                        "review_fingerprint": component_review_fingerprint(specs),
                        "review_manifest": json.loads(component_manifest_bytes(specs)),
                        "components": [
                            {
                                "component_id": item.spec.component_id,
                                "kind": item.spec.kind,
                                "version": item.spec.version,
                                "sha256": item.spec.sha256,
                                "byte_size": item.spec.byte_size,
                                "owned": False,
                            }
                            for item in verified
                        ],
                    }
                )
            )
        return 0
    except (ComponentPreflightError, InterruptedError, TimeoutError) as exc:
        print(json.dumps({"check": "FAILED", "reason": str(exc)}))
        return 1
