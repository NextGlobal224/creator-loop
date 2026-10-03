"""Record Creator decisions over exact Draft Version candidate snapshots."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from creator_loop.claims import _timestamp
from creator_loop.drafts import _active_project
from creator_loop.transactions import atomic_transaction


@dataclass(frozen=True)
class Selection:
    selection_event_id: str
    project_id: str
    selected_draft_version_id: str
    candidate_set_id: str
    reason_text: str | None
    actor_id: str
    created_at: str
    candidate_version_ids: tuple[str, ...]


def select_draft(
    db: sqlite3.Connection,
    *,
    project_id: str,
    candidate_version_ids: Sequence[str],
    selected_draft_version_id: str,
    actor: str,
    reason: str | None = None,
) -> Selection:
    """Append and seal one decision atomically, without rewriting old decisions.

    A fresh candidate-set ID identifies this decision's exact set. Historical
    sealed versions remain selectable; choosing one does not grant publication.
    """
    candidates = tuple(sorted(candidate_version_ids))
    if not candidates or len(set(candidates)) != len(candidates):
        raise ValueError("Selection needs distinct candidate Draft Versions")
    if selected_draft_version_id not in candidates:
        raise ValueError("Selected Draft Version must belong to the candidate set")
    if not actor.strip():
        raise ValueError("Selection actor is required")
    decision = Selection(
        uuid4().hex,
        project_id,
        selected_draft_version_id,
        uuid4().hex,
        reason if reason and reason.strip() else None,
        actor.strip(),
        _timestamp(),
        candidates,
    )
    with atomic_transaction(db):
        _active_project(db, project_id)
        for version in candidates:
            row = db.execute(
                """SELECT d.project_id,d.status FROM draft_versions v
                JOIN drafts d ON d.draft_id=v.draft_id
                JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
                WHERE v.draft_version_id=?""",
                (version,),
            ).fetchone()
            if row is None or row[0] != project_id or row[1] != "ACTIVE":
                raise ValueError(
                    "Candidates must be active sealed Drafts in this Project"
                )
        db.execute(
            """INSERT INTO selection_events(selection_event_id,project_id,
            selected_draft_version_id,candidate_set_id,reason_text,actor_id,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                decision.selection_event_id,
                decision.project_id,
                decision.selected_draft_version_id,
                decision.candidate_set_id,
                decision.reason_text,
                decision.actor_id,
                decision.created_at,
            ),
        )
        db.executemany(
            "INSERT INTO selection_candidates VALUES(?,?)",
            [(decision.selection_event_id, version) for version in candidates],
        )
        db.execute(
            "INSERT INTO selection_event_seals VALUES(?,?)",
            (decision.selection_event_id, decision.created_at),
        )
    return decision


def list_selections(db: sqlite3.Connection, *, project_id: str) -> list[Selection]:
    """Read completed decisions, including history of archived Projects/Drafts."""
    rows = db.execute(
        """SELECT e.selection_event_id,e.project_id,e.selected_draft_version_id,
        e.candidate_set_id,e.reason_text,e.actor_id,e.created_at
        FROM selection_events e JOIN selection_event_seals s
        ON s.selection_event_id=e.selection_event_id
        WHERE e.project_id=? ORDER BY e.rowid""",
        (project_id,),
    ).fetchall()
    return [
        Selection(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            tuple(
                candidate[0]
                for candidate in db.execute(
                    "SELECT draft_version_id FROM selection_candidates WHERE selection_event_id=? ORDER BY draft_version_id",
                    (row[0],),
                )
            ),
        )
        for row in rows
    ]
