"""Append human Draft review and atomic correction on exact sealed snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from creator_loop.claims import _timestamp
from creator_loop.drafts import (
    DraftAssertion,
    DraftClaimLink,
    SavedDraftVersion,
    _active_project,
    append_draft_version,
)
from creator_loop.evidence_reopen import reopen_evidence_version
from creator_loop.evidence_review import ReviewAction, ReviewState
from creator_loop.transactions import atomic_transaction


def current_draft_review(db: sqlite3.Connection, draft_version_id: str) -> ReviewState:
    row = db.execute(
        "SELECT action FROM review_events WHERE draft_version_id=? ORDER BY rowid DESC LIMIT 1",
        (draft_version_id,),
    ).fetchone()
    return "PENDING" if row is None else row[0]


def _latest_draft(db: sqlite3.Connection, version_id: str) -> None:
    row = db.execute(
        """SELECT d.project_id,d.status,v.version_no,
        (SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)
        FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
        WHERE v.draft_version_id=?""",
        (version_id,),
    ).fetchone()
    if row is None or row[1] != "ACTIVE" or row[2] != row[3]:
        raise ValueError("Latest active sealed Draft Version is required")
    _active_project(db, row[0])


def _identity(actor: str, reason: str, required: bool) -> None:
    if not actor.strip() or (required and not reason.strip()):
        raise ValueError("Draft review needs actor and reason for this action")


def _event(
    db: sqlite3.Connection, version_id: str, action: str, actor: str, reason: str
) -> str:
    event_id = uuid4().hex
    db.execute(
        """INSERT INTO review_events(review_event_id,evidence_version_id,claim_version_id,draft_version_id,action,actor_id,reason,created_at)
        VALUES(?,NULL,NULL,?,?,?,?,?)""",
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


def record_draft_review(
    db: sqlite3.Connection,
    *,
    draft_version_id: str,
    data_root: Path,
    action: ReviewAction,
    actor: str,
    reason: str = "",
) -> str:
    """ACCEPT rechecks every cited Claim's exact anchors; source flags stay explicit.

    This reviews an editorial snapshot. It does not rewrite assertions, clear
    NEEDS_SOURCE/stale warnings or replace Package approval/publication checks.
    """
    if action not in ("ACCEPT", "REJECT", "REQUEST_CHANGES", "REOPEN"):
        raise ValueError("Invalid Draft review action")
    _identity(actor, reason, action != "ACCEPT")
    with atomic_transaction(db):
        _latest_draft(db, draft_version_id)
        if action == "ACCEPT":
            for (claim_id,) in db.execute(
                "SELECT claim_version_id FROM draft_claims WHERE draft_version_id=?",
                (draft_version_id,),
            ).fetchall():
                if (
                    db.execute(
                        """SELECT 1 FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
                    WHERE v.claim_version_id=? AND c.deleted_at IS NULL""",
                        (claim_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Draft citation needs a live sealed Claim Version")
                citations = db.execute(
                    "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id=?",
                    (claim_id,),
                ).fetchall()
                if not citations:
                    raise ValueError("Draft Claim needs Evidence citations")
                for (evidence_id,) in citations:
                    if (
                        db.execute(
                            """SELECT 1 FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id
                        WHERE v.evidence_version_id=? AND e.deleted_at IS NULL""",
                            (evidence_id,),
                        ).fetchone()
                        is None
                    ):
                        raise ValueError("Draft citation needs live Evidence")
                    reopen_evidence_version(db, evidence_id, data_root)
        return _event(db, draft_version_id, action, actor, reason)


def correct_draft(
    db: sqlite3.Connection,
    *,
    draft_version_id: str,
    body_text: str,
    format: str,
    actor: str,
    reason: str,
    claims: Sequence[DraftClaimLink] = (),
    assertions: Sequence[DraftAssertion] = (),
) -> SavedDraftVersion:
    """Append changed snapshot and old-version CORRECT event in one transaction."""
    _identity(actor, reason, True)
    with atomic_transaction(db):
        _latest_draft(db, draft_version_id)
        saved = append_draft_version(
            db,
            draft_version_id=draft_version_id,
            body_text=body_text,
            format=format,
            actor=actor,
            claims=claims,
            assertions=assertions,
        )
        _event(db, draft_version_id, "CORRECT", actor, reason)
    return saved
