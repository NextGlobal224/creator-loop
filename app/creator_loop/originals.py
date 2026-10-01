"""Check an existing original against its SQLite metadata without changing either."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path, PureWindowsPath
from typing import Literal

READ_CHUNK_SIZE = 1024 * 1024

FailureReason = Literal[
    "missing_row",
    "invalid_role",
    "unsafe_path",
    "malformed_digest",
    "missing_file",
    "unavailable_file",
    "size_mismatch",
    "digest_mismatch",
]


class OriginalFileVerificationError(ValueError):
    def __init__(self, reason: FailureReason) -> None:
        self.reason = reason
        super().__init__(reason.replace("_", " "))


def verify_original_file(db: sqlite3.Connection, file_id: str, data_root: Path) -> None:
    """Verify recorded bytes at this moment; do not certify future immutability."""
    row = db.execute(
        """
        SELECT role, storage_key, byte_size, sha256
        FROM asset_files
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise OriginalFileVerificationError("missing_row")

    role, storage_key, recorded_size, recorded_digest = row
    if role != "ORIGINAL":
        raise OriginalFileVerificationError("invalid_role")

    if not isinstance(storage_key, str):
        raise OriginalFileVerificationError("unsafe_path")
    parts = storage_key.split("/")
    if (
        len(parts) < 3
        or parts[:2] != ["storage", "originals"]
        or any(part in ("", ".", "..") for part in parts)
        or any(
            char in storage_key
            for char in ("\\", "\x00", ":", "<", ">", '"', "|", "?", "*")
        )
        or PureWindowsPath(storage_key).drive
        or PureWindowsPath(storage_key).root
    ):
        raise OriginalFileVerificationError("unsafe_path")

    if (
        not isinstance(recorded_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_digest) is None
    ):
        raise OriginalFileVerificationError("malformed_digest")

    canonical_root = data_root.resolve()
    originals_root = (data_root / "storage" / "originals").resolve()
    candidate = (data_root / Path(*parts)).resolve()
    if not originals_root.is_relative_to(
        canonical_root
    ) or not candidate.is_relative_to(originals_root):
        raise OriginalFileVerificationError("unsafe_path")

    size = 0
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(READ_CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise OriginalFileVerificationError("missing_file") from exc
    except OSError as exc:
        raise OriginalFileVerificationError("unavailable_file") from exc

    if size != recorded_size:
        raise OriginalFileVerificationError("size_mismatch")
    if digest.hexdigest() != recorded_digest:
        raise OriginalFileVerificationError("digest_mismatch")
