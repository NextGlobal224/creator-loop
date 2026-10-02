"""Verify and locate a human video observation on decoded original frames."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import _anchor_path, reopen_evidence_version
from creator_loop.locator import validate_locator
from creator_loop.originals import verify_original_file


@dataclass(frozen=True)
class DecodedVideoFrame:
    duration_ms: int
    frame_time_ms: int
    image: QImage
    anchor_path: Path


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def decode_video_frame(
    db: sqlite3.Connection,
    file_id: str,
    data_root: Path,
    *,
    start_ms: int,
    timeout_seconds: float = 8.0,
) -> tuple[str, DecodedVideoFrame]:
    """Seek to a time in a verified MP4 and require a decoded video frame."""
    if type(start_ms) is not int or start_ms < 0:
        raise ValueError("Video position must be a non-negative millisecond")
    if QCoreApplication.instance() is None:
        raise RuntimeError("Qt event loop is required to decode video")
    row = db.execute(
        """SELECT f.asset_id,a.media_type,f.role,f.storage_key,f.mime_type
           FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
           WHERE f.file_id=?""",
        (file_id,),
    ).fetchone()
    if row is None or row[1:3] != ("VIDEO", "ORIGINAL") or row[4] != "video/mp4":
        raise ValueError("Video Evidence requires an MP4 original")
    asset_id, _media_type, role, key, _mime = row
    verify_original_file(db, file_id, data_root)
    path = _anchor_path(Path(data_root), role, key)

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoOutput(sink)
    frames: list[tuple[int, QImage]] = []

    def capture(frame: QVideoFrame) -> None:
        if frame.isValid() and frame.startTime() >= 0:
            image = frame.toImage()
            if not image.isNull():
                frames.append((frame.startTime() // 1000, image))

    sink.videoFrameChanged.connect(capture)
    deadline = time.monotonic() + timeout_seconds
    try:
        player.setSource(QUrl.fromLocalFile(str(path)))
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if player.error() != QMediaPlayer.Error.NoError:
                raise ValueError(f"Video decoder failed: {player.errorString()}")
            if player.duration() > 0 and player.hasVideo():
                break
            time.sleep(0.01)
        duration = player.duration()
        if duration <= 0 or not player.hasVideo():
            raise ValueError("Video decoder found no playable video track")
        if start_ms >= duration:
            raise ValueError("Video position exceeds decoded duration")
        player.setPosition(start_ms)
        player.play()
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if player.error() != QMediaPlayer.Error.NoError:
                raise ValueError(f"Video decoder failed: {player.errorString()}")
            for frame_time, image in frames:
                if frame_time >= start_ms:
                    verify_original_file(db, file_id, data_root)
                    return str(asset_id), DecodedVideoFrame(
                        duration, frame_time, image, path
                    )
            time.sleep(0.01)
        raise TimeoutError("Video decoder did not produce a frame at the locator")
    finally:
        player.stop()
        player.setSource(QUrl())


def create_video_evidence(
    db: sqlite3.Connection,
    *,
    file_id: str,
    data_root: Path,
    start_ms: int,
    end_ms: int,
    content: str,
    actor: str,
) -> EvidenceVersion:
    """Create video-track Evidence only after decoding a frame at its start."""
    if not content.strip() or not actor.strip():
        raise ValueError("Video observation and actor are required")
    asset_id, decoded = decode_video_frame(db, file_id, data_root, start_ms=start_ms)
    locator_data = json.dumps(
        {"start_ms": start_ms, "end_ms": end_ms, "track": "video"},
        sort_keys=True,
    )
    validate_locator("TIME_RANGE", locator_data, duration_ms=decoded.duration_ms)
    if decoded.frame_time_ms >= end_ms:
        raise ValueError("No decoded video frame lies inside the selected range")
    db.execute(
        """UPDATE asset_files SET duration_ms=?,width_px=?,height_px=?
           WHERE file_id=?""",
        (decoded.duration_ms, decoded.image.width(), decoded.image.height(), file_id),
    )
    timestamp = _timestamp()
    evidence_id = uuid4().hex
    version = EvidenceVersion(
        evidence_version_id=uuid4().hex,
        evidence_id=evidence_id,
        asset_id=asset_id,
        version_no=1,
        anchor_file_id=file_id,
        content=content.strip(),
        locator_type="TIME_RANGE",
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


def reopen_video_evidence(
    db: sqlite3.Connection, version_id: str, data_root: Path
) -> tuple[str, DecodedVideoFrame, int, int]:
    reopened = reopen_evidence_version(db, version_id, data_root)
    if reopened.locator_type != "TIME_RANGE" or reopened.locator["track"] != "video":
        raise ValueError("Selected Evidence is not a video time range")
    start_ms = reopened.locator["start_ms"]
    end_ms = reopened.locator["end_ms"]
    if type(start_ms) is not int or type(end_ms) is not int:
        raise ValueError("Video locator is malformed")
    _asset_id, decoded = decode_video_frame(
        db, reopened.anchor_file_id, data_root, start_ms=start_ms
    )
    if end_ms > decoded.duration_ms:
        raise ValueError("Video Evidence range exceeds decoded duration")
    if decoded.frame_time_ms >= end_ms:
        raise ValueError("No decoded video frame lies inside the saved range")
    row = db.execute(
        "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
        (version_id,),
    ).fetchone()
    return str(row[0]), decoded, start_ms, end_ms
