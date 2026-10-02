"""Append a corrected TEXT Evidence Version and review history atomically."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.locator import validate_locator
from creator_loop.text_evidence import read_verified_text_snapshot


@dataclass(frozen=True)
class CorrectedTextEvidence:
    evidence_id: str
    old_version_id: str
    new_version_id: str
    version_no: int


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def correct_text_evidence(
    db: sqlite3.Connection,
    *,
    evidence_version_id: str,
    data_root: Path,
    start: int,
    end: int,
    actor: str,
    reason: str,
) -> CorrectedTextEvidence:
    """Correct the current DIRECT_TEXT version without changing its history."""
    if not actor.strip() or not reason.strip():
        raise ValueError("Correction actor and reason are required")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,
                      e.evidence_type,e.deleted_at
               FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
               WHERE v.evidence_version_id=?""",
            (evidence_version_id,),
        ).fetchone()
        if row is None or row[4] != "DIRECT_TEXT" or row[5] is not None:
            raise ValueError("Current DIRECT_TEXT Evidence version is required")
        evidence_id, asset_id, file_id, old_number, _kind, _deleted = row
        newest = db.execute(
            "SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()[0]
        if old_number != newest:
            raise ValueError("Correction must start from the latest Evidence Version")
        verified_asset_id, snapshot = read_verified_text_snapshot(
            db, file_id, data_root
        )
        if verified_asset_id != asset_id:
            raise ValueError("Evidence anchor belongs to another Asset")
        digest = "sha256:" + hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        locator = json.dumps(
            {"start": start, "end": end, "text_digest": digest}, sort_keys=True
        )
        validate_locator("TEXT_RANGE", locator, text_snapshot=snapshot)
        new_version_id = uuid4().hex
        timestamp = _timestamp()
        db.execute(
            """INSERT INTO evidence_versions(
                 evidence_version_id,evidence_id,asset_id,version_no,anchor_file_id,
                 content,locator_type,locator_data,producer_type,processing_run_id,
                 created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_version_id,
                evidence_id,
                asset_id,
                old_number + 1,
                file_id,
                snapshot[start:end],
                "TEXT_RANGE",
                locator,
                "HUMAN",
                None,
                actor.strip(),
                timestamp,
            ),
        )
        db.execute(
            """INSERT INTO review_events(
                 review_event_id,evidence_version_id,claim_version_id,draft_version_id,
                 action,actor_id,reason,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                evidence_version_id,
                None,
                None,
                "CORRECT",
                actor.strip(),
                reason.strip(),
                timestamp,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return CorrectedTextEvidence(
        str(evidence_id), evidence_version_id, new_version_id, old_number + 1
    )


def claim_versions_needing_review(
    db: sqlite3.Connection, evidence_id: str
) -> list[str]:
    """Project Claims using older Evidence Versions; preserve their original FK."""
    return [
        row[0]
        for row in db.execute(
            """SELECT DISTINCT ce.claim_version_id
               FROM claim_evidence ce
               JOIN evidence_versions used
                 ON used.evidence_version_id=ce.evidence_version_id
               WHERE used.evidence_id=?
                 AND used.version_no < (
                     SELECT MAX(current.version_no)
                     FROM evidence_versions current
                     WHERE current.evidence_id=used.evidence_id)
               ORDER BY ce.claim_version_id""",
            (evidence_id,),
        ).fetchall()
    ]
