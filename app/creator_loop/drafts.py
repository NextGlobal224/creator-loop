"""Write immutable Draft snapshots with exact Claim citations and parent DAGs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, Sequence
from uuid import uuid4

from creator_loop.claims import _timestamp
from creator_loop.transactions import atomic_transaction

UseType = Literal["ASSERTED", "INSPIRATION", "QUOTE", "BACKGROUND"]
AssertionState = Literal["UNREVIEWED", "SUPPORTED", "NEEDS_SOURCE", "EDITORIAL"]


@dataclass(frozen=True)
class DraftClaimLink:
    claim_version_id: str
    use_type: UseType


@dataclass(frozen=True)
class DraftParent:
    draft_version_id: str
    relation_type: str


@dataclass(frozen=True)
class DraftAssertion:
    start: int
    end: int
    claim_version_id: str | None = None
    review_state: AssertionState = "NEEDS_SOURCE"


@dataclass(frozen=True)
class SavedDraftVersion:
    draft_id: str
    draft_version_id: str
    version_no: int


def _active_project(db: sqlite3.Connection, project_id: str) -> None:
    if (
        db.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE' AND deleted_at IS NULL",
            (project_id,),
        ).fetchone()
        is None
    ):
        raise ValueError("Active live Project is required")


def _validate_input(
    body_text: str,
    format: str,
    actor: str,
    claims: Sequence[DraftClaimLink],
    assertions: Sequence[DraftAssertion],
) -> None:
    if not body_text.strip() or not format.strip() or not actor.strip():
        raise ValueError("Draft body, format and actor are required")
    claim_ids = {link.claim_version_id for link in claims}
    if len(claim_ids) != len(claims):
        raise ValueError("Duplicate Draft Claim citation")
    for link in claims:
        if link.use_type not in ("ASSERTED", "INSPIRATION", "QUOTE", "BACKGROUND"):
            raise ValueError("Invalid Draft Claim use type")
    for assertion in assertions:
        if (
            type(assertion.start) is not int
            or type(assertion.end) is not int
            or not (0 <= assertion.start < assertion.end <= len(body_text))
        ):
            raise ValueError("Invalid Draft assertion offsets")
        if assertion.review_state not in (
            "UNREVIEWED",
            "SUPPORTED",
            "NEEDS_SOURCE",
            "EDITORIAL",
        ):
            raise ValueError("Invalid Draft assertion review state")
        if (
            assertion.claim_version_id is not None
            and assertion.claim_version_id not in claim_ids
        ):
            raise ValueError("Assertion must cite a Claim linked to this Draft Version")
        if assertion.review_state == "SUPPORTED" and assertion.claim_version_id is None:
            raise ValueError("SUPPORTED assertion needs an exact Claim Version")


def _insert_version(
    db: sqlite3.Connection,
    *,
    draft_id: str,
    project_id: str,
    version_no: int,
    body_text: str,
    format: str,
    actor: str,
    claims: Sequence[DraftClaimLink],
    assertions: Sequence[DraftAssertion],
    parents: Sequence[DraftParent],
) -> SavedDraftVersion:
    version_id = uuid4().hex
    if len({parent.draft_version_id for parent in parents}) != len(parents):
        raise ValueError("Duplicate Draft parent")
    for parent in parents:
        row = db.execute(
            """SELECT d.project_id FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id
            JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id WHERE v.draft_version_id=?""",
            (parent.draft_version_id,),
        ).fetchone()
        if row is None or row[0] != project_id or not parent.relation_type.strip():
            raise ValueError("Draft parent must be sealed and in the same Project")
    for link in claims:
        if (
            db.execute(
                """SELECT 1 FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
            JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id WHERE v.claim_version_id=? AND c.deleted_at IS NULL""",
                (link.claim_version_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Draft citation needs a live sealed Claim Version")
    db.execute(
        "INSERT INTO draft_versions(draft_version_id,draft_id,version_no,body_text,format,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            version_id,
            draft_id,
            version_no,
            body_text,
            format.strip(),
            actor.strip(),
            _timestamp(),
        ),
    )
    for parent in parents:
        # New child + sealed immutable parents cannot introduce a cycle. The
        # seal trigger additionally checks the graph, including migrated data.
        db.execute(
            "INSERT INTO draft_version_parents VALUES(?,?,?)",
            (version_id, parent.draft_version_id, parent.relation_type.strip()),
        )
    for link in claims:
        db.execute(
            "INSERT INTO draft_claims VALUES(?,?,?)",
            (version_id, link.claim_version_id, link.use_type),
        )
    for assertion in assertions:
        db.execute(
            "INSERT INTO draft_assertions VALUES(?,?,?,?,?,?,?)",
            (
                uuid4().hex,
                version_id,
                assertion.start,
                assertion.end,
                body_text[assertion.start : assertion.end],
                assertion.claim_version_id,
                assertion.review_state,
            ),
        )
    db.execute(
        "INSERT INTO draft_version_seals VALUES(?,?)", (version_id, _timestamp())
    )
    return SavedDraftVersion(draft_id, version_id, version_no)


def create_draft(
    db: sqlite3.Connection,
    *,
    project_id: str,
    body_text: str,
    format: str,
    actor: str,
    claims: Sequence[DraftClaimLink] = (),
    assertions: Sequence[DraftAssertion] = (),
    parents: Sequence[DraftParent] = (),
) -> SavedDraftVersion:
    """Create one alternative and its complete sealed snapshot atomically."""
    _validate_input(body_text, format, actor, claims, assertions)
    with atomic_transaction(db):
        _active_project(db, project_id)
        draft_id = uuid4().hex
        db.execute(
            "INSERT INTO drafts VALUES(?,?,?,?)",
            (draft_id, project_id, "ACTIVE", _timestamp()),
        )
        return _insert_version(
            db,
            draft_id=draft_id,
            project_id=project_id,
            version_no=1,
            body_text=body_text,
            format=format,
            actor=actor,
            claims=claims,
            assertions=assertions,
            parents=parents,
        )


def append_draft_version(
    db: sqlite3.Connection,
    *,
    draft_version_id: str,
    body_text: str,
    format: str,
    actor: str,
    claims: Sequence[DraftClaimLink] = (),
    assertions: Sequence[DraftAssertion] = (),
) -> SavedDraftVersion:
    """Append to the expected current snapshot; prior associations stay sealed."""
    _validate_input(body_text, format, actor, claims, assertions)
    with atomic_transaction(db):
        row = db.execute(
            """SELECT v.draft_id,d.project_id,v.version_no,v.body_text,v.format,d.status,
            (SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)
            FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
            WHERE v.draft_version_id=?""",
            (draft_version_id,),
        ).fetchone()
        if row is None or row[5] != "ACTIVE" or row[2] != row[6]:
            raise ValueError("Latest active sealed Draft Version is required")
        _active_project(db, row[1])
        old_claims = set(
            db.execute(
                "SELECT claim_version_id,use_type FROM draft_claims WHERE draft_version_id=?",
                (draft_version_id,),
            )
        )
        old_assertions = sorted(
            db.execute(
                "SELECT text_start,text_end,COALESCE(claim_version_id,''),review_state FROM draft_assertions WHERE draft_version_id=?",
                (draft_version_id,),
            )
        )
        new_assertions = sorted(
            (a.start, a.end, a.claim_version_id or "", a.review_state)
            for a in assertions
        )
        if (
            row[3] == body_text
            and row[4] == format.strip()
            and old_claims == {(c.claim_version_id, c.use_type) for c in claims}
            and old_assertions == new_assertions
        ):
            raise ValueError("No Draft snapshot changes")
        return _insert_version(
            db,
            draft_id=row[0],
            project_id=row[1],
            version_no=row[2] + 1,
            body_text=body_text,
            format=format,
            actor=actor,
            claims=claims,
            assertions=assertions,
            parents=[DraftParent(draft_version_id, "EDIT")],
        )


def combine_drafts(
    db: sqlite3.Connection,
    *,
    project_id: str,
    parent_version_ids: Sequence[str],
    body_text: str,
    format: str,
    actor: str,
    claims: Sequence[DraftClaimLink] = (),
    assertions: Sequence[DraftAssertion] = (),
) -> SavedDraftVersion:
    """Save an authored A+B alternative; do not fabricate merged text/citations."""
    if len(parent_version_ids) < 2 or len(set(parent_version_ids)) != len(
        parent_version_ids
    ):
        raise ValueError("Combine needs at least two distinct Draft Versions")
    return create_draft(
        db,
        project_id=project_id,
        body_text=body_text,
        format=format,
        actor=actor,
        claims=claims,
        assertions=assertions,
        parents=[
            DraftParent(version_id, "COMBINE") for version_id in parent_version_ids
        ],
    )
