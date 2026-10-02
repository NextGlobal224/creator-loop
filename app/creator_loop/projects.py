"""Manage Project workspaces and explicit references to existing domain objects.

Archived or deleted Projects cannot receive new references. Archiving is
idempotent, and their existing references remain readable as history. A
REUSE_MEDIA reference records intent only; it never approves publication rights.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from creator_loop.transactions import atomic_transaction

ProjectStatus = Literal["ACTIVE", "ARCHIVED"]
UsageIntent = Literal["RESEARCH", "QUOTE", "REUSE_MEDIA"]


@dataclass(frozen=True)
class Project:
    project_id: str
    title: str
    status: ProjectStatus
    created_at: str
    deleted_at: str | None


@dataclass(frozen=True)
class ProjectReference:
    project_reference_id: str
    project_id: str
    asset_id: str | None
    claim_version_id: str | None
    source_id: str | None
    usage_intent: UsageIntent


@dataclass(frozen=True)
class ReferenceTarget:
    target_id: str
    label: str


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _live_project(db: sqlite3.Connection, project_id: str) -> Project:
    row = db.execute(
        """SELECT project_id,title,status,created_at,deleted_at FROM projects
           WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    if row is None or row[4] is not None:
        raise ValueError("Live Project is required")
    return Project(*row)


def create_project(db: sqlite3.Connection, *, title: str) -> Project:
    """Create an active workspace without committing pending caller writes."""
    if not title.strip():
        raise ValueError("Project title is required")
    project = Project(uuid4().hex, title.strip(), "ACTIVE", _timestamp(), None)
    with atomic_transaction(db):
        db.execute(
            """INSERT INTO projects(project_id,title,status,created_at,deleted_at)
               VALUES(?,?,?,?,?)""",
            (
                project.project_id,
                project.title,
                project.status,
                project.created_at,
                project.deleted_at,
            ),
        )
    return project


def list_projects(
    db: sqlite3.Connection, *, include_archived: bool = False
) -> list[Project]:
    """List live Projects; archived workspaces are explicitly opt in."""
    rows = db.execute(
        """SELECT project_id,title,status,created_at,deleted_at FROM projects
           WHERE deleted_at IS NULL AND (? OR status='ACTIVE')
           ORDER BY created_at,rowid""",
        (include_archived,),
    ).fetchall()
    return [Project(*row) for row in rows]


def archive_project(db: sqlite3.Connection, *, project_id: str) -> Project:
    """Archive a live workspace; retain all references and repeat safely."""
    with atomic_transaction(db):
        project = _live_project(db, project_id)
        if project.status == "ARCHIVED":
            return project
        db.execute(
            "UPDATE projects SET status='ARCHIVED' WHERE project_id=?",
            (project_id,),
        )
        return _live_project(db, project_id)


def add_project_reference(
    db: sqlite3.Connection,
    *,
    project_id: str,
    usage_intent: UsageIntent,
    asset_id: str | None = None,
    claim_version_id: str | None = None,
    source_id: str | None = None,
) -> ProjectReference:
    """Add exactly one live target, preserving the selected Claim Version.

    Sources have no soft-delete field. Claim targets must be sealed and belong
    to a live Claim, but may be an older version explicitly chosen by a Creator.
    Rights, support thresholds and publication approval are separate gates.
    """
    if usage_intent not in ("RESEARCH", "QUOTE", "REUSE_MEDIA"):
        raise ValueError("Invalid Project reference usage intent")
    targets = (asset_id, claim_version_id, source_id)
    if sum(target is not None for target in targets) != 1:
        raise ValueError("Project reference requires exactly one target")
    if any(target is not None and not target.strip() for target in targets):
        raise ValueError("Project reference target ID is required")
    with atomic_transaction(db):
        project = _live_project(db, project_id)
        if project.status != "ACTIVE":
            raise ValueError("Archived Project cannot receive new references")
        if asset_id is not None:
            target = db.execute(
                "SELECT 1 FROM assets WHERE asset_id=? AND deleted_at IS NULL",
                (asset_id,),
            ).fetchone()
            if target is None:
                raise ValueError("Live Asset is required")
        elif claim_version_id is not None:
            target = db.execute(
                """SELECT 1 FROM claim_versions v
                   JOIN claims c ON c.claim_id=v.claim_id
                   JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
                   WHERE v.claim_version_id=? AND c.deleted_at IS NULL""",
                (claim_version_id,),
            ).fetchone()
            if target is None:
                raise ValueError("Sealed live Claim Version is required")
        else:
            target = db.execute(
                "SELECT 1 FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if target is None:
                raise ValueError("Existing Source is required")
        reference = ProjectReference(
            uuid4().hex,
            project_id,
            asset_id,
            claim_version_id,
            source_id,
            usage_intent,
        )
        db.execute(
            """INSERT INTO project_references(
                 project_reference_id,project_id,asset_id,claim_version_id,
                 source_id,usage_intent) VALUES(?,?,?,?,?,?)""",
            (
                reference.project_reference_id,
                reference.project_id,
                reference.asset_id,
                reference.claim_version_id,
                reference.source_id,
                reference.usage_intent,
            ),
        )
    return reference


def list_project_references(
    db: sqlite3.Connection, *, project_id: str
) -> list[ProjectReference]:
    """Read exact references, including history in archived/deleted Projects."""
    if (
        db.execute(
            "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        is None
    ):
        raise ValueError("Project does not exist")
    rows = db.execute(
        """SELECT project_reference_id,project_id,asset_id,claim_version_id,
                  source_id,usage_intent FROM project_references
           WHERE project_id=? ORDER BY rowid""",
        (project_id,),
    ).fetchall()
    return [ProjectReference(*row) for row in rows]


def list_reference_targets(
    db: sqlite3.Connection, target_type: str
) -> list[ReferenceTarget]:
    """Offer live selectable targets with their exact IDs and version labels."""
    if target_type == "asset_id":
        rows = db.execute(
            """SELECT asset_id,display_name,media_type FROM assets
               WHERE deleted_at IS NULL ORDER BY created_at,rowid"""
        ).fetchall()
        return [
            ReferenceTarget(identity, f"{name} — {kind} — {identity}")
            for identity, name, kind in rows
        ]
    if target_type == "claim_version_id":
        rows = db.execute(
            """SELECT v.claim_version_id,v.statement,v.version_no,c.claim_type
               FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
               JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
               WHERE c.deleted_at IS NULL ORDER BY v.created_at,v.rowid"""
        ).fetchall()
        return [
            ReferenceTarget(identity, f"v{number} — {statement} — {kind} — {identity}")
            for identity, statement, number, kind in rows
        ]
    if target_type == "source_id":
        rows = db.execute(
            """SELECT source_id,platform,canonical_url,external_id,rights_status
               FROM sources ORDER BY created_at,rowid"""
        ).fetchall()
        return [
            ReferenceTarget(
                identity,
                f"{platform} — {url or external or 'chưa biết URL/ID'} — {rights} — {identity}",
            )
            for identity, platform, url, external, rights in rows
        ]
    raise ValueError("Invalid Project reference target type")


def reference_target_label(db: sqlite3.Connection, reference: ProjectReference) -> str:
    """Describe a historical target without hiding its version or availability."""
    if reference.asset_id is not None:
        row = db.execute(
            "SELECT display_name,media_type,deleted_at FROM assets WHERE asset_id=?",
            (reference.asset_id,),
        ).fetchone()
        if row is None:
            return f"Asset không còn bản ghi — {reference.asset_id}"
        suffix = " — đã xóa mềm" if row[2] is not None else ""
        return f"{row[0]} — {row[1]} — {reference.asset_id}{suffix}"
    if reference.claim_version_id is not None:
        row = db.execute(
            """SELECT v.statement,v.version_no,c.deleted_at
               FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
               WHERE v.claim_version_id=?""",
            (reference.claim_version_id,),
        ).fetchone()
        if row is None:
            return f"Claim Version không còn bản ghi — {reference.claim_version_id}"
        suffix = " — đã xóa mềm" if row[2] is not None else ""
        return f"v{row[1]} — {row[0]} — {reference.claim_version_id}{suffix}"
    row = db.execute(
        """SELECT platform,canonical_url,external_id,rights_status FROM sources
           WHERE source_id=?""",
        (reference.source_id,),
    ).fetchone()
    if row is None:
        return f"Source không còn bản ghi — {reference.source_id}"
    return f"{row[0]} — {row[1] or row[2] or 'chưa biết URL/ID'} — {row[3]} — {reference.source_id}"
