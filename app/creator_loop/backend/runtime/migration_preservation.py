"""Full private migration preservation; no restore into live data or cleanup right.

Unassociated historical B workspaces remain opaque evidence. Normal update backup
is deliberately stricter and unchanged. A local archive is not a cloud backup.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO
from uuid import uuid4

from creator_loop.backend.library.storage_paths import resolve_storage_path
from creator_loop.backend.runtime.app_lock import AppDataLock
from creator_loop.backend.runtime.backup_runtime import hold_terminal_audio_runtime
from creator_loop.backend.runtime.corrupt_source_preservation import (
    _create_file,
    _publish_new_manifest,
)
from creator_loop.backend.runtime.database import _sqlite_uri, validate
from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.update_backup import _snapshot
from creator_loop.backend.runtime.windows_paths import resolve_file_path

FORMAT = "CREATOR_LOOP_PRESERVATION_V1"
MAX_BYTES = 128 * 1024**2
MAX_FILES = 10000
TABLES = (
    "projects",
    "project_references",
    "assets",
    "sources",
    "source_assets",
    "creator_workflows",
    "drafts",
    "draft_versions",
    "review_events",
    "processing_runs",
)


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or path.as_posix() != name
        or any(
            part in (".", "..")
            or part.endswith((".", " "))
            or PureWindowsPath(part).is_reserved()
            or any(ord(character) < 32 or character in '<>"|?*' for character in part)
            for part in path.parts
        )
    ):
        raise ValueError("Unsafe preservation path")
    return path


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
        for name in TABLES
    }


def _copy_locked(
    source: Path, target: BinaryIO, held: ExitStack, deadline: float
) -> dict[str, Any]:
    if sys.platform != "win32":
        raise OSError("Preservation reads require native Windows leases")
    lease = held.enter_context(RuntimeHandle(source, read_only=True))
    size = (lease.info.size_high << 32) | lease.info.size_low
    if size > MAX_BYTES:
        raise ValueError("Preservation file exceeds bounded capacity")
    buffer = ctypes.create_string_buffer(65536)
    received = ctypes.c_uint32()
    total, digest = 0, hashlib.sha256()
    while total < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Preservation deadline exceeded")
        wanted = min(len(buffer), size - total)
        if not lease.kernel.ReadFile(
            lease.handle, buffer, wanted, ctypes.byref(received), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not 0 < received.value <= wanted:
            raise OSError("Preservation source read incomplete")
        body = buffer.raw[: received.value]
        target.write(body)
        digest.update(body)
        total += len(body)
    return {"bytes": total, "sha256": digest.hexdigest()}


def verify_preservation_archive(stream: BinaryIO, isolated: Path) -> dict[str, Any]:
    """Reopen the exact archive into a new isolated directory; never run evidence.

    Portable validation can also inspect a later cloud-downloaded copy. That
    separate real observation must be recorded before claiming cloud restore.
    """
    isolated = isolated.absolute()
    isolated.mkdir(exist_ok=False)
    with ExitStack() as held, zipfile.ZipFile(stream) as archive:
        retained_directories: set[Path] = set()

        def directory(path: Path) -> None:
            if path in retained_directories:
                return
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked isolated restore directory")
            path.resolve(strict=True).relative_to(isolated.resolve(strict=True))
            if sys.platform == "win32":
                held.enter_context(
                    RuntimeHandle(path, directory=True, allow_child_writes=True)
                )
            retained_directories.add(path)

        directory(isolated)

        def destination_for(name: str) -> Path:
            relative = _relative(name)
            parent = isolated
            for part in relative.parts[:-1]:
                parent = parent / part
                parent.mkdir(exist_ok=True)
                directory(parent)
            destination = isolated.joinpath(*relative.parts)
            destination.resolve().relative_to(isolated.resolve(strict=True))
            return destination

        def retain_and_digest(path: Path) -> str:
            if sys.platform == "win32":
                held.enter_context(RuntimeHandle(path, read_only=True))
            with path.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()

        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or len(names) > MAX_FILES:
            raise ValueError("Duplicate or excessive preservation entries")
        if sum(item.file_size for item in entries) > MAX_BYTES:
            raise ValueError("Preservation expansion exceeds capacity")
        for item in entries:
            _relative(item.filename)
            if item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Linked/directory preservation member")
        if archive.getinfo("preservation.json").file_size > 8 * 1024**2:
            raise ValueError("Preservation manifest exceeds budget")
        manifest = json.loads(archive.read("preservation.json"))
        if not (
            manifest["format"] == FORMAT
            and manifest["permits_cleanup"] is False
            and manifest["cloud_restore_verified"] is False
            and set(names) == set(manifest["files"]) | {"preservation.json"}
        ):
            raise ValueError("Preservation identity/inventory mismatch")
        for name, expected in manifest["files"].items():
            _relative(name)
            if not (
                name in ("db/creator_loop.sqlite3", "db/backup-manifest.json")
                or re.fullmatch(r"media/[a-f0-9]{64}", name)
                or name.startswith("evidence/runtime/")
            ):
                raise ValueError("Unapproved preservation payload")
            target = destination_for(name)
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as source, target.open("xb") as output:
                while chunk := source.read(65536):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if {
                "bytes": size,
                "sha256": digest.hexdigest(),
            } != expected or retain_and_digest(target) != expected["sha256"]:
                raise ValueError("Preserved bytes differ from inventory")
        dbfile = isolated / "db/creator_loop.sqlite3"
        with closing(sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            validate(db, expected_version=manifest["schema_version"])
            if _counts(db) != manifest["table_counts"]:
                raise ValueError("Restored domain row counts differ")
            rows = db.execute(
                "SELECT file_id,role,storage_key,sha256,byte_size FROM asset_files ORDER BY file_id"
            ).fetchall()
            expected_media = [
                {
                    "file_id": fid,
                    "role": role,
                    "storage_key": key,
                    "sha256": sha,
                    "bytes": size,
                    "archive": "media/" + sha,
                }
                for fid, role, key, sha, size in rows
            ]
            if manifest["media"] != expected_media:
                raise ValueError("Restored media/ID mapping differs from domain")
            for media in expected_media:
                key = _relative(media["storage_key"])
                if len(key.parts) < 3 or key.parts[0] != "storage":
                    raise ValueError("Unsupported isolated media destination")
                record = manifest["files"][media["archive"]]
                if record != {"bytes": media["bytes"], "sha256": media["sha256"]}:
                    raise ValueError("Restored media digest differs from domain")
                # Rebuild the original relative locator in a fresh data root,
                # never follow the archived registration back to a live root.
                destination = destination_for(key.as_posix())
                payload = isolated / media["archive"]
                if not destination.exists():
                    with payload.open("rb") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                if retain_and_digest(destination) != media["sha256"]:
                    raise ValueError("Reopened media locator differs from domain")
        # Runtime is quarantined under evidence/, never restored as active work.
        return {
            "schema_version": manifest["schema_version"],
            "table_counts": manifest["table_counts"],
            "media_records": len(expected_media),
            "files_reopened": len(manifest["files"]),
            "unmatched_runtime_workspaces": len(manifest["opaque_runtime"]),
            "integrity_and_foreign_keys": "PASS",
            "all_archive_bytes_verified": True,
            "runtime_evidence_executed": False,
            "cloud_restore_verified": False,
            "permits_migration_or_cleanup": False,
        }


def create_preservation_archive(root: Path, *, timeout_seconds: float = 180) -> Path:
    """Private full archive under native locks; partial output is retained on error.

    Never change canonical rows/markers, recover jobs, delete originals or use
    unbound workspaces as terminal generation evidence. Outer timeout required.
    """
    if sys.platform != "win32":
        raise OSError("Canonical preservation requires native Windows leases")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise ValueError("Bounded preservation deadline required")
    root = resolve_file_path(root, strict=True)
    backups = root / "backups"
    identifier = uuid4().hex
    temporary = backups / (".preservation-" + identifier + ".pending")
    destination = backups / ("preservation-" + identifier + ".zip")
    work = backups / (".preservation-" + identifier + ".work")
    deadline = time.monotonic() + timeout_seconds
    with ExitStack() as held:
        held.enter_context(RuntimeHandle(root, directory=True, allow_child_writes=True))
        held.enter_context(
            RuntimeHandle(backups, directory=True, allow_child_writes=True)
        )
        held.enter_context(AppDataLock(root))
        db = held.enter_context(
            closing(
                sqlite3.connect(
                    _sqlite_uri(root / "creator_loop.sqlite3") + "?mode=rw",
                    uri=True,
                    timeout=10,
                )
            )
        )
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        held.callback(db.rollback)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (9, 10):
            raise ValueError("Preservation supports current B schema9/10 only")
        validate(db, expected_version=version)
        if db.execute(
            "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Active processing requires recovery before preservation"
            )
        opaque: list[dict[str, str]] = []
        recheck = held.enter_context(
            hold_terminal_audio_runtime(root, db, opaque_creator=opaque)
        )
        media = [
            {
                "file_id": fid,
                "role": role,
                "storage_key": key,
                "sha256": sha,
                "bytes": size,
                "archive": "media/" + sha,
            }
            for fid, role, key, sha, size in db.execute(
                "SELECT file_id,role,storage_key,sha256,byte_size FROM asset_files ORDER BY file_id"
            )
        ]
        runtime = []
        for path in (root / "runtime").rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked runtime preservation entry")
            if path.is_file() and path != root / "runtime/app-data.lock":
                runtime.append(path)
        runtime.sort()
        if len(runtime) + len(media) + 3 > MAX_FILES:
            raise ValueError("Preservation inventory exceeds capacity")
        estimate = (
            sum(item["bytes"] for item in media)
            + sum(p.stat().st_size for p in runtime)
            + (root / "creator_loop.sqlite3").stat().st_size
        )
        if (
            estimate > MAX_BYTES
            or shutil.disk_usage(backups).free < estimate * 3 + 16 * 1024**2
        ):
            raise OSError("Insufficient bounded preservation capacity")
        work.mkdir()
        (work / "database").mkdir()
        snapshot = _snapshot(
            root,
            root / "creator_loop.sqlite3",
            work / "database",
            version,
            max(0.1, deadline - time.monotonic()),
            recheck_runtime=recheck,
        )
        stream = _create_file(held, temporary)
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "created_at": time.time(),
            "schema_version": version,
            "table_counts": _counts(db),
            "media": media,
            "opaque_runtime": opaque,
            "files": {},
            "permits_cleanup": False,
            "cloud_restore_verified": False,
            "excluded_coordination_file": "runtime/app-data.lock",
            "evidence_status": "Preserved bytes only; unmatched provenance remains unknown",
        }
        with zipfile.ZipFile(
            stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:

            def add(source: Path, name: str) -> dict[str, Any]:
                _relative(name)
                with archive.open(name, "w") as output:
                    record = _copy_locked(source, output, held, deadline)
                manifest["files"][name] = record
                return record

            add(snapshot / "creator_loop.sqlite3", "db/creator_loop.sqlite3")
            add(snapshot / "backup-manifest.json", "db/backup-manifest.json")
            for item in media:
                _relative(item["storage_key"])
                if re.fullmatch("[a-f0-9]{64}", item["sha256"]) is None:
                    raise ValueError("Invalid source media digest")
                source = resolve_storage_path(root, item["role"], item["storage_key"])
                source.relative_to(
                    root
                )  # External roots require a separately supported path.
                if item["archive"] in manifest["files"]:
                    # Still read/lease every duplicate original, not just one filename.
                    with open(os.devnull, "wb") as sink:
                        record = _copy_locked(source, sink, held, deadline)
                else:
                    record = add(source, item["archive"])
                if record != {"bytes": item["bytes"], "sha256": item["sha256"]}:
                    raise ValueError(
                        "Original media differs from domain; no archive published"
                    )
            for source in runtime:
                add(
                    source,
                    "evidence/runtime/"
                    + source.relative_to(root / "runtime").as_posix(),
                )
            recheck()
            archive.writestr(
                "preservation.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
            )
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        proof = verify_preservation_archive(stream, work / "isolated")
        recheck()
        if time.monotonic() >= deadline:
            raise TimeoutError("Preservation deadline exceeded before publication")
        proof_path = work / "isolated-restore-proof.json"
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        _publish_new_manifest(temporary, destination, stream)
        return destination
