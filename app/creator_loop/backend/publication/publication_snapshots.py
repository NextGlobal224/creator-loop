"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.projects import list_projects
from creator_loop.backend.publication.publication import (
    list_approvals,
    list_posts,
    package_snapshot,
)
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        projects = list_projects(db, include_archived=True)
    return (projects,)


def load_project_snapshot(root: Any, project_currentData: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        db.execute("BEGIN")
        drafts = db.execute(
            "SELECT v.draft_version_id,v.version_no,v.body_text,d.status\n                FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id\n                JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id\n                WHERE d.project_id=? ORDER BY d.rowid,v.version_no",
            (project_currentData,),
        ).fetchall()
        files = db.execute(
            "SELECT f.file_id,a.display_name,f.role,f.sha256 FROM asset_files f\n                JOIN assets a ON a.asset_id=f.asset_id WHERE a.deleted_at IS NULL ORDER BY a.rowid,f.rowid"
        ).fetchall()
        packages = db.execute(
            "SELECT package_id,created_at,platform FROM publication_packages WHERE project_id=? AND sealed_at IS NOT NULL ORDER BY rowid",
            (project_currentData,),
        ).fetchall()
    return (drafts, files, packages)


def load_snapshot_snapshot(
    root: Any, _snapshot: Any, project_currentData: Any, identity: Any
) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        db.execute("BEGIN")
        _snapshot = package_snapshot(db, identity)
        if _snapshot.project_id != project_currentData:
            raise ValueError("Package does not belong to the selected Project")
        _approvals = list_approvals(db, identity)
        _posts = list_posts(db, identity)
    return (_snapshot, _approvals, _posts)
