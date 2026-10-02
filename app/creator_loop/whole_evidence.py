"""Record explicitly confirmed whole-source Evidence on one verified file."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import _anchor_path, reopen_evidence_version
from creator_loop.text_evidence import read_verified_text_snapshot
from creator_loop.transactions import atomic_transaction

if TYPE_CHECKING:
    from PySide6.QtGui import QImage


@dataclass(frozen=True)
class WholeSource:
    asset_id: str
    anchor_file_id: str
    media_type: str
    role: str
    anchor_path: Path
    text_snapshot: str | None
    image: QImage | None
    duration_ms: int | None


@dataclass(frozen=True)
class CorrectedWholeEvidence:
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


def read_verified_whole_source(
    db: sqlite3.Connection, file_id: str, data_root: Path
) -> WholeSource:
    """Read the exact anchor, never combine the other files of its Asset.

    An image is fully decoded with its saved orientation. MP4 verification
    includes an actual decoded first frame and duration, not full playback or
    a decision that the whole video is relevant. A later use rechecks bytes.
    """
    row = db.execute(
        """SELECT f.asset_id,a.media_type,f.role,f.storage_key
           FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
           WHERE f.file_id=?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Whole-source anchor does not exist")
    asset_id, media_type, role, storage_key = row
    path = _anchor_path(Path(data_root), role, storage_key)
    snapshot = None
    image = None
    duration = None
    if media_type == "TEXT":
        verified_asset_id, snapshot = read_verified_text_snapshot(
            db, file_id, data_root
        )
    elif media_type == "IMAGE":
        from creator_loop.image_evidence import read_verified_image

        verified_asset_id, image = read_verified_image(db, file_id, data_root)
    elif media_type == "VIDEO":
        from creator_loop.video_evidence import decode_video_frame

        verified_asset_id, decoded = decode_video_frame(
            db, file_id, data_root, start_ms=0
        )
        image = decoded.image
        duration = decoded.duration_ms
        path = decoded.anchor_path
    else:
        raise ValueError("Unsupported whole-source media type")
    if verified_asset_id != asset_id:
        raise ValueError("Whole-source anchor belongs to another Asset")
    return WholeSource(
        str(asset_id), file_id, media_type, role, path, snapshot, image, duration
    )


def _confirmed_actor(actor: str, whole_source_relevant: bool) -> str:
    if whole_source_relevant is not True:
        raise ValueError(
            "Explicit confirmation that the whole source is relevant is required"
        )
    if not actor.strip():
        raise ValueError("Whole-source Evidence actor is required")
    return actor.strip()


def _live_asset(db: sqlite3.Connection, asset_id: str) -> None:
    row = db.execute(
        "SELECT deleted_at FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None or row[0] is not None:
        raise ValueError("A live Asset is required for whole-source Evidence")


def _typed_content(source: WholeSource, content: str | None) -> tuple[str, str]:
    if source.media_type == "TEXT":
        snapshot = source.text_snapshot
        if snapshot is None or not snapshot.strip():
            raise ValueError("Whole TEXT source contains no text")
        if content is not None and content != snapshot:
            raise ValueError(
                "Whole DIRECT_TEXT content must equal the full NFC snapshot"
            )
        return "DIRECT_TEXT", snapshot
    if content is None or not content.strip():
        raise ValueError("Whole image/video observation content is required")
    return "VISUAL_OBSERVATION", content.strip()


def create_whole_evidence(
    db: sqlite3.Connection,
    *,
    file_id: str,
    data_root: Path,
    actor: str,
    whole_source_relevant: bool = False,
    content: str | None = None,
) -> EvidenceVersion:
    """Create PENDING human Evidence only after explicit whole-source confirmation."""
    actor = _confirmed_actor(actor, whole_source_relevant)
    with atomic_transaction(db):
        source = read_verified_whole_source(db, file_id, data_root)
        _live_asset(db, source.asset_id)
        evidence_type, saved_content = _typed_content(source, content)
        timestamp = _timestamp()
        evidence_id = uuid4().hex
        version = EvidenceVersion(
            uuid4().hex,
            evidence_id,
            source.asset_id,
            1,
            file_id,
            saved_content,
            "WHOLE_ASSET",
            "{}",
            "HUMAN",
            None,
            actor,
            timestamp,
        )
        EvidenceRepository(db).create_with_version(
            Evidence(evidence_id, source.asset_id, evidence_type, timestamp, None),
            version,
        )
    return version


def reopen_whole_evidence(
    db: sqlite3.Connection, version_id: str, data_root: Path
) -> tuple[str, WholeSource]:
    """Reopen a historical exact whole anchor and verify its saved content/type."""
    reopened = reopen_evidence_version(db, version_id, data_root)
    if reopened.locator_type != "WHOLE_ASSET":
        raise ValueError("Selected Evidence is not a whole-source locator")
    source = read_verified_whole_source(db, reopened.anchor_file_id, data_root)
    row = db.execute(
        """SELECT v.content,e.evidence_type FROM evidence_versions v
           JOIN evidences e ON e.evidence_id=v.evidence_id
           WHERE v.evidence_version_id=?""",
        (version_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Whole-source Evidence does not exist")
    saved_content, saved_type = row
    expected_type, verified_content = _typed_content(source, saved_content)
    if saved_type != expected_type:
        raise ValueError("Whole-source Evidence type does not match its media")
    return verified_content, source


def correct_whole_evidence(
    db: sqlite3.Connection,
    *,
    evidence_version_id: str,
    data_root: Path,
    actor: str,
    reason: str,
    whole_source_relevant: bool = False,
    content: str | None = None,
) -> CorrectedWholeEvidence:
    """Append on the same whole anchor; preserve old versions, reviews and citations."""
    actor = _confirmed_actor(actor, whole_source_relevant)
    if not reason.strip():
        raise ValueError("Whole-source correction reason is required")
    with atomic_transaction(db):
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,
                      v.locator_type,e.evidence_type,e.deleted_at
               FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
               WHERE v.evidence_version_id=?""",
            (evidence_version_id,),
        ).fetchone()
        if row is None or row[4] != "WHOLE_ASSET" or row[6] is not None:
            raise ValueError("Current WHOLE_ASSET Evidence version is required")
        evidence_id, asset_id, file_id, old_number, _locator, saved_type, _deleted = row
        _live_asset(db, asset_id)
        latest = db.execute(
            "SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=?",
            (evidence_id,),
        ).fetchone()[0]
        if old_number != latest:
            raise ValueError("Correction must start from the latest Evidence Version")
        _old_content, source = reopen_whole_evidence(db, evidence_version_id, data_root)
        if source.asset_id != asset_id or source.anchor_file_id != file_id:
            raise ValueError("Evidence anchor changed during correction")
        evidence_type, saved_content = _typed_content(source, content)
        if evidence_type != saved_type:
            raise ValueError("Whole-source correction must preserve Evidence type")
        timestamp = _timestamp()
        new_version_id = uuid4().hex
        EvidenceRepository(db).append_version(
            EvidenceVersion(
                new_version_id,
                evidence_id,
                asset_id,
                old_number + 1,
                file_id,
                saved_content,
                "WHOLE_ASSET",
                "{}",
                "HUMAN",
                None,
                actor,
                timestamp,
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
                actor,
                reason.strip(),
                timestamp,
            ),
        )
    return CorrectedWholeEvidence(
        str(evidence_id), evidence_version_id, new_version_id, old_number + 1
    )
