"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.draft_review import (
    current_draft_review,
)
from creator_loop.backend.creator.drafts import (
    DraftAssertion,
    DraftClaimLink,
)
from creator_loop.backend.creator.projects import list_projects
from creator_loop.backend.knowledge.claims import claim_support_review
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        projects = list_projects(db, include_archived=True)
    return (projects,)


def load_project_snapshot(root: Any, project_currentData: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        versions = db.execute(
            "SELECT v.draft_version_id,v.draft_id,v.version_no,v.body_text,v.format,\n                    d.status='ACTIVE' AND v.version_no=(SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)\n                    FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id\n                    WHERE d.project_id=? ORDER BY d.created_at DESC,d.draft_id,v.version_no DESC",
            (project_currentData,),
        ).fetchall()
        claims = db.execute(
            "SELECT v.claim_version_id,v.version_no,v.statement,c.deleted_at,\n                    v.version_no=(SELECT MAX(version_no) FROM claim_versions WHERE claim_id=v.claim_id),\n                    COALESCE((SELECT action FROM review_events WHERE claim_version_id=v.claim_version_id ORDER BY rowid DESC LIMIT 1),'PENDING')\n                    FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id\n                    ORDER BY c.created_at DESC,c.claim_id,v.version_no DESC"
        ).fetchall()
        supports = {row[0]: claim_support_review(db, row[0]) for row in claims}
    return (claims, supports, versions)


def load_version_snapshot(root: Any, version_id: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        _links = [
            DraftClaimLink(row[0], row[1])
            for row in db.execute(
                "SELECT claim_version_id,use_type FROM draft_claims WHERE draft_version_id=? ORDER BY claim_version_id",
                (version_id,),
            )
        ]
        _assertions = [
            (DraftAssertion(row[0], row[1], row[3], row[4]), row[2])
            for row in db.execute(
                "SELECT text_start,text_end,asserted_text,claim_version_id,review_state FROM draft_assertions WHERE draft_version_id=? ORDER BY text_start,text_end,assertion_id",
                (version_id,),
            )
        ]
        parents = db.execute(
            "SELECT parent_draft_version_id,relation_type FROM draft_version_parents WHERE child_draft_version_id=? ORDER BY parent_draft_version_id",
            (version_id,),
        ).fetchall()
        history = db.execute(
            "SELECT action,actor_id,COALESCE(reason,''),created_at FROM review_events WHERE draft_version_id=? ORDER BY rowid",
            (version_id,),
        ).fetchall()
        state = current_draft_review(db, version_id)
    return (history, parents, state, _links, _assertions)
