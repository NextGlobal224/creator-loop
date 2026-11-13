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
from dataclasses import asdict
from typing import Any

from creator_loop.backend.knowledge.cloud_audio import (
    CloudAudioProbe,
    CloudTranscript,
    CloudTranscriptChunk,
    merge_whisper_chunks,
    parse_whisper_transcript,
    probe_pcm_wav,
)
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
    for raw in decoded_chunks:
        probe = probe_pcm_wav(raw)
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
    return merge_whisper_chunks(chunks, source_duration_ms=value["duration_ms"])
