"""Resolve one Evidence Version to its recorded, verified physical anchor."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from creator_loop.locator import validate_locator
from creator_loop.originals import READ_CHUNK_SIZE
from creator_loop.storage_paths import StoragePathError, resolve_storage_path
from creator_loop.windows_paths import file_io_path

FailureReason = Literal[
    "missing_version",
    "missing_anchor",
    "anchor_asset_mismatch",
    "unsafe_path",
    "malformed_metadata",
    "missing_file",
    "unavailable_file",
    "size_mismatch",
    "digest_mismatch",
    "text_decode_error",
    "invalid_locator",
]


class EvidenceReopenError(ValueError):
    def __init__(self, reason: FailureReason) -> None:
        self.reason = reason
        super().__init__(reason.replace("_", " "))


@dataclass(frozen=True)
class ReopenedEvidence:
    evidence_version_id: str
    anchor_file_id: str
    anchor_path: Path
    locator_type: str
    locator: dict[str, object]
    text_excerpt: str | None


def _anchor_path(data_root: Path, role: str, storage_key: str) -> Path:
    try:
        return resolve_storage_path(data_root, role, storage_key)
    except StoragePathError as exc:
        raise EvidenceReopenError(exc.reason) from exc


def reopen_evidence_version(
    db: sqlite3.Connection, evidence_version_id: str, data_root: Path
) -> ReopenedEvidence:
    """Verify the selected anchor now; a later use must recheck mutable files."""
    row = db.execute(
        """
        SELECT v.asset_id, v.anchor_file_id, v.locator_type, v.locator_data,
               f.asset_id, f.role, f.storage_key, f.sha256, f.byte_size,
               f.duration_ms
        FROM evidence_versions v
        LEFT JOIN asset_files f ON f.file_id = v.anchor_file_id
        WHERE v.evidence_version_id = ?
        """,
        (evidence_version_id,),
    ).fetchone()
    if row is None:
        raise EvidenceReopenError("missing_version")

    (
        evidence_asset_id,
        anchor_file_id,
        locator_type,
        locator_data,
        anchor_asset_id,
        role,
        storage_key,
        recorded_digest,
        recorded_size,
        duration_ms,
    ) = row
    if anchor_asset_id is None:
        raise EvidenceReopenError("missing_anchor")
    if anchor_asset_id != evidence_asset_id:
        raise EvidenceReopenError("anchor_asset_mismatch")
    if (
        not isinstance(recorded_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_digest) is None
        or type(recorded_size) is not int
        or recorded_size < 0
    ):
        raise EvidenceReopenError("malformed_metadata")

    candidate = _anchor_path(Path(data_root), role, storage_key)
    digest = hashlib.sha256()
    size = 0
    text_chunks: list[bytes] | None = [] if locator_type == "TEXT_RANGE" else None
    try:
        with file_io_path(candidate).open("rb") as source:
            for chunk in iter(lambda: source.read(READ_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
                if text_chunks is not None:
                    text_chunks.append(chunk)
    except FileNotFoundError as exc:
        raise EvidenceReopenError("missing_file") from exc
    except OSError as exc:
        raise EvidenceReopenError("unavailable_file") from exc

    if size != recorded_size:
        raise EvidenceReopenError("size_mismatch")
    if digest.hexdigest() != recorded_digest:
        raise EvidenceReopenError("digest_mismatch")

    snapshot = None
    if text_chunks is not None:
        try:
            snapshot = b"".join(text_chunks).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise EvidenceReopenError("text_decode_error") from exc
    try:
        locator = validate_locator(
            locator_type,
            locator_data,
            duration_ms=duration_ms,
            text_snapshot=snapshot,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceReopenError("invalid_locator") from exc

    excerpt = None
    if snapshot is not None:
        nfc_snapshot = unicodedata.normalize("NFC", snapshot)
        excerpt = nfc_snapshot[locator["start"] : locator["end"]]
    return ReopenedEvidence(
        evidence_version_id,
        anchor_file_id,
        candidate,
        locator_type,
        locator,
        excerpt,
    )
