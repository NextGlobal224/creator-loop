from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from creator_loop.locator import validate_locator
from creator_loop.transactions import atomic_transaction


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    asset_id: str
    evidence_type: str
    created_at: str
    deleted_at: str | None


@dataclass(frozen=True)
class EvidenceVersion:
    evidence_version_id: str
    evidence_id: str
    asset_id: str
    version_no: int
    anchor_file_id: str
    content: str
    locator_type: str
    locator_data: str
    producer_type: str
    processing_run_id: str | None
    created_by: str | None
    created_at: str


class EvidenceRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create_with_version(
        self,
        evidence: Evidence,
        version: EvidenceVersion,
    ) -> None:
        with atomic_transaction(self.db):
            self._create_with_version(evidence, version)

    def _create_with_version(
        self,
        evidence: Evidence,
        version: EvidenceVersion,
    ) -> None:
        if version.evidence_id != evidence.evidence_id:
            raise ValueError("Evidence version belongs to another Evidence")

        if version.asset_id != evidence.asset_id:
            raise ValueError("Evidence version belongs to another Asset")

        anchor = self.db.execute(
            """
            SELECT asset_id, duration_ms
            FROM asset_files
            WHERE file_id = ?
            """,
            (version.anchor_file_id,),
        ).fetchone()

        if anchor is None:
            raise ValueError("Anchor file does not exist")

        anchor_asset_id, duration_ms = anchor

        if anchor_asset_id != evidence.asset_id:
            raise ValueError("Anchor file belongs to another Asset")

        validate_locator(
            version.locator_type,
            version.locator_data,
            duration_ms=duration_ms,
        )

        self.db.execute(
            """
            INSERT INTO evidences (
                evidence_id,
                asset_id,
                evidence_type,
                created_at,
                deleted_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id,
                evidence.asset_id,
                evidence.evidence_type,
                evidence.created_at,
                evidence.deleted_at,
            ),
        )

        self.db.execute(
            """
            INSERT INTO evidence_versions (
                evidence_version_id,
                evidence_id,
                asset_id,
                version_no,
                anchor_file_id,
                content,
                locator_type,
                locator_data,
                producer_type,
                processing_run_id,
                created_by,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.evidence_version_id,
                version.evidence_id,
                version.asset_id,
                version.version_no,
                version.anchor_file_id,
                version.content,
                version.locator_type,
                version.locator_data,
                version.producer_type,
                version.processing_run_id,
                version.created_by,
                version.created_at,
            ),
        )

    def append_version(self, version: EvidenceVersion) -> None:
        with atomic_transaction(self.db):
            self._append_version(version)

    def _append_version(self, version: EvidenceVersion) -> None:
        row = self.db.execute(
            """
            SELECT asset_id
            FROM evidences
            WHERE evidence_id=?
            """,
            (version.evidence_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Evidence does not exist")

        if row[0] != version.asset_id:
            raise ValueError("Evidence version asset does not match Evidence")

        current = self.db.execute(
            """
            SELECT MAX(version_no)
            FROM evidence_versions
            WHERE evidence_id=?
            """,
            (version.evidence_id,),
        ).fetchone()[0]

        if current is None:
            raise ValueError("Evidence has no existing version")

        if version.version_no != current + 1:
            raise ValueError("Evidence version must be the next version")

        anchor = self.db.execute(
            """
            SELECT asset_id, duration_ms
            FROM asset_files
            WHERE file_id = ?
            """,
            (version.anchor_file_id,),
        ).fetchone()

        if anchor is None:
            raise ValueError("Anchor file does not exist")

        anchor_asset_id, duration_ms = anchor

        if anchor_asset_id != version.asset_id:
            raise ValueError("Anchor file belongs to another Asset")

        validate_locator(
            version.locator_type,
            version.locator_data,
            duration_ms=duration_ms,
        )

        self.db.execute(
            """
            INSERT INTO evidence_versions (
                evidence_version_id,
                evidence_id,
                asset_id,
                version_no,
                anchor_file_id,
                content,
                locator_type,
                locator_data,
                producer_type,
                processing_run_id,
                created_by,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.evidence_version_id,
                version.evidence_id,
                version.asset_id,
                version.version_no,
                version.anchor_file_id,
                version.content,
                version.locator_type,
                version.locator_data,
                version.producer_type,
                version.processing_run_id,
                version.created_by,
                version.created_at,
            ),
        )
