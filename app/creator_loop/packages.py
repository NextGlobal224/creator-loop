"""Atomic creation of a publication snapshot with a canonical fingerprint."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .database import _connect_write
from .publication_policy import PublicationBlocker, factual_review_blockers


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
) -> str:
    if not items:
        raise ValueError("Package must contain at least one item")
    if db.in_transaction:
        raise RuntimeError("create_package owns its transaction")
    canonical_items = []
    for item in items:
        if (item.text_payload is None) == (item.file_id is None):
            raise ValueError("Exactly one item payload is required")
        if item.file_id:
            row = db.execute(
                "SELECT sha256 FROM asset_files WHERE file_id=?", (item.file_id,)
            ).fetchone()
            if not row or row[0] != item.content_digest:
                raise ValueError("Media file digest mismatch")
            digest = row[0]
        else:
            assert item.text_payload is not None
            digest = hashlib.sha256(item.text_payload.encode("utf-8")).hexdigest()
        canonical_items.append(
            (item.item_type, item.position, item.text_payload, item.file_id, digest)
        )
    canonical_items.sort(key=lambda x: (x[0], x[1]))
    manifest = {
        "project_id": project_id,
        "draft_version_id": draft_version_id,
        "platform": platform,
        "format": format,
        "items": canonical_items,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    try:
        db.execute("BEGIN IMMEDIATE")
        project = db.execute(
            "SELECT d.project_id FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id WHERE v.draft_version_id=?",
            (draft_version_id,),
        ).fetchone()
        if not project or project[0] != project_id:
            raise ValueError("Draft version does not belong to project")
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
    return fingerprint


class PublicationRepository:
    """The application write path for publication snapshots."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

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
            )
        finally:
            db.close()
