"""Revalidate a held separate corrupt-restore stage; never open the live DB."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from creator_loop.corrupt_database import _identity, _regular
from creator_loop.database import SCHEMA_VERSION, validate
from creator_loop.preserved_source_validation import (
    _unique_object,
    hold_preserved_source,
)
from creator_loop.publication_media import _open_read_lock
from creator_loop.restore_assessment import _database_identity
from creator_loop.runtime_files import RuntimeHandle

_MAX_MANIFEST = 16 * 1024**2
_KEYS = {
    "corrupt_restore_preparation_format",
    "restore_id",
    "prepared_at",
    "phase",
    "data_root_identity",
    "reviewed_assessment_identity",
    "damage_identity",
    "raw_manifest",
    "raw_manifest_sha256",
    "backup_id",
    "backup_sha256",
    "schema_from",
    "schema_to",
    "candidate_name",
    "candidate_manifest_identity",
    "registry_sha256",
    "staged_database",
    "staged_database_sha256",
    "staged_database_byte_size",
    "staged_database_identity",
    "staged_counts",
    "media_assessment",
    "confirmed_lost_changes",
    "confirmed_media_issues",
    "current_changes_assessable",
    "media_included",
    "raw_source_preserved",
    "apply_authorized",
    "activated",
    "restored",
}


def _hex(value: Any, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


def _metadata(body: bytes, root_identity: str, restore_id: str) -> dict[str, Any]:
    record = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(record, dict) or set(record) != _KEYS:
        raise ValueError("Unsupported corrupt-stage manifest shape")
    if (
        type(record["corrupt_restore_preparation_format"]) is not int
        or record["corrupt_restore_preparation_format"] != 1
        or record["restore_id"] != restore_id
        or record["data_root_identity"] != root_identity
        or record["phase"] != "CORRUPT_RESTORE_STAGED"
        or record["staged_database"] != "restored.sqlite3"
        or type(record["schema_from"]) is not int
        or not 1 <= record["schema_from"] <= SCHEMA_VERSION
        or type(record["schema_to"]) is not int
        or record["schema_to"] != SCHEMA_VERSION
        or type(record["staged_database_byte_size"]) is not int
        or record["staged_database_byte_size"] <= 0
    ):
        raise ValueError("Corrupt-stage identity/schema/size binding differs")
    for key in (
        "reviewed_assessment_identity",
        "damage_identity",
        "raw_manifest_sha256",
        "backup_sha256",
        "candidate_manifest_identity",
        "staged_database_sha256",
        "staged_database_identity",
    ):
        if not _hex(record[key]):
            raise ValueError("Corrupt-stage digest metadata required")
    if not _hex(record["backup_id"], 32) or (
        record["registry_sha256"] is not None and not _hex(record["registry_sha256"])
    ):
        raise ValueError("Corrupt-stage backup/registry metadata required")
    if (
        record["confirmed_lost_changes"] is not True
        or record["raw_source_preserved"] is not True
        or type(record["confirmed_media_issues"]) is not bool
        or any(
            record[key] is not False
            for key in (
                "current_changes_assessable",
                "media_included",
                "apply_authorized",
                "activated",
                "restored",
            )
        )
    ):
        raise ValueError(
            "Stage must retain explicit consent and cannot authorize apply"
        )
    if not isinstance(record["prepared_at"], str) or datetime.fromisoformat(
        record["prepared_at"]
    ).utcoffset() != timedelta(0):
        raise ValueError("UTC stage timestamp required")
    name = record["candidate_name"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name in (".", "..")
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    ):
        raise ValueError("Explicit candidate basename required")
    raw = record["raw_manifest"]
    if (
        not isinstance(raw, str)
        or re.fullmatch(
            r"backups[\\/]raw-source-[0-9a-f]{32}[\\/]raw-source-manifest\.json", raw
        )
        is None
    ):
        raise ValueError("Canonical relative raw archive path required")
    counts = record["staged_counts"]
    if (
        not isinstance(counts, dict)
        or not counts
        or any(
            not isinstance(key, str) or type(count) is not int or count < 0
            for key, count in counts.items()
        )
    ):
        raise ValueError("Staged table counts required")
    if not isinstance(record["media_assessment"], list):
        raise ValueError("Staged media assessment required")
    for entry in record["media_assessment"]:
        required = {"file_id", "role", "expected_size", "expected_sha256", "status"}
        if (
            not isinstance(entry, dict)
            or not required <= set(entry)
            or not set(entry)
            <= required | {"actual_size", "actual_sha256", "error_type"}
            or not isinstance(entry["status"], str)
            or not isinstance(entry["file_id"], str)
            or entry["role"]
            not in ("ORIGINAL", "DERIVED_AUDIO", "DERIVED_FRAME", "THUMBNAIL", "OTHER")
            or type(entry["expected_size"]) is not int
            or entry["expected_size"] < 0
            or not _hex(entry["expected_sha256"])
        ):
            raise ValueError("Malformed staged media assessment")
        if entry["status"] == "valid" and (
            type(entry.get("actual_size")) is not int
            or entry["actual_size"] != entry["expected_size"]
            or entry.get("actual_sha256") != entry["expected_sha256"]
        ):
            raise ValueError("Valid staged media requires matching bytes metadata")
        if entry["status"] != "valid" and record["confirmed_media_issues"] is not True:
            raise ValueError("Assessed media issue acknowledgement missing")
    return record


@contextmanager
def hold_corrupt_preparation(
    root: Path,
    manifest: Path,
    reviewed_manifest_sha256: str,
    *,
    timeout_seconds: float = 60,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Retain exact stage/archive proof; no current-source assessment or apply.

    The explicit manifest digest binds this read to the selected preparation.
    It is not a signature or consent to replace live data. Apply must separately
    own the app lock, freshly assess all live inputs/consents and journal before
    mutation. Portable checks cannot establish native Windows sharing.
    """
    if (
        not _hex(reviewed_manifest_sha256)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Explicit manifest digest and bounded work budget required")
    deadline = time.monotonic() + timeout_seconds

    def check() -> None:
        if cancelled and cancelled():
            raise InterruptedError("Corrupt-stage verification cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Corrupt-stage verification exceeded its work budget")

    check()
    canonical = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(canonical)):
        raise ValueError("Canonical data root required")
    selected = manifest.absolute()
    parent, folder = canonical / "backups", selected.parent
    if (
        selected.name != "preparation.json"
        or folder.parent != parent
        or re.fullmatch(r"corrupt-restore-[0-9a-f]{32}", folder.name) is None
        or selected.resolve(strict=True) != selected
    ):
        raise ValueError("Explicit canonical corrupt-stage manifest required")
    for path in (canonical, parent, folder):
        if (
            path.is_symlink()
            or path.is_junction()
            or not path.is_dir()
            or path.resolve(strict=True) != path
        ):
            raise ValueError("Real corrupt-stage directories required")
    with ExitStack() as handles:
        if sys.platform == "win32":
            for directory in (canonical, parent):
                handles.enter_context(
                    RuntimeHandle(directory, directory=True, allow_child_writes=True)
                )
            handles.enter_context(RuntimeHandle(folder, directory=True))
        held_files: dict[str, tuple[BinaryIO, os.stat_result]] = {}

        def acquire(path: Path) -> BinaryIO:
            check()
            before = _regular(path)
            if before is None:
                raise ValueError("Required corrupt-stage file missing")
            stream = handles.enter_context(_open_read_lock(path))
            actual = os.fstat(stream.fileno())
            if _identity(before) != _identity(actual):
                raise RuntimeError("Stage changed before acquiring lease")
            held_files[path.name] = stream, actual
            return stream

        def inventory() -> None:
            check()
            if {path.name for path in folder.iterdir()} != {
                "preparation.json",
                "restored.sqlite3",
            }:
                raise ValueError("Partial or unknown corrupt-stage inventory")
            for name, (stream, before) in held_files.items():
                actual = _regular(folder / name)
                if (
                    actual is None
                    or _identity(actual) != _identity(before)
                    or _identity(os.fstat(stream.fileno())) != _identity(before)
                ):
                    raise RuntimeError("Corrupt stage changed during verification")

        stream = acquire(selected)
        if not 0 < os.fstat(stream.fileno()).st_size <= _MAX_MANIFEST:
            raise ValueError("Stage manifest exceeds its metadata budget")
        body = stream.read(_MAX_MANIFEST + 1)
        check()
        if hashlib.sha256(body).hexdigest() != reviewed_manifest_sha256:
            raise ValueError("Selected preparation changed since review")
        record = _metadata(
            body,
            hashlib.sha256(str(canonical).encode("utf-8")).hexdigest(),
            folder.name[len("corrupt-restore-") :],
        )
        inventory()
        stage = folder / "restored.sqlite3"
        reader = acquire(stage)
        digest, size = hashlib.sha256(), 0
        while chunk := reader.read(1024**2):
            check()
            digest.update(chunk)
            size += len(chunk)
        if (
            size != record["staged_database_byte_size"]
            or digest.hexdigest() != record["staged_database_sha256"]
        ):
            raise ValueError("Staged database bytes/digest differ")
        archive = handles.enter_context(
            hold_preserved_source(
                canonical,
                canonical / record["raw_manifest"],
                record["damage_identity"],
                timeout_seconds=min(
                    timeout_seconds, max(0.001, deadline - time.monotonic())
                ),
                cancelled=cancelled,
            )
        )
        if archive["archive_manifest_sha256"] != record["raw_manifest_sha256"]:
            raise ValueError("Bound raw archive manifest changed")
        # Immutable read avoids journal creation/recovery for untrusted bytes.
        with closing(
            sqlite3.connect(stage.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.set_progress_handler(
                lambda: int(
                    time.monotonic() >= deadline or bool(cancelled and cancelled())
                ),
                1000,
            )
            validate(db, expected_version=SCHEMA_VERSION)
            identity, counts = _database_identity(db, deadline)
            if (
                identity != record["staged_database_identity"]
                or counts != record["staged_counts"]
            ):
                raise ValueError("Staged logical identity/counts differ")
            if db.execute(
                "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
            ).fetchone():
                raise ValueError("Staged processing needs explicit recovery")
            refs = db.execute(
                "SELECT file_id,role,byte_size,sha256 FROM asset_files ORDER BY file_id"
            ).fetchall()
            recorded = [
                (
                    entry.get("file_id"),
                    entry.get("role"),
                    entry.get("expected_size"),
                    entry.get("expected_sha256"),
                )
                for entry in record["media_assessment"]
            ]
            if refs != recorded:
                raise ValueError("Staged media references differ from preparation")
        inventory()
        yield {
            **record,
            "stage_revalidated": True,
            "preparation_manifest_sha256": reviewed_manifest_sha256,
            "current_source_assessed": False,
        }
        inventory()
