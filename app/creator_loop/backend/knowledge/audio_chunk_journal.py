"""Durable transcript chunk state; no decoding, provider calls or cost grant.

The caller must supply chunks produced by a trusted decoder for the exact
original. This module verifies identities and timing, but cannot prove that
the decoder actually derived those chunks from compressed source bytes.
It is not wired to the Worker or exposed as a client endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from creator_loop.backend.knowledge.cloud_audio import (
    WHISPER_MODEL,
    CloudAudioProbe,
    CloudTranscript,
    CloudTranscriptChunk,
    _store_verified_transcript,
    merge_whisper_chunks,
    parse_whisper_transcript,
    probe_pcm_wav,
)
from creator_loop.backend.knowledge.evidence_reopen import _anchor_path
from creator_loop.backend.runtime.transactions import atomic_transaction

_ID = re.compile(r"[0-9a-f]{32}\Z")
_LIMIT = 1024 * 1024


def _encoded(value: Any) -> str:
    result = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(result.encode()) > _LIMIT:
        raise ValueError("Audio journal exceeds retained response limit")
    return result


def _read(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise ValueError("Invalid audio journal identity")
    row = db.execute(
        "SELECT record FROM control WHERE id=?", ("audio-chunks-" + run_id,)
    ).fetchone()
    if not row:
        raise ValueError("Audio journal does not exist")
    value = json.loads(row[0])
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ValueError("Audio journal identity changed")
    return value


def _save(db: sqlite3.Connection, value: dict[str, Any]) -> None:
    db.execute(
        "UPDATE control SET record=? WHERE id=?",
        (_encoded(value), "audio-chunks-" + value["run_id"]),
    )


def _slice_frames_and_codec(raw: bytes, probe: CloudAudioProbe) -> tuple[int, int]:
    """Inspect PCM layout already validated by probe_pcm_wav."""
    offset = 12
    codec = 0
    while offset < len(raw):
        length = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        if raw[offset : offset + 4] == b"fmt ":
            codec = int.from_bytes(raw[offset + 8 : offset + 10], "little")
        if raw[offset : offset + 4] == b"data":
            frames = length // (probe.channels * probe.sample_width)
            return frames, codec
        offset += 8 + length + length % 2
    raise ValueError("Audio chunk has no PCM data")


def create_chunk_journal(
    db: sqlite3.Connection,
    *,
    run_id: str,
    source: bytes,
    decoded_chunks: tuple[bytes, ...],
    duration_ms: int,
    decoder: str,
    model: str,
    deployment: str,
) -> dict[str, Any]:
    """Persist a bounded plan; chunks and original remain outside the journal."""
    if (
        not isinstance(run_id, str)
        or not _ID.fullmatch(run_id)
        or not isinstance(source, bytes)
        or not 0 < len(source) <= 8 * 1024 * 1024
        or not isinstance(decoded_chunks, tuple)
        or not 1 <= len(decoded_chunks) <= 64
        or type(duration_ms) is not int
        or not 0 < duration_ms <= 900_000
        or not isinstance(decoder, str)
        or not 1 <= len(decoder.strip()) <= 200
        or not isinstance(model, str)
        or not 1 <= len(model.strip()) <= 200
        or not isinstance(deployment, str)
        or not 1 <= len(deployment.strip()) <= 200
        or any(not isinstance(raw, bytes) for raw in decoded_chunks)
        or sum(len(raw) for raw in decoded_chunks) > 32 * 1024 * 1024
    ):
        raise ValueError("Invalid audio journal plan")
    source_sha = hashlib.sha256(source).hexdigest()
    start = 0
    chunks = []
    format_identity = None
    final_fractional = False
    for index, raw in enumerate(decoded_chunks):
        probe = probe_pcm_wav(raw)
        frames, codec = _slice_frames_and_codec(raw, probe)
        fractional = bool(frames * 1000 % probe.sample_rate)
        if fractional and index != len(decoded_chunks) - 1:
            raise ValueError("Non-final audio chunk has fractional milliseconds")
        final_fractional = fractional
        current_format = (probe.sample_rate, probe.channels, probe.sample_width, codec)
        if format_identity is not None and current_format != format_identity:
            raise ValueError("Decoded audio chunk format changed")
        format_identity = current_format
        request_id = hashlib.sha256(
            f"{run_id}:{source_sha}:{start}:{probe.sha256}".encode()
        ).hexdigest()[:32]
        chunks.append(
            {
                "start_ms": start,
                "probe": asdict(probe),
                "request_id": request_id,
                "state": "READY",
            }
        )
        start += probe.duration_ms
    if start != duration_ms:
        raise ValueError("Decoded chunks do not cover the declared duration")
    value = {
        "run_id": run_id,
        "source_sha256": source_sha,
        "source_size": len(source),
        "duration_ms": duration_ms,
        "final_duration_ceiled": final_fractional,
        "decoder": decoder,
        "model": model,
        "deployment": deployment,
        "state": "ACTIVE",
        "chunks": chunks,
    }
    with atomic_transaction(db):
        db.execute(
            "INSERT INTO control(id,record) VALUES(?,?)",
            ("audio-chunks-" + run_id, _encoded(value)),
        )
    return value


def reserve_next_chunk(
    db: sqlite3.Connection, *, run_id: str, source: bytes, chunk: bytes
) -> dict[str, Any]:
    """Persist UNKNOWN *before* dispatch; a second dispatch is refused."""
    if db.in_transaction:
        raise ValueError("Reservation requires its own committed transaction")
    with atomic_transaction(db):
        value = _read(db, run_id)
        if value["state"] != "ACTIVE" or any(
            c["state"] not in ("READY", "RECEIVED") for c in value["chunks"]
        ):
            raise ValueError("Audio job stopped or outcome unknown; do not retry")
        if (
            not isinstance(source, bytes)
            or len(source) != value["source_size"]
            or hashlib.sha256(source).hexdigest() != value["source_sha256"]
        ):
            raise ValueError("Original audio changed")
        next_chunk = next((c for c in value["chunks"] if c["state"] == "READY"), None)
        if next_chunk is None:
            raise ValueError("No unreserved audio chunk")
        if asdict(probe_pcm_wav(chunk)) != next_chunk["probe"]:
            raise ValueError("Decoded audio chunk changed")
        next_chunk["state"] = "UNKNOWN"
        _save(db, value)
        return {
            "request_id": next_chunk["request_id"],
            "source_sha256": value["source_sha256"],
            "input_sha256": next_chunk["probe"]["sha256"],
            "start_ms": next_chunk["start_ms"],
            "model": value["model"],
            "deployment": value["deployment"],
        }


def record_chunk_response(
    db: sqlite3.Connection, *, run_id: str, envelope: dict[str, Any]
) -> str:
    """Retain an identity-matched response, including one arriving after stop."""
    if not isinstance(envelope, dict) or set(envelope) != {
        "request_id",
        "source_sha256",
        "input_sha256",
        "model",
        "deployment",
        "native_response",
        "native_usage",
    }:
        raise ValueError("Invalid audio response envelope")
    _encoded(envelope)
    with atomic_transaction(db):
        value = _read(db, run_id)
        matches = [
            c for c in value["chunks"] if c["request_id"] == envelope["request_id"]
        ]
        if len(matches) != 1:
            raise ValueError("Unknown audio request")
        item = matches[0]
        if (
            envelope["source_sha256"] != value["source_sha256"]
            or envelope["input_sha256"] != item["probe"]["sha256"]
            or envelope["model"] != value["model"]
            or envelope["deployment"] != value["deployment"]
        ):
            raise ValueError("Audio response identity mismatch")
        if item["state"] in ("RECEIVED", "INVALID"):
            if item["envelope"] != envelope:
                raise ValueError("Audio response conflicts with retained receipt")
            return (
                "CANCELLED_RECEIPT_RETAINED"
                if value["state"] == "CANCELLED"
                else str(item["state"])
            )
        if item["state"] != "UNKNOWN":
            raise ValueError("Audio request was not reserved")
        try:
            parse_whisper_transcript(
                envelope["native_response"], CloudAudioProbe(**item["probe"])
            )
        except ValueError:
            status = "INVALID"
        else:
            status = "RECEIVED"
        item.update(state=status, envelope=envelope)
        _save(db, value)
        return "CANCELLED_RECEIPT_RETAINED" if value["state"] == "CANCELLED" else status


async def dispatch_next_chunk(
    db: sqlite3.Connection,
    *,
    run_id: str,
    source: bytes,
    chunk: bytes,
    provider: Callable[[dict[str, Any], bytes], Awaitable[dict[str, Any]]],
) -> str:
    """Dispatch one chunk after its reservation commits.

    The caller must enforce the actual grant and cost guard before entry.
    On interruption or provider error, UNKNOWN remains durable. This routine
    is not an endpoint and does not fence a concurrent stop at the network
    boundary; Worker integration must solve that race separately.
    """
    packet = reserve_next_chunk(db, run_id=run_id, source=source, chunk=chunk)
    envelope = await provider(packet, chunk)
    return record_chunk_response(db, run_id=run_id, envelope=envelope)


def stop_chunk_journal(db: sqlite3.Connection, *, run_id: str) -> None:
    with atomic_transaction(db):
        value = _read(db, run_id)
        value["state"] = "CANCELLED"
        _save(db, value)


def merge_chunk_journal(
    db: sqlite3.Connection, *, run_id: str, source: bytes
) -> CloudTranscript:
    value = _read(db, run_id)
    if value["state"] != "ACTIVE" or any(
        c["state"] != "RECEIVED" for c in value["chunks"]
    ):
        raise ValueError("Audio responses incomplete, stopped or unknown")
    if (
        not isinstance(source, bytes)
        or len(source) != value["source_size"]
        or hashlib.sha256(source).hexdigest() != value["source_sha256"]
    ):
        raise ValueError("Original audio changed")
    chunks = tuple(
        CloudTranscriptChunk(
            c["start_ms"],
            CloudAudioProbe(**c["probe"]),
            c["envelope"]["native_response"],
        )
        for c in value["chunks"]
    )
    transcript = merge_whisper_chunks(chunks, source_duration_ms=value["duration_ms"])
    if value.get("final_duration_ceiled") is True:
        transcript = replace(
            transcript,
            warnings=(*transcript.warnings, "FINAL_DURATION_CEILED_TO_MILLISECOND"),
        )
    return transcript


def store_completed_chunk_journal(
    db: sqlite3.Connection, *, data_root: Path, run_id: str, file_id: str
) -> dict[str, Any]:
    """Bind retained chunk receipts to versioned evidence in the same DB.

    Offline integration candidate: the decoder's source-to-PCM provenance and
    the real Durable Object storage adapter must still be verified by the caller.
    This function neither dispatches a model nor settles usage.
    """
    with atomic_transaction(db):
        value = _read(db, run_id)
        has_partial = any("partial_artifact" in item for item in value["chunks"])
        row = db.execute(
            """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size,f.duration_ms,
                      a.media_type,r.status,r.model_name
               FROM asset_files f JOIN assets a USING(asset_id)
               JOIN processing_runs r ON r.asset_id=f.asset_id AND r.input_file_id=f.file_id
               WHERE f.file_id=? AND f.role='ORIGINAL' AND r.run_id=?
                 AND r.tool_name='CLOUDFLARE_WORKERS_AI_SOURCE'
                 AND r.task_type='SOURCE_TRANSCRIPTION' AND a.deleted_at IS NULL""",
            (file_id, run_id),
        ).fetchone()
        if (
            row is None
            or row[5:] != ("AUDIO", "RUNNING", WHISPER_MODEL)
            or value["model"] != WHISPER_MODEL
            or (row[4] is not None and row[4] != value["duration_ms"])
        ):
            raise ValueError("Chunk journal does not match the active source run")
        source = _anchor_path(data_root, "ORIGINAL", row[1]).read_bytes()
        if len(source) != row[3] or hashlib.sha256(source).hexdigest() != row[2]:
            raise ValueError("Original audio changed before transcript storage")
        transcript = merge_chunk_journal(db, run_id=run_id, source=source)
        if has_partial:
            # Reuse earlier machine versions and preserve any human edits.
            # Save missing completed chunks in the same transaction, without
            # generating another Evidence for already materialized segments.
            for index in range(len(value["chunks"])):
                store_received_chunk_for_review(
                    db, data_root=data_root, run_id=run_id, file_id=file_id, index=index
                )
            value = _read(db, run_id)
        receipts = {
            "decoder": value["decoder"],
            "deployment": value["deployment"],
            "source_to_decoded_provenance": "NOT_VERIFIED_BY_JOURNAL",
            "chunks": value["chunks"],
        }
        return _store_verified_transcript(
            db,
            data_root=data_root,
            file_id=file_id,
            run_id=run_id,
            asset_id=row[0],
            source_sha=row[2],
            duration_ms=value["duration_ms"],
            model=WHISPER_MODEL,
            native_response=receipts,
            transcript=transcript,
            artifact_format="CREATOR_CLOUD_CHUNKED_TRANSCRIPT_V1",
            create_evidence=not has_partial,
        )


def store_received_chunk_for_review(
    db: sqlite3.Connection, *, data_root: Path, run_id: str, file_id: str, index: int
) -> dict[str, Any]:
    """Persist one received chunk for offline human review without dispatching.

    This requires journal and domain tables in the same SQLite transaction.
    The real Worker/DO adapter and cost reservation for reads/writes remain
    prerequisites; this function is not a client endpoint or a cost guard.
    """
    if type(index) is not int or index < 0:
        raise ValueError("Invalid audio chunk index")
    with atomic_transaction(db):
        value = _read(db, run_id)
        if index >= len(value["chunks"]):
            raise ValueError("Invalid audio chunk index")
        item = value["chunks"][index]
        if item["state"] != "RECEIVED":
            raise ValueError("Only received chunks can be reviewed")
        row = db.execute(
            """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size,f.duration_ms,
                      a.media_type,r.status,r.model_name
               FROM asset_files f JOIN assets a USING(asset_id)
               JOIN processing_runs r ON r.asset_id=f.asset_id AND r.input_file_id=f.file_id
               WHERE f.file_id=? AND f.role='ORIGINAL' AND r.run_id=?
                 AND r.tool_name='CLOUDFLARE_WORKERS_AI_SOURCE'
                 AND r.task_type='SOURCE_TRANSCRIPTION' AND a.deleted_at IS NULL""",
            (file_id, run_id),
        ).fetchone()
        if (
            row is None
            or row[5:] != ("AUDIO", "RUNNING", WHISPER_MODEL)
            or value["model"] != WHISPER_MODEL
            or (row[4] is not None and row[4] != value["duration_ms"])
        ):
            raise ValueError("Chunk journal does not match the active source run")
        source = _anchor_path(data_root, "ORIGINAL", row[1]).read_bytes()
        if len(source) != row[3] or hashlib.sha256(source).hexdigest() != row[2]:
            raise ValueError("Original audio changed before transcript storage")
        if row[2] != value["source_sha256"] or row[3] != value["source_size"]:
            raise ValueError("Journal original does not match source")
        if "partial_artifact" in item:
            existing = item["partial_artifact"]
            artifact = db.execute(
                """SELECT storage_key,sha256,byte_size FROM asset_files
                   WHERE file_id=? AND parent_file_id=? AND processing_run_id=?
                     AND role='OTHER' AND mime_type='application/json'""",
                (existing["artifact_file_id"], file_id, run_id),
            ).fetchone()
            if artifact is None or artifact[1] != existing["sha256"]:
                raise ValueError("Retained partial artifact is missing or changed")
            retained = _anchor_path(data_root, "OTHER", artifact[0]).read_bytes()
            if (
                len(retained) != artifact[2]
                or hashlib.sha256(retained).hexdigest() != artifact[1]
            ):
                raise ValueError("Retained partial artifact is missing or changed")
            manifest = json.loads(retained)
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "CREATOR_CLOUD_CHUNKED_TRANSCRIPT_V1"
                or manifest.get("input_file_id") != file_id
                or manifest.get("input_sha256") != row[2]
                or manifest.get("run_id") != run_id
                or manifest.get("evidence_version_ids")
                != existing["evidence_version_ids"]
                or manifest.get("native_response", {})
                .get("chunk", {})
                .get("request_id")
                != item["request_id"]
            ):
                raise ValueError("Retained partial artifact does not match the chunk")
            for version_id in existing["evidence_version_ids"]:
                if not db.execute(
                    """SELECT 1 FROM evidence_versions WHERE evidence_version_id=?
                       AND anchor_file_id=? AND processing_run_id=? AND producer_type='MODEL'""",
                    (version_id, file_id, run_id),
                ).fetchone():
                    raise ValueError("Retained partial evidence is missing or changed")
            return {**existing, "already_stored": True}
        parsed = parse_whisper_transcript(
            item["envelope"]["native_response"], CloudAudioProbe(**item["probe"])
        )
        offset = item["start_ms"]
        transcript = replace(
            parsed,
            segments=tuple(
                replace(
                    segment,
                    start_ms=segment.start_ms + offset,
                    end_ms=segment.end_ms + offset,
                )
                for segment in parsed.segments
            ),
            warnings=(*parsed.warnings, "PARTIAL_TRANSCRIPT_REQUIRES_REVIEW"),
            untranscribed_ranges=tuple(
                (start + offset, end + offset)
                for start, end in parsed.untranscribed_ranges
            ),
        )
        result = _store_verified_transcript(
            db,
            data_root=data_root,
            file_id=file_id,
            run_id=run_id,
            asset_id=row[0],
            source_sha=row[2],
            duration_ms=value["duration_ms"],
            model=WHISPER_MODEL,
            native_response={
                "decoder": value["decoder"],
                "deployment": value["deployment"],
                "source_to_decoded_provenance": "NOT_VERIFIED_BY_JOURNAL",
                "chunk": item,
            },
            transcript=transcript,
            artifact_format="CREATOR_CLOUD_CHUNKED_TRANSCRIPT_V1",
            complete_run=False,
        )
        item["partial_artifact"] = {
            "artifact_file_id": result["artifact_file_id"],
            "sha256": result["sha256"],
            "evidence_version_ids": result["evidence_version_ids"],
            "review_state": result["review_state"],
        }
        _save(db, value)
        return {**item["partial_artifact"], "already_stored": False}
