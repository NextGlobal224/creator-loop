"""Read historical RAW and select a MODEL Evidence version without acceptance.

Call from owned, process-deadlined I/O, never a GUI thread. No engine/model
installation is needed to read a retained transcription. SQL owns lineage and
run state; immutable provenance supplies historical settings only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import open_readonly, validate
from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion
from creator_loop.publication_media import _open_read_lock, hold_registered_file
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.transactions import atomic_transaction
from creator_loop.whisper_adapter import (
    MAX_RAW_BYTES,
    WhisperSegment,
    parse_whisper_raw,
)

MODEL_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"


@dataclass(frozen=True)
class TranscriptSuggestions:
    raw_file_id: str
    raw_sha256: str
    asset_id: str
    original_file_id: str
    run_id: str
    language: str
    segments: tuple[WhisperSegment, ...]


def _root(root: Path, coordination: AppDataLock) -> Path:
    canonical = root.resolve(strict=True)
    if (
        not isinstance(coordination, AppDataLock)
        or not coordination.held
        or coordination.root != canonical
    ):
        raise RuntimeError("Transcript selection requires this root's held app lock")
    require_no_pending_restore(canonical)
    database = canonical / "creator_loop.sqlite3"
    if not database.is_file() or database.is_symlink() or database.is_junction():
        raise ValueError("Transcript selection requires an existing regular database")
    return canonical


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate transcription provenance field")
        result[key] = value
    return result


def _verified_bytes(
    db: sqlite3.Connection,
    file_id: str,
    root: Path,
    held: ExitStack,
    *,
    maximum: int,
) -> tuple[bytes, str]:
    row = db.execute(
        "SELECT role,storage_key,sha256,byte_size FROM asset_files WHERE file_id=?",
        (file_id,),
    ).fetchone()
    if row is None or type(row[3]) is not int or not 0 < row[3] <= maximum:
        raise ValueError("Registered transcription file exceeds read budget")
    hold_registered_file(
        db,
        file_id=file_id,
        expected_digest=row[2],
        data_root=root,
        handles=held,
    )
    # The verified lease denies replacement/write until the enclosing commit.
    with _open_read_lock(resolve_storage_path(root, row[0], row[1])) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) != row[3]:
        raise ValueError("Registered transcription read size changed")
    return raw, str(row[2])


def _load(
    db: sqlite3.Connection, raw_file_id: str, root: Path, held: ExitStack
) -> TranscriptSuggestions:
    row = db.execute(
        """SELECT f.asset_id,f.processing_run_id,p.file_id,p.duration_ms,
                  o.file_id,o.duration_ms,p.sha256,p.byte_size,o.sha256
           FROM asset_files f
           JOIN assets a ON a.asset_id=f.asset_id AND a.deleted_at IS NULL
           JOIN processing_runs r ON r.run_id=f.processing_run_id AND r.asset_id=f.asset_id
           JOIN asset_files p ON p.file_id=f.parent_file_id AND p.asset_id=f.asset_id
           JOIN processing_runs d ON d.run_id=p.processing_run_id AND d.asset_id=f.asset_id
           JOIN asset_files o ON o.file_id=p.parent_file_id AND o.asset_id=f.asset_id
           WHERE f.file_id=? AND a.media_type='VIDEO'
           AND f.role='OTHER' AND f.mime_type='application/json'
           AND r.input_file_id=p.file_id AND r.task_type='AUDIO_TRANSCRIPTION'
           AND r.status='SUCCEEDED' AND r.finished_at IS NOT NULL
           AND r.tool_name='whisper.cpp' AND r.tool_version='1.8.7'
           AND r.model_name='Whisper base multilingual' AND r.model_version=?
           AND p.role='DERIVED_AUDIO' AND p.mime_type='audio/wav'
           AND d.input_file_id=o.file_id AND d.task_type='VIDEO_AUDIO_DECODE'
           AND d.status='SUCCEEDED' AND d.finished_at IS NOT NULL
           AND o.role='ORIGINAL' AND o.mime_type='video/mp4'""",
        (raw_file_id, MODEL_REVISION),
    ).fetchone()
    if (
        row is None
        or type(row[3]) is not int
        or type(row[5]) is not int
        or not 0 < row[3] <= row[5]
    ):
        raise ValueError("RAW requires successful same-Asset VIDEO/PCM lineage")
    (
        asset_id,
        run_id,
        pcm_id,
        duration,
        original_id,
        _,
        pcm_digest,
        pcm_size,
        original_digest,
    ) = row
    for identity, digest in ((pcm_id, pcm_digest), (original_id, original_digest)):
        hold_registered_file(
            db,
            file_id=identity,
            expected_digest=digest,
            data_root=root,
            handles=held,
        )
    provenance_ids = db.execute(
        """SELECT file_id FROM asset_files WHERE asset_id=? AND parent_file_id=?
           AND processing_run_id=? AND role='OTHER' AND mime_type='application/json'""",
        (asset_id, raw_file_id, run_id),
    ).fetchall()
    if len(provenance_ids) != 1:
        raise ValueError("RAW requires one registered immutable provenance artifact")
    raw, digest = _verified_bytes(db, raw_file_id, root, held, maximum=MAX_RAW_BYTES)
    metadata, _ = _verified_bytes(db, provenance_ids[0][0], root, held, maximum=65536)
    try:
        provenance = json.loads(metadata.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid transcription provenance JSON") from exc
    if not isinstance(provenance, dict):
        raise ValueError("Invalid transcription provenance shape")
    expected = {
        "kind": "WHISPER_TRANSCRIPTION_PROVENANCE",
        "run_id": run_id,
        "input_file_id": pcm_id,
        "original_file_id": original_id,
        "raw_file_id": raw_file_id,
        "raw_sha256": digest,
        "input_sha256": pcm_digest,
        "input_bytes": pcm_size,
    }
    settings = provenance.get("settings")
    if (
        type(provenance.get("format")) is not int
        or provenance["format"] != 1
        or any(provenance.get(key) != value for key, value in expected.items())
        or not isinstance(settings, dict)
        or not isinstance(settings.get("language"), str)
        or re.fullmatch("[a-z]{2}", settings["language"]) is None
    ):
        raise ValueError("Transcription provenance does not match registered lineage")
    language = settings["language"]
    return TranscriptSuggestions(
        raw_file_id,
        digest,
        str(asset_id),
        str(original_id),
        str(run_id),
        language,
        parse_whisper_raw(raw, duration_ms=duration, language=language),
    )


def load_transcript_suggestions(
    raw_file_id: str, root: Path, coordination: AppDataLock
) -> TranscriptSuggestions:
    canonical = _root(root, coordination)
    with (
        closing(open_readonly(canonical / "creator_loop.sqlite3")) as db,
        ExitStack() as held,
    ):
        validate(db)
        return _load(db, raw_file_id, canonical, held)


def create_transcript_evidence(
    raw_file_id: str,
    segment_index: int,
    expected_raw_sha256: str,
    root: Path,
    coordination: AppDataLock,
) -> EvidenceVersion:
    """Explicit segment selection creates a fresh MODEL/SPEECH version, PENDING.

    This is a machine suggestion, even when a person chooses it. A later human
    correction appends a HUMAN version; review remains a separate domain action.
    """
    canonical = _root(root, coordination)
    if type(segment_index) is not int or segment_index < 0:
        raise ValueError("Select one machine segment by its nonnegative index")
    with (
        closing(
            sqlite3.connect(
                (canonical / "creator_loop.sqlite3").as_uri() + "?mode=rw",
                uri=True,
                timeout=10,
            )
        ) as db,
        ExitStack() as held,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        validate(db)
        with atomic_transaction(db):
            suggestions = _load(db, raw_file_id, canonical, held)
            if suggestions.raw_sha256 != expected_raw_sha256 or segment_index >= len(
                suggestions.segments
            ):
                raise ValueError("Machine segment selection is stale or absent")
            segment = suggestions.segments[segment_index]
            if not segment.text.strip():
                raise ValueError("Empty machine text cannot become Evidence")
            timestamp = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            evidence_id = uuid4().hex
            version = EvidenceVersion(
                uuid4().hex,
                evidence_id,
                suggestions.asset_id,
                1,
                suggestions.original_file_id,
                segment.text,
                "TIME_RANGE",
                json.dumps(
                    {
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "track": "audio",
                    },
                    sort_keys=True,
                ),
                "MODEL",
                suggestions.run_id,
                None,
                timestamp,
            )
            EvidenceRepository(db).create_with_version(
                Evidence(evidence_id, suggestions.asset_id, "SPEECH", timestamp, None),
                version,
            )
        return version
