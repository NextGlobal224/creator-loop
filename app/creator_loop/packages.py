"""Atomic creation of a publication snapshot with a canonical fingerprint."""

import hashlib
import json
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .database import _connect_write
from .publication_media import hold_publication_media
from .publication_policy import PublicationBlocker, factual_review_blockers

CanonicalItem = tuple[str, int, str | None, str | None, str]

if TYPE_CHECKING:
    from .publication import Approval, ApprovalDecision, Post


@dataclass(frozen=True)
class PackageItem:
    item_type: str
    position: int
    text_payload: str | None = None
    file_id: str | None = None
    content_digest: str | None = None


def _create_package(
    db: sqlite3.Connection,
    *,
    package_id: str,
    project_id: str,
    draft_version_id: str,
    platform: str,
    format: str,
    created_at: str,
    items: list[PackageItem],
    data_root: Path | None = None,
) -> str:
    if not items:
        raise ValueError("Package must contain at least one item")
    if db.in_transaction:
        raise RuntimeError("create_package owns its transaction")
    if not all(value.strip() for value in (package_id, platform, format, created_at)):
        raise ValueError("Package ID, platform, format and timestamp are required")
    handles = ExitStack()
    try:
        db.execute("BEGIN IMMEDIATE")
        project = db.execute(
            """SELECT d.project_id FROM draft_versions v
            JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
            JOIN drafts d ON d.draft_id=v.draft_id AND d.status='ACTIVE'
            JOIN projects p ON p.project_id=d.project_id
            WHERE v.draft_version_id=? AND p.status='ACTIVE' AND p.deleted_at IS NULL""",
            (draft_version_id,),
        ).fetchone()
        if not project or project[0] != project_id:
            raise ValueError(
                "Sealed active Draft version must belong to an active live Project"
            )
        canonical_items = []
        positions: set[tuple[str, int]] = set()
        for item in items:
            if (item.text_payload is None) == (item.file_id is None):
                raise ValueError("Exactly one item payload is required")
            if type(item.position) is not int or item.position < 0:
                raise ValueError("Item position must be a nonnegative integer")
            position = (item.item_type, item.position)
            if position in positions:
                raise ValueError("Duplicate item type/position")
            positions.add(position)
            if item.file_id is not None:
                if data_root is None:
                    raise ValueError("Media Package requires the recorded data root")
                digest = hold_publication_media(
                    db,
                    file_id=item.file_id,
                    expected_digest=item.content_digest,
                    data_root=data_root,
                    handles=handles,
                )
            else:
                assert item.text_payload is not None
                digest = hashlib.sha256(item.text_payload.encode("utf-8")).hexdigest()
                if item.content_digest is not None and item.content_digest != digest:
                    raise ValueError("Text item digest mismatch")
            canonical_items.append(
                (item.item_type, item.position, item.text_payload, item.file_id, digest)
            )
        fingerprint = package_fingerprint(
            project_id, draft_version_id, platform, format, canonical_items
        )
        db.execute(
            "INSERT INTO publication_packages VALUES (?,?,?,?,?,?,?,?,?)",
            (
                package_id,
                project_id,
                draft_version_id,
                platform,
                format,
                fingerprint,
                len(canonical_items),
                None,
                created_at,
            ),
        )
        for idx, (kind, pos, text, file_id, digest) in enumerate(canonical_items):
            db.execute(
                "INSERT INTO publication_package_items VALUES (?,?,?,?,?,?,?)",
                (f"{package_id}:{idx}", package_id, kind, pos, text, file_id, digest),
            )
        db.execute(
            "UPDATE publication_packages SET sealed_at=? WHERE package_id=?",
            (created_at, package_id),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        handles.close()
    return fingerprint


def package_fingerprint(
    project_id: str,
    draft_version_id: str,
    platform: str,
    format: str,
    items: list[CanonicalItem],
) -> str:
    manifest = {
        "project_id": project_id,
        "draft_version_id": draft_version_id,
        "platform": platform,
        "format": format,
        "items": sorted(items, key=lambda item: (item[0], item[1])),
    }
    return hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class PublicationRepository:
    """The application write path for publication snapshots."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def record_approval(
        self,
        *,
        package_id: str,
        expected_fingerprint: str,
        decision: "ApprovalDecision",
        actor: str,
        data_root: Path,
        reason: str = "",
    ) -> "Approval":
        from .publication import _record_approval

        db = _connect_write(self.db_path)
        try:
            return _record_approval(
                db,
                package_id=package_id,
                expected_fingerprint=expected_fingerprint,
                decision=decision,
                actor=actor,
                reason=reason,
                data_root=data_root,
            )
        finally:
            db.close()

    def record_manual_post(
        self,
        *,
        post_id: str,
        package_id: str,
        expected_fingerprint: str,
        external_post_id: str | None,
        external_url: str | None,
        published_at: str,
        data_root: Path,
    ) -> "Post":
        from .publication import _record_manual_post

        db = _connect_write(self.db_path)
        try:
            return _record_manual_post(
                db,
                post_id=post_id,
                package_id=package_id,
                expected_fingerprint=expected_fingerprint,
                external_post_id=external_post_id,
                external_url=external_url,
                published_at=published_at,
                data_root=data_root,
            )
        finally:
            db.close()

    def prepare_manual_post(
        self,
        *,
        post_id: str,
        package_id: str,
        expected_fingerprint: str,
        data_root: Path,
    ) -> "Post":
        from .publication import _prepare_manual_post

        db = _connect_write(self.db_path)
        try:
            return _prepare_manual_post(
                db,
                post_id=post_id,
                package_id=package_id,
                expected_fingerprint=expected_fingerprint,
                data_root=data_root,
            )
        finally:
            db.close()

    def factual_review_blockers(
        self, *, package_id: str, data_root: Path
    ) -> tuple[PublicationBlocker, ...]:
        """Inspect exact factual citations; this is not an Approval or publish permit."""
        db = _connect_write(self.db_path)
        try:
            db.execute("BEGIN")
            return factual_review_blockers(
                db, package_id=package_id, data_root=data_root
            )
        finally:
            db.rollback()
            db.close()

    def create_package(
        self,
        *,
        package_id: str,
        project_id: str,
        draft_version_id: str,
        platform: str,
        format: str,
        created_at: str,
        items: list[PackageItem],
        data_root: Path | None = None,
    ) -> str:
        db = _connect_write(self.db_path)
        try:
            return _create_package(
                db,
                package_id=package_id,
                project_id=project_id,
                draft_version_id=draft_version_id,
                platform=platform,
                format=format,
                created_at=created_at,
                items=items,
                data_root=data_root,
            )
        finally:
            db.close()
