"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.projects import list_projects
from creator_loop.backend.creator.selections import (
    list_selections,
)
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        projects = list_projects(db, include_archived=True)
    return (projects,)


def load_project_snapshot(root: Any, project_currentData: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        db.execute("BEGIN")
        versions = db.execute(
            "SELECT v.draft_version_id,v.version_no,v.body_text,d.status,\n                    v.version_no=(SELECT MAX(version_no) FROM draft_versions WHERE draft_id=v.draft_id)\n                    FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id\n                    JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id\n                    WHERE d.project_id=? ORDER BY d.rowid,v.version_no",
            (project_currentData,),
        ).fetchall()
        _decisions = list_selections(db, project_id=str(project_currentData))
    return (versions, _decisions)
