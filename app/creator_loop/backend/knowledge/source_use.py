"""Exact Project source-use choices; never create factual/publication approvals."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.knowledge.evidence_review import current_evidence_review
from creator_loop.backend.runtime.transactions import atomic_transaction


def current_source_use(
    db: sqlite3.Connection, project_id: str, file_id: str
) -> dict[str, Any] | None:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_material_use_versions'"
    ).fetchone():
        return None  # Legacy cloud remains readable until explicit backed-up migration.
    row = db.execute(
        """SELECT u.use_version_id,u.version_no,u.state,u.actor_id,u.reason,u.created_at
           FROM project_material_use_versions u JOIN project_material_use_seals s USING(use_version_id)
           WHERE u.project_id=? AND u.file_id=? ORDER BY u.version_no DESC LIMIT 1""",
        (project_id, file_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(
        zip(
            (
                "use_version_id",
                "version_no",
                "state",
                "actor_id",
                "reason",
                "created_at",
            ),
            row,
        )
    )
    result["evidence_version_ids"] = [
        item[0]
        for item in db.execute(
            "SELECT evidence_version_id FROM project_material_use_items WHERE use_version_id=? ORDER BY position",
            (row[0],),
        )
    ]
    result["factual_approval"] = False
    result["publication_approval"] = False
    return result


def select_source_use(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    project_id: str,
    file_id: str,
    state: str,
    evidence_version_ids: list[str],
    expected_parent: str | None,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Append an explicit client choice; processing alone never invokes this."""
    if (
        state not in ("SELECTED", "EXCLUDED", "REOPENED")
        or not isinstance(evidence_version_ids, list)
        or len(evidence_version_ids) > 100
        or any(not isinstance(v, str) or not v for v in evidence_version_ids)
        or len(set(evidence_version_ids)) != len(evidence_version_ids)
        or bool(evidence_version_ids) != (state == "SELECTED")
        or not isinstance(actor, str)
        or not actor.strip()
        or len(actor) > 200
        or not isinstance(reason, str)
        or len(reason) > 2000
    ):
        raise ValueError("Invalid explicit source-use choice")
    with atomic_transaction(db):
        current = current_source_use(db, project_id, file_id)
        if (current["use_version_id"] if current else None) != expected_parent:
            raise ValueError("Source choice changed; reopen before selecting again")
        for vid in evidence_version_ids:
            row = db.execute(
                """SELECT v.anchor_file_id,v.version_no,
                   (SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
                   FROM evidence_versions v JOIN evidences e USING(evidence_id)
                   WHERE v.evidence_version_id=? AND e.deleted_at IS NULL""",
                (vid,),
            ).fetchone()
            if (
                row is None
                or row[0] != file_id
                or row[1] != row[2]
                or current_evidence_review(db, vid)
                in ("REJECT", "REQUEST_CHANGES", "REOPEN", "CORRECT")
            ):
                raise ValueError("Select current source versions from this Original")
            reopen_evidence_version(db, vid, data_root)
        key, now = uuid4().hex, _timestamp()
        db.execute(
            "INSERT INTO project_material_use_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key,
                project_id,
                file_id,
                (current["version_no"] + 1) if current else 1,
                expected_parent,
                state,
                actor.strip(),
                reason.strip(),
                now,
            ),
        )
        db.executemany(
            "INSERT INTO project_material_use_items VALUES(?,?,?)",
            [(key, index, vid) for index, vid in enumerate(evidence_version_ids)],
        )
        db.execute("INSERT INTO project_material_use_seals VALUES(?,?)", (key, now))
        result = current_source_use(db, project_id, file_id)
        assert result is not None
        return result


def permitted_source_versions(
    db: sqlite3.Connection, *, data_root: Path, project_id: str, file_id: str
) -> tuple[tuple[str, str], ...]:
    """Resolve a live exact source-use snapshot, not a factual ACCEPT shortcut."""
    use = current_source_use(db, project_id, file_id)
    if use is None or use["state"] != "SELECTED":
        raise ValueError(
            "Mở nguồn, chọn đoạn và xác nhận phần được dùng cho Draft trước."
        )
    result = []
    for vid in use["evidence_version_ids"]:
        row = db.execute(
            """SELECT v.content,v.version_no,
               (SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
               FROM evidence_versions v JOIN evidences e USING(evidence_id)
               WHERE v.evidence_version_id=? AND v.anchor_file_id=? AND e.deleted_at IS NULL""",
            (vid, file_id),
        ).fetchone()
        if (
            row is None
            or row[1] != row[2]
            or current_evidence_review(db, vid)
            in ("REJECT", "REQUEST_CHANGES", "REOPEN", "CORRECT")
        ):
            raise ValueError(
                "Đoạn nguồn đã sửa/bỏ/yêu cầu xem lại; mở nguồn và chọn phiên bản hiện tại."
            )
        reopen_evidence_version(db, vid, data_root)
        result.append((vid, row[0]))
    if not result:
        raise ValueError("Chưa chọn đoạn nguồn cho Draft.")
    return tuple(result)
