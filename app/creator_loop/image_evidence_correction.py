"""Append a corrected image region without changing its source or history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import uuid4

from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.image_evidence import crop_image_region, read_verified_image
from creator_loop.locator import validate_locator


@dataclass(frozen=True)
class CorrectedImageEvidence:
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


def correct_image_evidence(
    db: sqlite3.Connection,
    *,
    evidence_version_id: str,
    data_root: Path,
    region: dict[str, float],
    content: str,
    actor: str,
    reason: str,
) -> CorrectedImageEvidence:
    """Correct the current IMAGE_REGION version on its exact verified anchor."""
    if not content.strip() or not actor.strip() or not reason.strip():
        raise ValueError("Image observation, correction actor and reason are required")
    if db.in_transaction:
        raise ValueError("Image correction requires a clean transaction")

    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,
                      v.locator_type,e.evidence_type,e.deleted_at
               FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
               WHERE v.evidence_version_id=?""",
            (evidence_version_id,),
        ).fetchone()
        if (
            row is None
            or row[4] != "IMAGE_REGION"
            or row[5] != "VISUAL_OBSERVATION"
            or row[6] is not None
        ):
            raise ValueError("Current IMAGE_REGION Evidence version is required")
        evidence_id, asset_id, file_id, old_number, _locator, _kind, _deleted = row
        newest = db.execute(
            "SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()[0]
        if old_number != newest:
            raise ValueError("Correction must start from the latest Evidence Version")

        reopened = reopen_evidence_version(db, evidence_version_id, data_root)
        if reopened.anchor_file_id != file_id:
            raise ValueError("Evidence anchor changed during correction")
        verified_asset_id, image = read_verified_image(db, file_id, data_root)
        if verified_asset_id != asset_id:
            raise ValueError("Evidence anchor belongs to another Asset")
        locator_data = json.dumps(region, sort_keys=True)
        validated = cast(
            dict[str, float], validate_locator("IMAGE_REGION", locator_data)
        )
        crop_image_region(image, validated)

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
                content.strip(),
                "IMAGE_REGION",
                locator_data,
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
    return CorrectedImageEvidence(
        str(evidence_id), evidence_version_id, new_version_id, old_number + 1
    )
