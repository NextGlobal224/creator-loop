"""Keep a service's SQLite writes atomic without taking over a caller's transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4


@contextmanager
def atomic_transaction(db: sqlite3.Connection) -> Iterator[None]:
    """Commit our own transaction, or use a savepoint inside the caller's."""
    if db.in_transaction:
        name = f"creator_loop_{uuid4().hex}"
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
            db.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
            raise
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise
