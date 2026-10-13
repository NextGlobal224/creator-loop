"""Read/service transactions; UI receives detached snapshots."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.publication.observations import (
    list_observations,
)
from creator_loop.backend.runtime.database import open_readonly


def reload_snapshot(root: Any, post_id: Any) -> Any:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        db.execute("BEGIN")
        post = db.execute(
            "SELECT platform,status,published_at FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()
        if post is None:
            raise ValueError("Post no longer available")
        history = list_observations(db, post_id)
    return (history, post)
