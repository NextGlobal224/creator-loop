"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.creator.projects import (
    list_project_references,
    list_projects,
    list_reference_targets,
    reference_target_label,
)
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any, include_archived_isChecked: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        projects = list_projects(db, include_archived=include_archived_isChecked)
    return (projects,)


def project_changed_snapshot(root: Any, project_id: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        references = list_project_references(db, project_id=project_id)
        labels = [reference_target_label(db, reference) for reference in references]
    return (labels, references)


def reload_targets_snapshot(root: Any, target_type_currentData: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        targets = list_reference_targets(db, str(target_type_currentData))
    return (targets,)
