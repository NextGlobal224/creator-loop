"""Recover B status only for an exactly bound dead parent; never stop processes."""

import sys
from contextlib import closing
from pathlib import Path

from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.runtime.database import _connect_write
from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_recovery import _identity_dead


def recover_generation(root: Path) -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    recovered = []
    with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
        rows = db.execute(
            "SELECT run_id,tool_name,tool_version FROM processing_runs WHERE task_type IN ('LOCAL_CREATOR_GENERATION','CREATOR_GENERATION') AND status='RUNNING'"
        ).fetchall()
        for run_id, tool, version in rows:
            workspace = root / "runtime" / ("creator-task-" + run_id)
            try:
                with RuntimeHandle(workspace / "ownership.json") as file:
                    marker = file.read_json()
                if (
                    marker.get("kind") != "CREATOR_WORKFLOW_OWNER"
                    or marker.get("data_root") != str(root.resolve())
                    or marker.get("workspace") != workspace.name
                    or marker.get("component_version") != tool + "/" + version
                ):
                    continue
                if not _identity_dead(marker["parent"]):
                    continue
            except (ValueError, OSError, KeyError, TypeError):
                continue
            db.execute(
                "UPDATE processing_runs SET status='CANCELLED',finished_at=?,error_code='PARENT_EXIT',error_message='Previous B owner exited; no unverified Draft accepted' WHERE run_id=? AND status='RUNNING'",
                (_timestamp(), run_id),
            )
            recovered.append(run_id)
        db.commit()
    return tuple(recovered)
