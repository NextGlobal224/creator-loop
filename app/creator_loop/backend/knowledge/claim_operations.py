"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.knowledge.claim_review import (
    correct_claim,
    record_claim_review,
)
from creator_loop.backend.knowledge.claims import (
    create_claim,
)
from creator_loop.backend.runtime.database import _connect_write


def run_ClaimWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            if self.action == "create":
                saved = create_claim(
                    db,
                    claim_type=self.claim_type,
                    statement=self.statement,
                    actor=self.actor,
                    links=self.links,
                )
                version_id = saved.claim_version_id
            elif self.action == "correct":
                saved = correct_claim(
                    db,
                    claim_version_id=self.version_id,
                    statement=self.statement,
                    actor=self.actor,
                    reason=self.reason,
                    links=self.links,
                )
                version_id = saved.claim_version_id
            elif self.action == "review":
                record_claim_review(
                    db,
                    claim_version_id=self.version_id,
                    data_root=self.root,
                    action=self.review_action,
                    actor=self.actor,
                    reason=self.reason,
                )
                version_id = self.version_id
            else:
                raise ValueError("Unknown Claim action")
        self.completed.emit(version_id)
    except Exception as exc:
        self.failed.emit(str(exc))
