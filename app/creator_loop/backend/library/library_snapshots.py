"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        rows = db.execute(
            "SELECT a.media_type, a.display_name, f.mime_type,\n                          f.byte_size, f.file_id, a.asset_id\n                   FROM assets a JOIN asset_files f ON f.asset_id = a.asset_id\n                   WHERE f.role = 'ORIGINAL' AND a.deleted_at IS NULL\n                   ORDER BY a.created_at DESC, f.created_at DESC, f.file_id DESC"
        ).fetchall()
        evidence_rows = db.execute(
            "SELECT v.evidence_version_id, a.display_name, v.content,\n                          v.version_no, v.locator_type,\n                          json_extract(v.locator_data,'$.track'),f.role,\n                          COALESCE((SELECT action FROM review_events re\n                                    WHERE re.evidence_version_id=v.evidence_version_id\n                                    ORDER BY re.rowid DESC LIMIT 1),'PENDING')\n                   FROM evidence_versions v\n                   JOIN evidences e ON e.evidence_id = v.evidence_id\n                   JOIN assets a ON a.asset_id = e.asset_id\n                   JOIN asset_files f ON f.file_id=v.anchor_file_id\n                   WHERE e.deleted_at IS NULL\n                   ORDER BY v.created_at DESC, v.evidence_version_id DESC"
        ).fetchall()
        run_rows = db.execute(
            "SELECT a.display_name,r.task_type,r.status,\n                          COALESCE((SELECT f.storage_key FROM asset_files f\n                                    WHERE f.processing_run_id=r.run_id\n                                    ORDER BY f.created_at,f.file_id LIMIT 1),''),\n                          (SELECT f.file_id FROM asset_files f\n                           WHERE f.processing_run_id=r.run_id\n                           ORDER BY f.created_at,f.file_id LIMIT 1),\n                          (SELECT CASE WHEN COUNT(*)=1 THEN MIN(f.file_id) ELSE NULL END\n                           FROM asset_files f\n                           WHERE f.processing_run_id=r.run_id AND f.asset_id=r.asset_id\n                             AND f.parent_file_id=r.input_file_id\n                             AND f.role='OTHER' AND f.mime_type='application/json')\n                   FROM processing_runs r JOIN assets a ON a.asset_id=r.asset_id\n                   ORDER BY r.created_at DESC,r.run_id DESC"
        ).fetchall()
    return (evidence_rows, rows, run_rows)


def open_selected_draft_snapshot(root: Any, version_id: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        row = db.execute(
            "SELECT d.project_id FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id\n                    JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id WHERE v.draft_version_id=?",
            (version_id,),
        ).fetchone()
    return (row,)
