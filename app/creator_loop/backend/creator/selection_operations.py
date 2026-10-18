"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.selections import (
    select_draft,
)
from creator_loop.backend.runtime.database import _connect_write


def run_SelectionWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            decision = select_draft(
                db,
                project_id=self.project_id,
                candidate_version_ids=self.candidates,
                selected_draft_version_id=self.selected,
                actor=self.actor,
                reason=self.reason,
            )
        self.completed.emit(decision.selection_event_id)
    except Exception as exc:
        self.failed.emit(str(exc))
