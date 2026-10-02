"""Attach explicit provenance to logical Assets without guessing from filenames."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from creator_loop.library import LibraryRepository, Source

RIGHTS = frozenset({"UNKNOWN", "OWNED", "LICENSED", "REFERENCE_ONLY", "RESTRICTED"})
RELATIONSHIPS = frozenset({"ORIGIN", "REPOST", "REFERENCE", "UNKNOWN"})


@dataclass(frozen=True)
class SourceDetails:
    platform: str
    canonical_url: str | None = None
    external_id: str | None = None
    publisher_name: str | None = None
    published_at: str | None = None
    captured_at: str | None = None
    rights_status: str = "UNKNOWN"


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validate_link(relationship_type: str) -> None:
    if relationship_type not in RELATIONSHIPS:
        raise ValueError("Unknown Source relationship")


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def create_source_for_asset(
    db: sqlite3.Connection,
    *,
    asset_id: str,
    details: SourceDetails,
    relationship_type: str,
) -> str:
    """Insert Source and association atomically; leave unknown fields NULL."""
    platform = details.platform.strip()
    if not platform:
        raise ValueError("Source platform is required")
    if details.rights_status not in RIGHTS:
        raise ValueError("Unknown rights status")
    _validate_link(relationship_type)
    source_id = uuid4().hex
    timestamp = _timestamp()
    repo = LibraryRepository(db)
    try:
        db.execute("BEGIN IMMEDIATE")
        repo.create_source(
            Source(
                source_id=source_id,
                platform=platform,
                canonical_url=_optional(details.canonical_url),
                external_id=_optional(details.external_id),
                publisher_name=_optional(details.publisher_name),
                published_at=_optional(details.published_at),
                captured_at=_optional(details.captured_at),
                rights_status=details.rights_status,
                created_at=timestamp,
            )
        )
        repo.link_source_asset(
            source_id=source_id,
            asset_id=asset_id,
            relationship_type=relationship_type,
            verification_status="UNREVIEWED",
            recorded_at=timestamp,
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return source_id


def link_existing_source(
    db: sqlite3.Connection,
    *,
    source_id: str,
    asset_id: str,
    relationship_type: str,
) -> None:
    """Associate one existing Source to another Asset without copying Source data."""
    _validate_link(relationship_type)
    try:
        db.execute("BEGIN IMMEDIATE")
        LibraryRepository(db).link_source_asset(
            source_id=source_id,
            asset_id=asset_id,
            relationship_type=relationship_type,
            verification_status="UNREVIEWED",
            recorded_at=_timestamp(),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
