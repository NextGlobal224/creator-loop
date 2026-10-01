"""Create human TEXT Evidence from a verified physical original snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import EvidenceReopenError, _anchor_path


def create_text_evidence(
    db: sqlite3.Connection,
    *,
    file_id: str,
    data_root: Path,
    start: int,
    end: int,
    actor: str,
) -> EvidenceVersion:
    """Record one NFC code-point range after checking the stored file digest."""
    if not actor.strip():
        raise ValueError("Evidence actor is required")
    row = db.execute(
        """SELECT f.asset_id, a.media_type, f.role, f.storage_key, f.sha256,
                  f.byte_size, f.mime_type
           FROM asset_files f JOIN assets a ON a.asset_id = f.asset_id
           WHERE f.file_id = ?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Original file does not exist")
    asset_id, media_type, role, key, stored_digest, stored_size, mime_type = row
    if (media_type, role, mime_type) != ("TEXT", "ORIGINAL", "text/plain"):
        raise ValueError("Evidence requires a TEXT original")
    path = _anchor_path(Path(data_root), role, key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EvidenceReopenError("missing_file") from exc
    except OSError as exc:
        raise EvidenceReopenError("unavailable_file") from exc
    if len(raw) != stored_size:
        raise EvidenceReopenError("size_mismatch")
    if hashlib.sha256(raw).hexdigest() != stored_digest:
        raise EvidenceReopenError("digest_mismatch")
    try:
        snapshot = unicodedata.normalize("NFC", raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise EvidenceReopenError("text_decode_error") from exc
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(snapshot)
    ):
        raise ValueError("Text range exceeds NFC snapshot")

    text_digest = "sha256:" + hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    evidence_id = uuid4().hex
    version = EvidenceVersion(
        evidence_version_id=uuid4().hex,
        evidence_id=evidence_id,
        asset_id=asset_id,
        version_no=1,
        anchor_file_id=file_id,
        content=snapshot[start:end],
        locator_type="TEXT_RANGE",
        locator_data=json.dumps(
            {"start": start, "end": end, "text_digest": text_digest},
            sort_keys=True,
        ),
        producer_type="HUMAN",
        processing_run_id=None,
        created_by=actor,
        created_at=timestamp,
    )
    EvidenceRepository(db).create_with_version(
        Evidence(evidence_id, asset_id, "DIRECT_TEXT", timestamp, None), version
    )
    return version
