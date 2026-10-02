"""Append a human correction on the saved audio or video time-range anchor."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.audio_evidence import decode_audio_segment
from creator_loop.evidence import EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.locator import validate_locator
from creator_loop.transactions import atomic_transaction
from creator_loop.video_evidence import decode_video_frame


@dataclass(frozen=True)
class CorrectedTimeEvidence:
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


def correct_time_evidence(
    db: sqlite3.Connection,
    *,
    evidence_version_id: str,
    data_root: Path,
    start_ms: int,
    end_ms: int,
    content: str,
    actor: str,
    reason: str,
) -> CorrectedTimeEvidence:
    """Correct the latest supported MP4 ORIGINAL range, preserving track/history.

    Audio SPEECH/OTHER and video VISUAL_OBSERVATION use their saved track and
    exact file. The new version and CORRECT event share our transaction, or a
    savepoint if the caller already owns one.
    """
    if not content.strip() or not actor.strip() or not reason.strip():
        raise ValueError("Time observation, correction actor and reason are required")

    with atomic_transaction(db):
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,
                      v.locator_type,e.evidence_type,e.deleted_at,f.duration_ms
               FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
               JOIN asset_files f ON f.file_id=v.anchor_file_id
               WHERE v.evidence_version_id=?""",
            (evidence_version_id,),
        ).fetchone()
        if row is None or row[4] != "TIME_RANGE" or row[6] is not None:
            raise ValueError("Current TIME_RANGE Evidence version is required")
        (
            evidence_id,
            asset_id,
            file_id,
            old_number,
            _locator,
            kind,
            _deleted,
            duration,
        ) = row
        newest = db.execute(
            "SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()[0]
        if old_number != newest:
            raise ValueError("Correction must start from the latest Evidence Version")

        reopened = reopen_evidence_version(db, evidence_version_id, data_root)
        if reopened.anchor_file_id != file_id:
            raise ValueError("Evidence anchor changed during correction")
        track = reopened.locator["track"]
        if (track == "video" and kind != "VISUAL_OBSERVATION") or (
            track == "audio" and kind not in ("SPEECH", "OTHER")
        ):
            raise ValueError("Unsupported TIME_RANGE Evidence type for its saved track")
        locator_data = json.dumps(
            {"start_ms": start_ms, "end_ms": end_ms, "track": track}, sort_keys=True
        )
        validate_locator("TIME_RANGE", locator_data, duration_ms=duration)
        if track == "video":
            verified_asset_id, frame = decode_video_frame(
                db, file_id, data_root, start_ms=start_ms
            )
            decoded_duration = frame.duration_ms
            if frame.frame_time_ms >= end_ms:
                raise ValueError(
                    "No decoded video frame lies inside the corrected range"
                )
        else:
            verified_asset_id, segment = decode_audio_segment(
                db, file_id, data_root, start_ms=start_ms, end_ms=end_ms
            )
            decoded_duration = segment.duration_ms
        if verified_asset_id != asset_id:
            raise ValueError("Evidence anchor belongs to another Asset")
        validate_locator("TIME_RANGE", locator_data, duration_ms=decoded_duration)

        new_version_id = uuid4().hex
        timestamp = _timestamp()
        EvidenceRepository(db).append_version(
            EvidenceVersion(
                evidence_version_id=new_version_id,
                evidence_id=evidence_id,
                asset_id=asset_id,
                version_no=old_number + 1,
                anchor_file_id=file_id,
                content=content.strip(),
                locator_type="TIME_RANGE",
                locator_data=locator_data,
                producer_type="HUMAN",
                processing_run_id=None,
                created_by=actor.strip(),
                created_at=timestamp,
            )
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
    return CorrectedTimeEvidence(
        str(evidence_id), evidence_version_id, new_version_id, old_number + 1
    )
