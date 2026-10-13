"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any, TypedDict

from creator_loop.backend.creator.draft_review import (
    correct_draft,
    record_draft_review,
)
from creator_loop.backend.creator.drafts import (
    DraftAssertion,
    DraftClaimLink,
    combine_drafts,
    create_draft,
)
from creator_loop.backend.runtime.database import _connect_write


class _DraftPayload(TypedDict):
    body_text: str
    format: str
    actor: str
    claims: tuple[DraftClaimLink, ...]
    assertions: tuple[DraftAssertion, ...]


def run_CreatorWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            if self.action == "review":
                record_draft_review(
                    db,
                    draft_version_id=self.version_id,
                    data_root=self.root,
                    action=self.review_action,
                    actor=self.actor,
                    reason=self.reason,
                )
                result_id = self.version_id
            else:
                values = _DraftPayload(
                    body_text=self.body_text,
                    format=self.format,
                    actor=self.actor,
                    claims=self.claims,
                    assertions=self.assertions,
                )
                if self.action == "create":
                    saved = create_draft(db, project_id=self.project_id, **values)
                elif self.action == "correct":
                    saved = correct_draft(
                        db,
                        draft_version_id=self.version_id,
                        reason=self.reason,
                        **values,
                    )
                elif self.action == "combine":
                    saved = combine_drafts(
                        db,
                        project_id=self.project_id,
                        parent_version_ids=self.parents,
                        **values,
                    )
                else:
                    raise ValueError("Unknown Creator action")
                result_id = saved.draft_version_id
        self.completed.emit(result_id)
    except Exception as exc:
        self.failed.emit(str(exc))
