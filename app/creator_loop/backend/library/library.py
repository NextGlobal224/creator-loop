from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    source_id: str
    platform: str
    canonical_url: str | None
    external_id: str | None
    publisher_name: str | None
    published_at: str | None
    captured_at: str | None
    rights_status: str
    created_at: str


@dataclass(frozen=True)
class Asset:
    asset_id: str
    media_type: str
    display_name: str
    created_at: str
    deleted_at: str | None


class LibraryRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create_source(self, source: Source) -> None:
        self.db.execute(
            """
            INSERT INTO sources (
                source_id,
                platform,
                canonical_url,
                external_id,
                publisher_name,
                published_at,
                captured_at,
                rights_status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_id,
                source.platform,
                source.canonical_url,
                source.external_id,
                source.publisher_name,
                source.published_at,
                source.captured_at,
                source.rights_status,
                source.created_at,
            ),
        )

    def create_asset(self, asset: Asset) -> None:
        self.db.execute(
            """
            INSERT INTO assets (
                asset_id,
                media_type,
                display_name,
                created_at,
                deleted_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                asset.asset_id,
                asset.media_type,
                asset.display_name,
                asset.created_at,
                asset.deleted_at,
            ),
        )

    def link_source_asset(
        self,
        *,
        source_id: str,
        asset_id: str,
        relationship_type: str,
        verification_status: str,
        recorded_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO source_assets (
                source_id,
                asset_id,
                relationship_type,
                verification_status,
                recorded_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_id,
                asset_id,
                relationship_type,
                verification_status,
                recorded_at,
            ),
        )

    def create_asset_file(
        self,
        *,
        asset_id: str,
        file_id: str,
        role: str,
        storage_key: str,
        sha256: str,
        byte_size: int,
        mime_type: str,
        parent_file_id: str | None,
        processing_run_id: str | None,
        created_at: str,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO asset_files (
                file_id,
                asset_id,
                role,
                storage_key,
                sha256,
                byte_size,
                mime_type,
                parent_file_id,
                processing_run_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                asset_id,
                role,
                storage_key,
                sha256,
                byte_size,
                mime_type,
                parent_file_id,
                processing_run_id,
                created_at,
            ),
        )
        if commit:
            self.db.commit()

    def create_processing_run(
        self,
        *,
        run_id: str,
        asset_id: str,
        input_file_id: str | None,
        task_type: str,
        status: str,
        tool_name: str,
        tool_version: str,
        model_name: str | None,
        model_version: str | None,
        started_at: str | None,
        finished_at: str | None,
        error_code: str | None,
        error_message: str | None,
        created_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO processing_runs (
                run_id,
                asset_id,
                input_file_id,
                task_type,
                status,
                tool_name,
                tool_version,
                model_name,
                model_version,
                started_at,
                finished_at,
                error_code,
                error_message,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                asset_id,
                input_file_id,
                task_type,
                status,
                tool_name,
                tool_version,
                model_name,
                model_version,
                started_at,
                finished_at,
                error_code,
                error_message,
                created_at,
            ),
        )
        self.db.commit()
