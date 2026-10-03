"""Human Package decisions and manual Post records through one write boundary.

No network publishing occurs here. The person records the external post; retry
uses the same identity or exact external ID to avoid duplicating its local record.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from creator_loop.claims import _timestamp
from creator_loop.packages import CanonicalItem, package_fingerprint
from creator_loop.publication_media import hold_publication_media
from creator_loop.publication_policy import require_factual_reviews

ApprovalDecision = Literal["APPROVED", "REJECTED", "REVOKED"]


@dataclass(frozen=True)
class PackageSnapshot:
    package_id: str
    project_id: str
    draft_version_id: str
    platform: str
    format: str
    fingerprint: str
    created_at: str
    items: tuple[CanonicalItem, ...]


@dataclass(frozen=True)
class Approval:
    approval_id: str
    package_id: str
    package_fingerprint: str
    decision: ApprovalDecision
    actor_id: str
    decided_at: str
    reason: str | None


@dataclass(frozen=True)
class Post:
    post_id: str
    package_id: str
    platform: str
    external_post_id: str | None
    external_url: str | None
    published_at: str | None
    status: str
    created_at: str


def package_snapshot(db: sqlite3.Connection, package_id: str) -> PackageSnapshot:
    row = db.execute(
        """SELECT package_id,project_id,draft_version_id,platform,format,
        fingerprint,created_at,item_count,sealed_at FROM publication_packages WHERE package_id=?""",
        (package_id,),
    ).fetchone()
    if row is None or row[8] is None:
        raise ValueError("Sealed Package is required")
    items = db.execute(
        """SELECT item_type,position,text_payload,file_id,content_digest
        FROM publication_package_items WHERE package_id=? ORDER BY item_type,position""",
        (package_id,),
    ).fetchall()
    if len(items) != row[7] or not items:
        raise ValueError("Package item count mismatch")
    canonical: list[CanonicalItem] = []
    for kind, pos, text, file_id, digest in items:
        if (text is None) == (file_id is None):
            raise ValueError("Package item payload mismatch")
        if (
            text is not None
            and hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        ):
            raise ValueError("Package text digest mismatch")
        canonical.append((kind, pos, text, file_id, digest))
    if package_fingerprint(row[1], row[2], row[3], row[4], canonical) != row[5]:
        raise ValueError("Package fingerprint mismatch")
    return PackageSnapshot(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], tuple(canonical)
    )


def list_approvals(db: sqlite3.Connection, package_id: str) -> tuple[Approval, ...]:
    return tuple(
        Approval(*row)
        for row in db.execute(
            "SELECT * FROM approvals WHERE package_id=? ORDER BY rowid", (package_id,)
        ).fetchall()
    )


def list_posts(db: sqlite3.Connection, package_id: str) -> tuple[Post, ...]:
    return tuple(
        Post(*row)
        for row in db.execute(
            "SELECT * FROM posts WHERE package_id=? ORDER BY rowid", (package_id,)
        ).fetchall()
    )


def _approved_package(
    db: sqlite3.Connection,
    package_id: str,
    expected_fingerprint: str,
    data_root: Path,
    handles: ExitStack,
) -> PackageSnapshot:
    package = package_snapshot(db, package_id)
    if package.fingerprint != expected_fingerprint:
        raise ValueError("Post fingerprint does not match the exact Package")
    row = db.execute(
        "SELECT decision,package_fingerprint FROM approvals WHERE package_id=? ORDER BY rowid DESC LIMIT 1",
        (package_id,),
    ).fetchone()
    if row is None or row[0] != "APPROVED" or row[1] != package.fingerprint:
        state = row[0] if row else "NONE"
        raise ValueError(
            f"Post requires current APPROVED for this Package/fingerprint; current {state}"
        )
    _publication_ready(db, package, data_root, handles)
    return package


def _prepare_manual_post(
    db: sqlite3.Connection,
    *,
    post_id: str,
    package_id: str,
    expected_fingerprint: str,
    data_root: Path,
) -> Post:
    """Record a gated pending command; no external publishing or durable permit."""
    if not post_id.strip():
        raise ValueError("Post identity is required")
    if db.in_transaction:
        raise RuntimeError("Post repository owns its transaction")
    with ExitStack() as handles:
        try:
            db.execute("BEGIN IMMEDIATE")
            package = _approved_package(
                db, package_id, expected_fingerprint, data_root, handles
            )
            row = db.execute(
                "SELECT * FROM posts WHERE post_id=?", (post_id,)
            ).fetchone()
            if row is not None:
                saved = Post(*row)
                if saved.package_id != package_id or saved.platform != package.platform:
                    raise ValueError("Post identity belongs to another Package")
            else:
                saved = Post(
                    post_id,
                    package_id,
                    package.platform,
                    None,
                    None,
                    None,
                    "PENDING",
                    _timestamp(),
                )
                db.execute(
                    "INSERT INTO posts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        saved.post_id,
                        saved.package_id,
                        saved.platform,
                        None,
                        None,
                        None,
                        saved.status,
                        saved.created_at,
                    ),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return saved


def _publication_ready(
    db: sqlite3.Connection,
    package: PackageSnapshot,
    data_root: Path,
    handles: ExitStack,
) -> None:
    if (
        db.execute(
            """SELECT 1 FROM projects p JOIN drafts d ON d.project_id=p.project_id
        JOIN draft_versions v ON v.draft_id=d.draft_id
        JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
        WHERE p.project_id=? AND v.draft_version_id=? AND p.status='ACTIVE'
        AND p.deleted_at IS NULL AND d.status='ACTIVE'""",
            (package.project_id, package.draft_version_id),
        ).fetchone()
        is None
    ):
        raise ValueError(
            "Publication requires an active live Project and sealed active Draft"
        )
    unresolved = db.execute(
        "SELECT assertion_id,review_state FROM draft_assertions WHERE draft_version_id=? AND review_state IN ('NEEDS_SOURCE','UNREVIEWED') ORDER BY text_start",
        (package.draft_version_id,),
    ).fetchall()
    if unresolved:
        raise ValueError(
            "Draft assertions require review/source: "
            + "; ".join(f"{a}: {state}" for a, state in unresolved)
        )
    require_factual_reviews(
        db, package_id=package.package_id, data_root=data_root, handles=handles
    )
    for _kind, _pos, _text, file_id, digest in package.items:
        if file_id is not None:
            hold_publication_media(
                db,
                file_id=file_id,
                expected_digest=digest,
                data_root=data_root,
                handles=handles,
            )


def _record_approval(
    db: sqlite3.Connection,
    *,
    package_id: str,
    expected_fingerprint: str,
    decision: ApprovalDecision,
    actor: str,
    reason: str,
    data_root: Path,
) -> Approval:
    if decision not in ("APPROVED", "REJECTED", "REVOKED"):
        raise ValueError("Invalid Approval decision")
    if not actor.strip() or (decision != "APPROVED" and not reason.strip()):
        raise ValueError("Approval actor and rejection/revocation reason are required")
    if db.in_transaction:
        raise RuntimeError("Approval repository owns its transaction")
    with ExitStack() as handles:
        try:
            db.execute("BEGIN IMMEDIATE")
            package = package_snapshot(db, package_id)
            if package.fingerprint != expected_fingerprint:
                raise ValueError(
                    "Decision fingerprint does not match the exact Package"
                )
            if decision == "APPROVED":
                _publication_ready(db, package, data_root, handles)
            saved = Approval(
                uuid4().hex,
                package_id,
                package.fingerprint,
                decision,
                actor.strip(),
                _timestamp(),
                reason.strip() or None,
            )
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?)",
                (
                    saved.approval_id,
                    saved.package_id,
                    saved.package_fingerprint,
                    saved.decision,
                    saved.actor_id,
                    saved.decided_at,
                    saved.reason,
                ),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return saved


def _record_manual_post(
    db: sqlite3.Connection,
    *,
    post_id: str,
    package_id: str,
    expected_fingerprint: str,
    external_post_id: str | None,
    external_url: str | None,
    published_at: str,
    data_root: Path,
) -> Post:
    if not post_id.strip():
        raise ValueError("Post identity is required")
    try:
        instant = datetime.fromisoformat(published_at.strip().replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("Missing timestamp timezone")
        published = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValueError(
            "Publication timestamp must be ISO8601 with an explicit timezone"
        ) from exc
    if db.in_transaction:
        raise RuntimeError("Post repository owns its transaction")
    with ExitStack() as handles:
        try:
            db.execute("BEGIN IMMEDIATE")
            package = _approved_package(
                db, package_id, expected_fingerprint, data_root, handles
            )
            external_id = (
                external_post_id.strip() or None
                if external_post_id is not None
                else None
            )
            url = external_url.strip() or None if external_url is not None else None
            existing = db.execute(
                """SELECT * FROM posts WHERE post_id=? OR (platform=? AND
                ((? IS NOT NULL AND external_post_id=?) OR (? IS NOT NULL AND external_url=?))) ORDER BY rowid""",
                (post_id, package.platform, external_id, external_id, url, url),
            ).fetchall()
            if existing:
                if len(existing) != 1:
                    raise ValueError(
                        "Post identities refer to different existing posts"
                    )
                saved = Post(*existing[0])
                if (
                    saved.post_id == post_id
                    and saved.package_id == package_id
                    and saved.platform == package.platform
                    and saved.status == "PENDING"
                ):
                    db.execute(
                        "UPDATE posts SET external_post_id=?,external_url=?,published_at=?,status='PUBLISHED' WHERE post_id=?",
                        (external_id, url, published, post_id),
                    )
                    saved = Post(
                        saved.post_id,
                        saved.package_id,
                        saved.platform,
                        external_id,
                        url,
                        published,
                        "PUBLISHED",
                        saved.created_at,
                    )
                if (
                    saved.package_id,
                    saved.platform,
                    saved.external_post_id,
                    saved.external_url,
                    saved.published_at,
                    saved.status,
                ) != (
                    package_id,
                    package.platform,
                    external_id,
                    url,
                    published,
                    "PUBLISHED",
                ):
                    raise ValueError("Retry payload differs from the existing Post")
            else:
                saved = Post(
                    post_id,
                    package_id,
                    package.platform,
                    external_id,
                    url,
                    published,
                    "PUBLISHED",
                    _timestamp(),
                )
                db.execute(
                    "INSERT INTO posts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        saved.post_id,
                        saved.package_id,
                        saved.platform,
                        saved.external_post_id,
                        saved.external_url,
                        saved.published_at,
                        saved.status,
                        saved.created_at,
                    ),
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return saved
