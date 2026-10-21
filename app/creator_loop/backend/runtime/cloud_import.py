"""Append preserved B rows to a staging cloud DB; never commit or publish.

The caller owns archive provenance, media verification, revision CAS and durable
receipts. This core cannot authorize an import from a hash alone. It preserves
all target rows, SQL triggers and foreign keys, including historical seals.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from creator_loop.backend.runtime.database import validate

B_TABLES = (
    "sources",
    "assets",
    "source_assets",
    "processing_runs",
    "asset_files",
    "evidences",
    "evidence_versions",
    "claims",
    "claim_versions",
    "claim_evidence",
    "projects",
    "project_references",
    "drafts",
    "draft_versions",
    "draft_version_parents",
    "draft_claims",
    "draft_assertions",
    "claim_version_seals",
    "creator_draft_choices",
    "draft_version_seals",
    "creator_workflows",
    "creator_workflow_materials",
    "creator_workflow_claims",
    "creator_workflow_drafts",
    "draft_render_requests",
    "draft_render_inputs",
    "draft_render_outputs",
    "review_events",
)
MAX_ROWS = 10000
MAX_BYTES = 4 * 1024 * 1024


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _existing(db: sqlite3.Connection, table: str, row: dict, keys: list[str]):
    cursor = db.execute(
        "SELECT * FROM "
        + _quote(table)
        + " WHERE "
        + " AND ".join(_quote(k) + "=?" for k in keys),
        tuple(row[k] for k in keys),
    )
    found = cursor.fetchone()
    return (
        None if found is None else dict(zip((c[0] for c in cursor.description), found))
    )


def append_preserved_b_rows(
    db: sqlite3.Connection, tables: dict[str, list[dict[str, Any]]]
) -> dict[str, int]:
    """All-or-nothing append inside the caller's transaction, with no replacement.

    Exact existing rows are no-ops; any differing same-PK row refuses the batch.
    Caller must compare its *transformed* row manifest (including deliberate media
    locator relocation) to the archive and retain that ID/locator mapping. This
    function does not transform source data, run jobs, create approvals or commit.
    """
    if not db.in_transaction:
        raise ValueError("Import requires the caller's explicit transaction")
    validate(db)
    if not isinstance(tables, dict) or set(tables) - set(B_TABLES):
        raise ValueError("Unsupported B import tables")
    try:
        encoded = json.dumps(tables, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Import rows must contain finite JSON values") from error
    if len(encoded) > MAX_BYTES:
        raise ValueError("B import metadata exceeds bounded capacity")
    count = 0
    absent: dict[str, list[dict[str, Any]]] = {}
    identical = 0
    for table, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError("Import table must be a row list")
        info = list(db.execute("PRAGMA table_info(" + _quote(table) + ")"))
        columns = {r[1] for r in info}
        keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        if not keys:
            raise ValueError("Import table lacks a primary key")
        seen: set[tuple] = set()
        absent[table] = []
        for row in rows:
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Too many B import rows")
            if not isinstance(row, dict) or set(row) != columns:
                raise ValueError("Import row must match the exact target columns")
            if any(
                value is not None
                and (
                    type(value) not in (str, int, float)
                    or isinstance(value, float)
                    and not math.isfinite(value)
                )
                for value in row.values()
            ):
                raise ValueError("Unsupported SQLite import value")
            identity = tuple(row[k] for k in keys)
            if None in identity or identity in seen:
                raise ValueError("Missing or duplicate imported identity")
            seen.add(identity)
            if table == "processing_runs" and row["status"] not in (
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            ):
                raise ValueError("Active historical processing cannot be imported")
            current = _existing(db, table, row, keys)
            if current is None:
                absent[table].append(row)
            elif current != row:
                raise ValueError("Existing cloud row differs; never overwrite")
            else:
                identical += 1

    inserted = 0
    previous_defer = db.execute("PRAGMA defer_foreign_keys").fetchone()[0]
    db.execute("SAVEPOINT preserved_b_import")
    db.execute("PRAGMA defer_foreign_keys=ON")

    def insert(table: str, row: dict) -> None:
        nonlocal inserted
        names = list(row)
        db.execute(
            "INSERT INTO "
            + _quote(table)
            + "("
            + ",".join(map(_quote, names))
            + ") VALUES("
            + ",".join("?" for _ in names)
            + ")",
            tuple(row[k] for k in names),
        )
        inserted += 1

    def phase(names: list[str]) -> None:
        pending = [(t, row) for t in names for row in absent.get(t, [])]
        for _ in range(64):
            if not pending:
                return
            remaining = []
            for table, row in pending:
                db.execute("SAVEPOINT preserved_b_row")
                try:
                    insert(table, row)
                except sqlite3.IntegrityError:
                    db.execute("ROLLBACK TO preserved_b_row")
                    remaining.append((table, row))
                finally:
                    db.execute("RELEASE preserved_b_row")
            if len(remaining) == len(pending):
                raise ValueError("Import dependency or domain guard refused the rows")
            pending = remaining
        if pending:
            raise ValueError("Import dependency depth exceeds bounded capacity")

    try:
        late = {
            "claim_version_seals",
            "draft_version_seals",
            "creator_draft_choices",
            "creator_workflow_materials",
            "creator_workflow_claims",
            "creator_workflow_drafts",
            "draft_render_requests",
            "draft_render_inputs",
            "draft_render_outputs",
            "review_events",
        }
        phase([t for t in B_TABLES if t not in late])
        phase(["claim_version_seals", "creator_draft_choices"])
        phase(["draft_version_seals"])
        phase(
            [
                "creator_workflow_materials",
                "creator_workflow_claims",
                "creator_workflow_drafts",
            ]
        )
        requests = absent.get("draft_render_requests", [])
        for row in requests:
            insert("draft_render_requests", {**row, "sealed_at": None})
        phase(["draft_render_inputs"])
        for row in requests:
            if row["sealed_at"] is not None:
                db.execute(
                    "UPDATE draft_render_requests SET sealed_at=? WHERE render_run_id=?",
                    (row["sealed_at"], row["render_run_id"]),
                )
        phase(["draft_render_outputs", "review_events"])
        validate(db)
        # Compare exact final rows, including reconstructed historical seals.
        for table, rows in absent.items():
            info = list(db.execute("PRAGMA table_info(" + _quote(table) + ")"))
            keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            if any(_existing(db, table, row, keys) != row for row in rows):
                raise ValueError("Imported row differs from its preserved manifest")
        db.execute("RELEASE preserved_b_import")
    except BaseException:
        db.execute("ROLLBACK TO preserved_b_import")
        db.execute("RELEASE preserved_b_import")
        raise
    finally:
        db.execute("PRAGMA defer_foreign_keys=" + str(int(previous_defer)))
    return {"inserted": inserted, "identical": identical}
