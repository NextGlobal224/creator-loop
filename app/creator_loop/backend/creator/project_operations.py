"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.projects import (
    add_project_reference,
    archive_project,
    create_project,
)
from creator_loop.backend.runtime.database import _connect_write


def run_ProjectWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            if self.action == "create":
                saved_project_id = create_project(db, title=self.title).project_id
            elif self.action == "archive":
                saved_project_id = archive_project(
                    db, project_id=self.project_id
                ).project_id
            elif self.action == "reference":
                if self.target_type not in (
                    "asset_id",
                    "source_id",
                    "claim_version_id",
                ):
                    raise ValueError("Invalid Project reference target type")
                add_project_reference(
                    db,
                    project_id=self.project_id,
                    usage_intent=self.usage_intent,
                    **{self.target_type: self.target_id},
                )
                saved_project_id = self.project_id
            else:
                raise ValueError("Unknown Project action")
        self.completed.emit(saved_project_id)
    except Exception as exc:
        self.failed.emit(f"Không thể ghi Project: {exc}")
