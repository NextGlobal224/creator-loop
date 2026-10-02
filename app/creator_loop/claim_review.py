"""Append Claim review/correction history against exact sealed versions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from creator_loop.claims import (
    EvidenceLink,
    SavedClaimVersion,
    _timestamp,
    append_claim_version,
)
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.evidence_review import ReviewAction, ReviewState
from creator_loop.transactions import atomic_transaction


def current_claim_review(db: sqlite3.Connection, claim_version_id: str) -> ReviewState:
    row = db.execute(
        "SELECT action FROM review_events WHERE claim_version_id=? ORDER BY rowid DESC LIMIT 1",
        (claim_version_id,),
    ).fetchone()
    return "PENDING" if row is None else row[0]


def _latest_claim(db: sqlite3.Connection, version_id: str) -> str:
    row = db.execute(
        """SELECT v.claim_id,v.version_no,c.deleted_at,s.sealed_at
        FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
        LEFT JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
        WHERE v.claim_version_id=?""",
        (version_id,),
    ).fetchone()
    if row is None or row[2] is not None or row[3] is None:
        raise ValueError("Live sealed Claim Version is required")
    newest = db.execute(
        "SELECT MAX(version_no) FROM claim_versions WHERE claim_id=?", (row[0],)
    ).fetchone()[0]
    if row[1] != newest:
        raise ValueError("Review must target the latest Claim Version")
    return str(row[0])


def _actor_reason(actor: str, reason: str, required: bool) -> None:
    if not actor.strip():
        raise ValueError("Review actor is required")
    if required and not reason.strip():
        raise ValueError("Review reason is required for this action")


def _event(
    db: sqlite3.Connection, version_id: str, action: str, actor: str, reason: str
) -> str:
    event_id = uuid4().hex
    db.execute(
        """INSERT INTO review_events(
        review_event_id,evidence_version_id,claim_version_id,draft_version_id,
        action,actor_id,reason,created_at) VALUES(?,NULL,?,NULL,?,?,?,?)""",
        (
            event_id,
            version_id,
            action,
            actor.strip(),
            reason.strip() or None,
            _timestamp(),
        ),
    )
    return event_id


def record_claim_review(
    db: sqlite3.Connection,
    *,
    claim_version_id: str,
    data_root: Path,
    action: ReviewAction,
    actor: str,
    reason: str = "",
) -> str:
    """ACCEPT verifies exact citation anchors; publication remains a separate gate."""
    if action not in ("ACCEPT", "REJECT", "REQUEST_CHANGES", "REOPEN"):
        raise ValueError("Invalid Claim review action")
    _actor_reason(actor, reason, action != "ACCEPT")
    with atomic_transaction(db):
        _latest_claim(db, claim_version_id)
        if action == "ACCEPT":
            links = db.execute(
                "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id=?",
                (claim_version_id,),
            ).fetchall()
            if not links:
                raise ValueError("Claim acceptance needs Evidence citations")
            for (version_id,) in links:
                row = db.execute(
                    """SELECT e.deleted_at FROM evidence_versions v
                    JOIN evidences e ON e.evidence_id=v.evidence_id WHERE v.evidence_version_id=?""",
                    (version_id,),
                ).fetchone()
                if row is None or row[0] is not None:
                    raise ValueError("Claim citation needs live Evidence")
                reopen_evidence_version(db, version_id, data_root)
        return _event(db, claim_version_id, action, actor, reason)


def correct_claim(
    db: sqlite3.Connection,
    *,
    claim_version_id: str,
    statement: str,
    actor: str,
    reason: str,
    links: Sequence[EvidenceLink],
) -> SavedClaimVersion:
    """Create replacement statement/citations/seal and CORRECT event atomically."""
    _actor_reason(actor, reason, True)
    with atomic_transaction(db):
        claim_id = _latest_claim(db, claim_version_id)
        saved = append_claim_version(
            db, claim_id=claim_id, statement=statement, actor=actor, links=links
        )
        _event(db, claim_version_id, "CORRECT", actor, reason)
    return saved
