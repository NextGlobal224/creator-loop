"""Append human review events for a current Evidence Version."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from creator_loop.evidence_reopen import reopen_evidence_version

ReviewAction = Literal["ACCEPT", "REJECT", "REQUEST_CHANGES", "REOPEN"]
ReviewState = Literal[
    "PENDING", "ACCEPT", "REJECT", "REQUEST_CHANGES", "REOPEN", "CORRECT"
]


def current_evidence_review(
    db: sqlite3.Connection, evidence_version_id: str
) -> ReviewState:
    """Derive the latest review action from append-only history."""
    row = db.execute(
        """SELECT action FROM review_events WHERE evidence_version_id=?
           ORDER BY rowid DESC LIMIT 1""",
        (evidence_version_id,),
    ).fetchone()
    return "PENDING" if row is None else row[0]


def record_evidence_review(
    db: sqlite3.Connection,
    *,
    evidence_version_id: str,
    data_root: Path,
    action: ReviewAction,
    actor: str,
    reason: str = "",
) -> str:
    """Review only the latest live version; ACCEPT verifies its saved anchor."""
    if action not in ("ACCEPT", "REJECT", "REQUEST_CHANGES", "REOPEN"):
        raise ValueError("Invalid Evidence review action")
    if not actor.strip():
        raise ValueError("Review actor is required")
    if action != "ACCEPT" and not reason.strip():
        raise ValueError("Review reason is required for this action")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT v.evidence_id,v.version_no,e.deleted_at
               FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
               WHERE v.evidence_version_id=?""",
            (evidence_version_id,),
        ).fetchone()
        if row is None or row[2] is not None:
            raise ValueError("Live Evidence Version is required")
        newest = db.execute(
            "SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=?",
            (row[0],),
        ).fetchone()[0]
        if row[1] != newest:
            raise ValueError("Review must target the latest Evidence Version")
        if action == "ACCEPT":
            reopen_evidence_version(db, evidence_version_id, data_root)
        event_id = uuid4().hex
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        db.execute(
            """INSERT INTO review_events(
                 review_event_id,evidence_version_id,claim_version_id,draft_version_id,
                 action,actor_id,reason,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                evidence_version_id,
                None,
                None,
                action,
                actor.strip(),
                reason.strip() or None,
                timestamp,
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return event_id
