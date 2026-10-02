"""Create and reopen human image observations from verified original pixels."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import uuid4

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader

from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import (
    EvidenceReopenError,
    _anchor_path,
    reopen_evidence_version,
)
from creator_loop.locator import validate_locator

MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000


def read_verified_image(
    db: sqlite3.Connection, file_id: str, data_root: Path
) -> tuple[str, QImage]:
    row = db.execute(
        """SELECT f.asset_id,a.media_type,f.role,f.storage_key,f.sha256,
                  f.byte_size,f.mime_type
           FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
           WHERE f.file_id=?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Image original does not exist")
    asset_id, media_type, role, key, digest, size, mime = row
    if (
        media_type != "IMAGE"
        or role != "ORIGINAL"
        or mime
        not in (
            "image/png",
            "image/jpeg",
        )
    ):
        raise ValueError("Evidence requires an image original")
    if not isinstance(size, int) or size < 1 or size > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds local decode budget")
    path = _anchor_path(Path(data_root), role, key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EvidenceReopenError("missing_file") from exc
    except OSError as exc:
        raise EvidenceReopenError("unavailable_file") from exc
    if len(raw) != size:
        raise EvidenceReopenError("size_mismatch")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceReopenError("digest_mismatch")
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    dimensions = reader.size()
    if (
        dimensions.isValid()
        and dimensions.width() * dimensions.height() > MAX_IMAGE_PIXELS
    ):
        raise ValueError("Image exceeds local pixel budget")
    image = reader.read()
    if image.isNull():
        raise ValueError(f"Image cannot be decoded: {reader.errorString()}")
    if image.width() * image.height() > MAX_IMAGE_PIXELS:
        raise ValueError("Image exceeds local pixel budget")
    return asset_id, image


def create_image_evidence(
    db: sqlite3.Connection,
    *,
    file_id: str,
    data_root: Path,
    region: dict[str, float],
    content: str,
    actor: str,
) -> EvidenceVersion:
    if not actor.strip() or not content.strip():
        raise ValueError("Image observation and actor are required")
    asset_id, _image = read_verified_image(db, file_id, data_root)
    locator_data = json.dumps(region, sort_keys=True)
    validate_locator("IMAGE_REGION", locator_data)
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    evidence_id = uuid4().hex
    version = EvidenceVersion(
        evidence_version_id=uuid4().hex,
        evidence_id=evidence_id,
        asset_id=asset_id,
        version_no=1,
        anchor_file_id=file_id,
        content=content.strip(),
        locator_type="IMAGE_REGION",
        locator_data=locator_data,
        producer_type="HUMAN",
        processing_run_id=None,
        created_by=actor.strip(),
        created_at=timestamp,
    )
    EvidenceRepository(db).create_with_version(
        Evidence(evidence_id, asset_id, "VISUAL_OBSERVATION", timestamp, None),
        version,
    )
    return version


def reopen_image_region(
    db: sqlite3.Connection, version_id: str, data_root: Path
) -> tuple[str, QImage]:
    reopened = reopen_evidence_version(db, version_id, data_root)
    if reopened.locator_type != "IMAGE_REGION":
        raise ValueError("Selected Evidence is not an image region")
    asset_id, image = read_verified_image(db, reopened.anchor_file_id, data_root)
    row = db.execute(
        "SELECT asset_id,content FROM evidence_versions WHERE evidence_version_id=?",
        (version_id,),
    ).fetchone()
    if row is None or row[0] != asset_id:
        raise ValueError("Image Evidence belongs to another Asset")
    region = cast(dict[str, float], reopened.locator)
    left = math.floor(image.width() * region["x"])
    top = math.floor(image.height() * region["y"])
    right = math.ceil(image.width() * (region["x"] + region["width"]))
    bottom = math.ceil(image.height() * (region["y"] + region["height"]))
    crop = image.copy(left, top, right - left, bottom - top)
    if crop.isNull():
        raise ValueError("Image region resolves to no pixels")
    return str(row[1]), crop
