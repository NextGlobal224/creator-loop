"""Write Claim Versions with exact Evidence Version relationships."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence
from uuid import uuid4

ClaimType = Literal["FACTUAL", "INTERPRETIVE", "EDITORIAL_HYPOTHESIS"]
RelationType = Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]


@dataclass(frozen=True)
class EvidenceLink:
    evidence_version_id: str
    relation_type: RelationType


@dataclass(frozen=True)
class SavedClaimVersion:
    claim_id: str
    claim_version_id: str
    version_no: int


@dataclass(frozen=True)
class ClaimSupportReview:
    support_links: int
    accepted_current_supports: int
    stale_links: int


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validate_input(statement: str, actor: str, links: Sequence[EvidenceLink]) -> None:
    if not statement.strip() or not actor.strip():
        raise ValueError("Claim statement and actor are required")
    if not links:
        raise ValueError("Claim needs at least one Evidence Version")
    for link in links:
        if link.relation_type not in ("SUPPORTS", "CONTRADICTS", "CONTEXT"):
            raise ValueError("Invalid Claim Evidence relation")


def _insert_version(
    db: sqlite3.Connection,
    claim_id: str,
    version_no: int,
    statement: str,
    actor: str,
    links: Sequence[EvidenceLink],
) -> SavedClaimVersion:
    version_id = uuid4().hex
    db.execute(
        """INSERT INTO claim_versions(
             claim_version_id,claim_id,version_no,statement,created_by,created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            version_id,
            claim_id,
            version_no,
            statement.strip(),
            actor.strip(),
            _timestamp(),
        ),
    )
    for link in links:
        row = db.execute(
            """SELECT e.deleted_at FROM evidence_versions v
               JOIN evidences e ON e.evidence_id=v.evidence_id
               WHERE v.evidence_version_id=?""",
            (link.evidence_version_id,),
        ).fetchone()
        if row is None or row[0] is not None:
            raise ValueError("Claim link needs a live Evidence Version")
        db.execute(
            """INSERT INTO claim_evidence(
                 claim_version_id,evidence_version_id,relation_type)
               VALUES(?,?,?)""",
            (version_id, link.evidence_version_id, link.relation_type),
        )
    db.execute(
        "INSERT INTO claim_version_seals(claim_version_id,sealed_at) VALUES(?,?)",
        (version_id, _timestamp()),
    )
    return SavedClaimVersion(claim_id, version_id, version_no)


def create_claim(
    db: sqlite3.Connection,
    *,
    claim_type: ClaimType,
    statement: str,
    actor: str,
    links: Sequence[EvidenceLink],
) -> SavedClaimVersion:
    """Create a draft Claim; publication support threshold is a separate gate."""
    if claim_type not in ("FACTUAL", "INTERPRETIVE", "EDITORIAL_HYPOTHESIS"):
        raise ValueError("Invalid Claim type")
    _validate_input(statement, actor, links)
    if db.in_transaction:
        raise ValueError("Claim creation requires a clean transaction")
    claim_id = uuid4().hex
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO claims(claim_id,claim_type,created_at) VALUES(?,?,?)",
            (claim_id, claim_type, _timestamp()),
        )
        saved = _insert_version(db, claim_id, 1, statement, actor, links)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return saved


def append_claim_version(
    db: sqlite3.Connection,
    *,
    claim_id: str,
    statement: str,
    actor: str,
    links: Sequence[EvidenceLink],
) -> SavedClaimVersion:
    """Append a Claim Version without changing prior statements or citations."""
    _validate_input(statement, actor, links)
    if db.in_transaction:
        raise ValueError("Claim version append requires a clean transaction")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT deleted_at FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None or row[0] is not None:
            raise ValueError("Live Claim is required")
        newest = db.execute(
            "SELECT MAX(version_no) FROM claim_versions WHERE claim_id=?", (claim_id,)
        ).fetchone()[0]
        if newest is None:
            raise ValueError("Claim has no existing Version")
        saved = _insert_version(db, claim_id, newest + 1, statement, actor, links)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return saved


def claim_support_review(
    db: sqlite3.Connection, claim_version_id: str
) -> ClaimSupportReview:
    """Project support review and stale citations; do not authorize publication."""
    if (
        db.execute(
            """SELECT 1 FROM claim_versions v
               JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
               WHERE v.claim_version_id=?""",
            (claim_version_id,),
        ).fetchone()
        is None
    ):
        raise ValueError("Sealed Claim Version does not exist")
    rows = db.execute(
        """SELECT ce.relation_type,v.version_no,
                  (SELECT MAX(latest.version_no) FROM evidence_versions latest
                   WHERE latest.evidence_id=v.evidence_id),
                  (SELECT re.action FROM review_events re
                   WHERE re.evidence_version_id=v.evidence_version_id
                   ORDER BY re.rowid DESC LIMIT 1),e.deleted_at
           FROM claim_evidence ce
           JOIN evidence_versions v ON v.evidence_version_id=ce.evidence_version_id
           JOIN evidences e ON e.evidence_id=v.evidence_id
           WHERE ce.claim_version_id=?""",
        (claim_version_id,),
    ).fetchall()
    return ClaimSupportReview(
        support_links=sum(row[0] == "SUPPORTS" for row in rows),
        accepted_current_supports=sum(
            row[0] == "SUPPORTS"
            and row[1] == row[2]
            and row[3] == "ACCEPT"
            and row[4] is None
            for row in rows
        ),
        stale_links=sum(row[1] < row[2] for row in rows),
    )
