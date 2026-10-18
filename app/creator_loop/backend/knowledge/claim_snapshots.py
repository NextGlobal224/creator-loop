"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.knowledge.claim_review import (
    current_claim_review,
)
from creator_loop.backend.knowledge.claims import (
    EvidenceLink,
    claim_support_review,
)
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        versions = db.execute(
            "SELECT v.claim_version_id,v.claim_id,v.version_no,v.statement,c.claim_type,\n                    c.deleted_at IS NULL AND v.version_no=(SELECT MAX(version_no) FROM claim_versions WHERE claim_id=v.claim_id)\n                    FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id\n                    JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id\n                    ORDER BY c.created_at DESC,c.claim_id,v.version_no DESC"
        ).fetchall()
        evidence = db.execute(
            "SELECT v.evidence_version_id,v.version_no,v.content,e.deleted_at,a.deleted_at,\n                    v.version_no=(SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id),\n                    COALESCE((SELECT action FROM review_events WHERE evidence_version_id=v.evidence_version_id ORDER BY rowid DESC LIMIT 1),'PENDING')\n                    FROM evidence_versions v JOIN evidences e ON e.evidence_id=v.evidence_id JOIN assets a ON a.asset_id=e.asset_id\n                    ORDER BY e.created_at DESC,e.evidence_id,v.version_no DESC"
        ).fetchall()
    return (evidence, versions)


def selected_version_changed_snapshot(root: Any, version_id: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        _links = [
            EvidenceLink(row[0], row[1])
            for row in db.execute(
                "SELECT evidence_version_id,relation_type FROM claim_evidence WHERE claim_version_id=? ORDER BY evidence_version_id,relation_type",
                (version_id,),
            )
        ]
        support = claim_support_review(db, version_id)
        state = current_claim_review(db, version_id)
        events = db.execute(
            "SELECT action,actor_id,COALESCE(reason,''),created_at FROM review_events WHERE claim_version_id=? ORDER BY rowid",
            (version_id,),
        ).fetchall()
    return (events, state, support, _links)
