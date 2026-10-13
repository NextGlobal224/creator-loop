"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.runtime.database import open_readonly


def init_snapshot(asset_id: Any, root: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        linked = db.execute(
            "SELECT s.platform, COALESCE(s.canonical_url,s.external_id,''),\n                          s.rights_status,sa.relationship_type\n                   FROM source_assets sa JOIN sources s ON s.source_id=sa.source_id\n                   WHERE sa.asset_id=? ORDER BY sa.recorded_at,s.source_id",
            (asset_id,),
        ).fetchall()
        all_sources = db.execute(
            "SELECT source_id,platform,COALESCE(canonical_url,external_id,'')\n                   FROM sources ORDER BY created_at,source_id"
        ).fetchall()
    return (all_sources, linked)
