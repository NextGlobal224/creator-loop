"""Verify a decoded MP4 audio track before anchoring a human observation."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QIODevice, QUrl
from PySide6.QtMultimedia import QAudioBuffer, QAudioBufferOutput, QMediaPlayer

from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.evidence_reopen import _anchor_path, reopen_evidence_version
from creator_loop.locator import validate_locator
from creator_loop.originals import verify_original_file
from creator_loop.transactions import atomic_transaction


@dataclass(frozen=True)
class DecodedAudioSegment:
    duration_ms: int
    buffer_start_ms: int
    buffer_end_ms: int
    anchor_path: Path
    data_root: Path | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def decode_audio_segment(
    db: sqlite3.Connection,
    file_id: str,
    data_root: Path,
    *,
    start_ms: int,
    end_ms: int,
    timeout_seconds: float = 8.0,
) -> tuple[str, DecodedAudioSegment]:
    """Require decoded audio samples overlapping the selected original range."""
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or not 0 <= start_ms < end_ms
    ):
        raise ValueError("Audio range must be increasing milliseconds")
    if QCoreApplication.instance() is None:
        raise RuntimeError("Qt event loop is required to decode audio")
    row = db.execute(
        """SELECT f.asset_id,a.media_type,f.role,f.storage_key,f.mime_type,f.byte_size,f.sha256
           FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id
           WHERE f.file_id=?""",
        (file_id,),
    ).fetchone()
    if row is None or row[1:3] != ("VIDEO", "ORIGINAL") or row[4] != "video/mp4":
        raise ValueError("Audio Evidence requires an MP4 VIDEO original")
    asset_id, _media_type, role, key, _mime, size, digest = row
    verify_original_file(db, file_id, data_root)
    path = _anchor_path(Path(data_root), role, key)
    from creator_loop.isolated_decode import decode_isolated

    body, _image = decode_isolated(
        path,
        data_root,
        mode="audio",
        start_ms=start_ms,
        end_ms=end_ms,
        timeout_seconds=timeout_seconds,
    )
    verify_original_file(db, file_id, data_root)
    return str(asset_id), DecodedAudioSegment(
        body["duration_ms"],
        body["buffer_start_ms"],
        body["buffer_end_ms"],
        path,
        Path(data_root),
        size,
        digest,
    )


def _decode_audio_local(
    path: Path,
    device: QIODevice,
    *,
    start_ms: int,
    end_ms: int,
    timeout_seconds: float,
) -> DecodedAudioSegment:
    """Child-only Qt decoder; its caller's native Job bounds cleanup as well."""
    player = QMediaPlayer()
    output = QAudioBufferOutput()
    player.setAudioBufferOutput(output)
    buffers: list[tuple[int, int]] = []

    def capture(buffer: QAudioBuffer) -> None:
        if buffer.isValid() and buffer.sampleCount() > 0 and buffer.startTime() >= 0:
            begin = buffer.startTime() // 1000
            finish = (buffer.startTime() + buffer.duration() + 999) // 1000
            if not buffers and begin < end_ms and finish > start_ms:
                buffers.append((begin, finish))

    output.audioBufferReceived.connect(capture)
    deadline = time.monotonic() + timeout_seconds
    try:
        player.setSourceDevice(device, QUrl.fromLocalFile(str(path)))
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if player.error() != QMediaPlayer.Error.NoError:
                raise ValueError(f"Audio decoder failed: {player.errorString()}")
            if player.duration() > 0 and player.mediaStatus() in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            ):
                break
            time.sleep(0.01)
        duration = player.duration()
        if duration <= 0 or not player.hasAudio():
            raise ValueError("Audio decoder found no playable audio track")
        if end_ms > duration:
            raise ValueError("Audio range exceeds decoded duration")
        player.setPosition(start_ms)
        player.play()
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if player.error() != QMediaPlayer.Error.NoError:
                raise ValueError(f"Audio decoder failed: {player.errorString()}")
            for begin, finish in buffers:
                if begin < end_ms and finish > start_ms:
                    return DecodedAudioSegment(duration, begin, finish, path)
            time.sleep(0.01)
        raise TimeoutError("Audio decoder did not produce samples in the range")
    finally:
        player.stop()
        player.setSource(QUrl())


def create_audio_evidence(
    db: sqlite3.Connection,
    *,
    file_id: str,
    data_root: Path,
    start_ms: int,
    end_ms: int,
    content: str,
    actor: str,
    evidence_type: str = "SPEECH",
) -> EvidenceVersion:
    """Create a human audio observation after checking the selected track."""
    if not content.strip() or not actor.strip():
        raise ValueError("Audio observation and actor are required")
    if evidence_type not in ("SPEECH", "OTHER"):
        raise ValueError("Audio observation type must be SPEECH or OTHER")
    asset_id, decoded = decode_audio_segment(
        db, file_id, data_root, start_ms=start_ms, end_ms=end_ms
    )
    locator_data = json.dumps(
        {"start_ms": start_ms, "end_ms": end_ms, "track": "audio"}, sort_keys=True
    )
    validate_locator("TIME_RANGE", locator_data, duration_ms=decoded.duration_ms)
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
    with atomic_transaction(db):
        db.execute(
            "UPDATE asset_files SET duration_ms=? WHERE file_id=?",
            (decoded.duration_ms, file_id),
        )
        EvidenceRepository(db).create_with_version(
            Evidence(evidence_id, asset_id, evidence_type, timestamp, None), version
        )
    return version


def reopen_audio_evidence(
    db: sqlite3.Connection, version_id: str, data_root: Path
) -> tuple[str, DecodedAudioSegment, int, int]:
    reopened = reopen_evidence_version(db, version_id, data_root)
    if reopened.locator_type != "TIME_RANGE" or reopened.locator["track"] != "audio":
        raise ValueError("Selected Evidence is not an audio time range")
    start_ms = reopened.locator["start_ms"]
    end_ms = reopened.locator["end_ms"]
    if type(start_ms) is not int or type(end_ms) is not int:
        raise ValueError("Audio locator is malformed")
    _asset_id, decoded = decode_audio_segment(
        db, reopened.anchor_file_id, data_root, start_ms=start_ms, end_ms=end_ms
    )
    row = db.execute(
        "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
        (version_id,),
    ).fetchone()
    return str(row[0]), decoded, start_ms, end_ms
