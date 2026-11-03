"""Bounded, synchronous snapshot primitives; not a cloud backup or cutover fence.

The caller must execute capture in one Durable Object storage transaction.  This
module intentionally exposes no HTTP route and never schedules jobs or alarms.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import tempfile
import zlib
from pathlib import Path
from typing import Any, Callable

TABLES = {
    "checkpoint": ("id", "revision", "payload", "sha256"),
    "objects": ("sha256", "size"),
    "object_chunks": ("sha256", "position", "content"),
    "receipts": ("id", "fingerprint", "response"),
    "backups": ("id", "created_at", "record"),
    "jobs": ("id", "status", "record"),
    "control": ("id", "record"),
    "preservation_archives": ("sha256", "record"),
    "preservation_chunks": ("sha256", "position", "content"),
}
FORMAT = "B_COORDINATION_SNAPSHOT_V1"
MAX_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 96 * 1024 * 1024
STREAM_PART_BYTES = 256 * 1024
MAX_STREAM_ROW_BYTES = 2 * 1024 * 1024


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _checked_cursor(
    table: str, row: dict[str, Any], keys: tuple[str, ...]
) -> tuple[Any, ...]:
    values = tuple(row[key] for key in keys)
    if any(
        type(value)
        is not (
            int if key == "position" or (table == "checkpoint" and key == "id") else str
        )
        for key, value in zip(keys, values)
    ):
        raise ValueError("invalid snapshot primary key")
    return values


def capture_parts(
    rows: Callable[..., list[dict[str, Any]]],
    write_part: Callable[[int, bytes], None],
    *,
    max_bytes: int = MAX_STREAM_BYTES,
) -> dict[str, Any]:
    """Write a bounded immutable stream under the caller's single SQL transaction.

    `write_part` must persist into a separate table outside TABLES in that same
    transaction and roll back on any error. No alarm, late write or proof of quiescence is implied. This is not
    exposed via the Worker and has not been tested on Cloudflare.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_STREAM_BYTES:
        raise ValueError("invalid stream size bound")
    buffer = bytearray()
    parts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    total = 0
    whole = hashlib.sha256()

    def emit(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > max_bytes:
            raise ValueError("stream exceeds bound; transaction must roll back")
        whole.update(data)
        offset = 0
        while offset < len(data):
            amount = min(STREAM_PART_BYTES - len(buffer), len(data) - offset)
            buffer.extend(data[offset : offset + amount])
            offset += amount
            if len(buffer) == STREAM_PART_BYTES:
                flush()

    def flush() -> None:
        if buffer:
            data = bytes(buffer)
            write_part(len(parts), data)
            parts.append(
                {
                    "position": len(parts),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            buffer.clear()

    for table, columns in TABLES.items():
        keys = (
            columns[:2]
            if table in ("object_chunks", "preservation_chunks")
            else columns[:1]
        )
        order = ",".join(keys)
        lengths = "+".join(
            "COALESCE(length(CAST(" + c + " AS BLOB)),0)" for c in columns
        )
        count = rows("SELECT COUNT(*) AS n FROM " + table)[0]["n"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid row count")
        counts[table] = count
        cursor: tuple[Any, ...] | None = None
        for _ in range(count):
            where = (
                ""
                if cursor is None
                else " WHERE (" + order + ") > (" + ",".join("?" for _ in keys) + ")"
            )
            args = () if cursor is None else cursor
            size = rows(
                "SELECT "
                + lengths
                + " AS bytes FROM "
                + table
                + where
                + " ORDER BY "
                + order
                + " LIMIT 1",
                *args,
            )
            if (
                len(size) != 1
                or type(size[0]["bytes"]) is not int
                or size[0]["bytes"] < 0
                or size[0]["bytes"] > MAX_STREAM_ROW_BYTES
            ):
                raise ValueError("row exceeds bounded stream capacity")
            result = rows(
                "SELECT "
                + ",".join(columns)
                + " FROM "
                + table
                + where
                + " ORDER BY "
                + order
                + " LIMIT 1",
                *args,
            )
            if len(result) != 1 or set(result[0]) != set(columns):
                raise ValueError("snapshot row changed or invalid")
            cursor = _checked_cursor(table, result[0], keys)
            row = result[0]
            # JSON escaping can expand a valid SQL row beyond the line bound.
            # Encode only affected fields before JSON serialization: this bounds
            # expansion without materializing a many-megabyte escaped string.
            encoded = {
                column: base64.b64encode(value.encode("utf-8")).decode("ascii")
                for column, value in row.items()
                if isinstance(value, str) and re.search(r'[\x00-\x1f"\\]', value)
            }
            record = {"table": table, "row": {**row, **encoded}}
            if encoded:
                record["base64_fields"] = sorted(encoded)
            line = _bytes(record)
            if len(line) > 4 * 1024 * 1024:
                raise ValueError("encoded row exceeds stream line bound")
            emit(line + b"\n")
    flush()
    return {
        "format": "B_COORDINATION_STREAM_V2",
        "bytes": total,
        "sha256": whole.hexdigest(),
        "parts": parts,
        "counts": counts,
        "alarm": "NOT_CAPTURED",
        "late_writes_after_snapshot": "NOT_CAPTURED",
        "sessions": "EXCLUDED",
        "backup_history": "INCLUDED",
    }


def capture_pinned(
    rows: Callable[..., list[dict[str, Any]]],
    execute: Callable[..., Any],
    snapshot_id: str,
    *,
    max_bytes: int = MAX_STREAM_BYTES,
) -> dict[str, Any]:
    """Pin chunks outside captured tables; caller must supply one atomic transaction.

    Requires separate snapshot_parts and snapshot_manifests tables. No Worker
    route, cost validation, alarm fencing or Cloudflare guarantee is supplied.
    """
    if not isinstance(snapshot_id, str) or not re.fullmatch(
        r"[a-f0-9]{32}", snapshot_id
    ):
        raise ValueError("invalid snapshot id")
    if rows(
        "SELECT 1 FROM snapshot_parts WHERE snapshot_id=? LIMIT 1", snapshot_id
    ) or rows("SELECT 1 FROM snapshot_manifests WHERE id=? LIMIT 1", snapshot_id):
        raise ValueError("snapshot id already exists")

    def write_part(position: int, data: bytes) -> None:
        execute(
            "INSERT INTO snapshot_parts(snapshot_id,position,content) VALUES(?,?,?)",
            snapshot_id,
            position,
            base64.b64encode(data).decode(),
        )

    manifest = capture_parts(rows, write_part, max_bytes=max_bytes)
    execute(
        "INSERT INTO snapshot_manifests(id,record) VALUES(?,?)",
        snapshot_id,
        _bytes(manifest).decode(),
    )
    return manifest


def restore_parts(
    manifest: Any,
    read_part: Callable[[int], bytes],
    rows: Callable[..., list[dict[str, Any]]],
    execute: Callable[..., Any],
    *,
    verify: Callable[[], None],
) -> None:
    """Reconstruct into an empty *isolated* target inside a caller transaction.

    Checks transport and table counts, not domain/media semantics. The caller
    must enforce isolation, rollback, disable job execution and provide semantic
    verification within the transaction; alarms and later writes are separate.
    """
    keys = {
        "format",
        "bytes",
        "sha256",
        "parts",
        "counts",
        "alarm",
        "late_writes_after_snapshot",
        "sessions",
        "backup_history",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != keys
        or manifest["format"]
        not in ("B_COORDINATION_STREAM_V1", "B_COORDINATION_STREAM_V2")
        or manifest["alarm"] != "NOT_CAPTURED"
        or manifest["late_writes_after_snapshot"] != "NOT_CAPTURED"
        or manifest["sessions"] != "EXCLUDED"
        or manifest["backup_history"] != "INCLUDED"
    ):
        raise ValueError("invalid stream manifest")
    if (
        type(manifest["bytes"]) is not int
        or not 0 <= manifest["bytes"] <= MAX_STREAM_BYTES
        or not isinstance(manifest["parts"], list)
        or not isinstance(manifest["counts"], dict)
        or set(manifest["counts"]) != set(TABLES)
    ):
        raise ValueError("invalid stream bounds")
    if (
        len(manifest["parts"])
        > (MAX_STREAM_BYTES + STREAM_PART_BYTES - 1) // STREAM_PART_BYTES
    ):
        raise ValueError("too many stream parts")
    if any(type(v) is not int or v < 0 for v in manifest["counts"].values()):
        raise ValueError("invalid stream counts")
    for table in TABLES:
        if rows("SELECT 1 FROM " + table + " LIMIT 1"):
            raise ValueError("target is not empty")
    counts = dict.fromkeys(TABLES, 0)
    whole = hashlib.sha256()
    total = 0
    pending = bytearray()
    for index, part in enumerate(manifest["parts"]):
        if (
            not isinstance(part, dict)
            or set(part) != {"position", "bytes", "sha256"}
            or part["position"] != index
            or type(part["bytes"]) is not int
            or not 0 < part["bytes"] <= STREAM_PART_BYTES
        ):
            raise ValueError("invalid stream part manifest")
        if index < len(manifest["parts"]) - 1 and part["bytes"] != STREAM_PART_BYTES:
            raise ValueError("invalid nonfinal stream part size")
        data = read_part(index)
        if (
            not isinstance(data, bytes)
            or len(data) != part["bytes"]
            or hashlib.sha256(data).hexdigest() != part["sha256"]
        ):
            raise ValueError("stream part mismatch")
        total += len(data)
        if total > MAX_STREAM_BYTES:
            raise ValueError("stream exceeds bound")
        whole.update(data)
        pending.extend(data)
        while b"\n" in pending:
            end = pending.index(b"\n")
            if end > 4 * 1024 * 1024:
                raise ValueError("stream line too large")
            record = json.loads(bytes(pending[:end]))
            del pending[: end + 1]
            if (
                not isinstance(record, dict)
                or set(record)
                not in ({"table", "row"}, {"table", "row", "base64_fields"})
                or record["table"] not in TABLES
                or not isinstance(record["row"], dict)
            ):
                raise ValueError("invalid stream row")
            table = record["table"]
            columns = TABLES[table]
            if set(record["row"]) != set(columns):
                raise ValueError("invalid stream columns")
            if "base64_fields" in record:
                fields = record["base64_fields"]
                if (
                    manifest["format"] != "B_COORDINATION_STREAM_V2"
                    or not isinstance(fields, list)
                    or not fields
                    or any(
                        not isinstance(field, str) or field not in columns
                        for field in fields
                    )
                    or len(fields) != len(set(fields))
                ):
                    raise ValueError("invalid encoded stream columns")
                try:
                    for field in fields:
                        record["row"][field] = base64.b64decode(
                            record["row"][field], validate=True
                        ).decode("utf-8")
                except (ValueError, TypeError, UnicodeDecodeError) as error:
                    raise ValueError("invalid encoded stream text") from error
            row_bytes = 0
            for value in record["row"].values():
                if isinstance(value, str):
                    row_bytes += len(value.encode("utf-8"))
                elif type(value) is float and math.isfinite(value):
                    pass
                elif value is not None and type(value) is not int:
                    raise ValueError("invalid stream value")
            if row_bytes > MAX_STREAM_ROW_BYTES:
                raise ValueError("restored row exceeds bounded stream capacity")
            counts[table] += 1
            if counts[table] > manifest["counts"][table]:
                raise ValueError("stream row count exceeded")
            statement = (
                "INSERT INTO "
                + table
                + " ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")"
            )
            execute(statement, *(record["row"][column] for column in columns))
        if len(pending) > 4 * 1024 * 1024:
            raise ValueError("stream line too large")
    if (
        pending
        or total != manifest["bytes"]
        or whole.hexdigest() != manifest["sha256"]
        or counts != manifest["counts"]
    ):
        raise ValueError("incomplete stream")
    verify()


def _checkpoint_media_references(checkpoint: Any) -> dict[str, int]:
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != set(TABLES["checkpoint"])
        or checkpoint["id"] != 1
    ):
        raise ValueError("invalid checkpoint")
    stream = zlib.decompressobj()
    raw = stream.decompress(
        base64.b64decode(checkpoint["payload"], validate=True), 16 * 1024 * 1024 + 1
    )
    if (
        len(raw) > 16 * 1024 * 1024
        or not stream.eof
        or stream.unused_data
        or stream.unconsumed_tail
        or hashlib.sha256(raw).hexdigest() != checkpoint["sha256"]
    ):
        raise ValueError("checkpoint hash or size invalid")
    references: dict[str, int] = {}
    try:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.sqlite3"
            path.write_bytes(raw)
            db = sqlite3.connect(path)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (10, 11):
                    raise ValueError("unsupported checkpoint schema")
                from creator_loop.backend.runtime.database import (
                    validate as validate_domain,
                )

                validate_domain(db, expected_version=version)
                for key, sha, size in db.execute(
                    "SELECT storage_key,sha256,byte_size FROM asset_files WHERE role='ORIGINAL'"
                ):
                    if key != "storage/originals/" + sha or (
                        sha in references and references[sha] != size
                    ):
                        raise ValueError("checkpoint media reference invalid")
                    references[sha] = size
            finally:
                db.close()
    except (sqlite3.Error, RuntimeError) as exc:
        raise ValueError("invalid checkpoint database") from exc
    return references


def verify_restored(rows: Callable[..., list[dict[str, Any]]]) -> None:
    """Verify restored domain/media and stream large coordination tables.

    Does not establish provenance of a snapshot, alarm state, semantic validity
    of grants/reservations, or the absence of later/in-flight writes.
    """

    def scan(table: str, columns: tuple[str, ...], key_columns: tuple[str, ...]):
        order = ",".join(key_columns)
        cursor = None
        while True:
            where = (
                ""
                if cursor is None
                else " WHERE ("
                + order
                + ") > ("
                + ",".join("?" for _ in key_columns)
                + ")"
            )
            found = rows(
                "SELECT "
                + ",".join(columns)
                + " FROM "
                + table
                + where
                + " ORDER BY "
                + order
                + " LIMIT 1",
                *(cursor or ()),
            )
            if not found:
                return
            if len(found) != 1 or set(found[0]) != set(columns):
                raise ValueError("invalid restored row")
            cursor = _checked_cursor(table, found[0], key_columns)
            yield found[0]

    checkpoints = rows("SELECT id,revision,payload,sha256 FROM checkpoint")
    if len(checkpoints) != 1 or checkpoints[0]["id"] != 1:
        raise ValueError("invalid checkpoint")
    references = _checkpoint_media_references(checkpoints[0])
    objects = {}
    for item in scan("objects", TABLES["objects"], ("sha256",)):
        key, size = item["sha256"], item["size"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or type(size) is not int
            or size < 0
            or key in objects
        ):
            raise ValueError("invalid media identity")
        objects[key] = (size, hashlib.sha256(), 0, 0)
    for key, size in references.items():
        if key not in objects or objects[key][0] != size:
            raise ValueError("checkpoint references missing media")
    for item in scan("object_chunks", TABLES["object_chunks"], ("sha256", "position")):
        key = item["sha256"]
        if key not in objects:
            raise ValueError("orphan media chunk")
        expected_size, state, position, total = objects[key]
        if item["position"] != position:
            raise ValueError("missing media chunk")
        content = base64.b64decode(item["content"], validate=True)
        if len(content) > 48000:
            raise ValueError("oversized media chunk")
        state.update(content)
        objects[key] = (expected_size, state, position + 1, total + len(content))
    for key, (size, state, _, total) in objects.items():
        if total != size or state.hexdigest() != key:
            raise ValueError("media digest or size invalid")
    for job in scan("jobs", TABLES["jobs"], ("id",)):
        record = json.loads(job["record"])
        if (
            not isinstance(record, dict)
            or record.get("id") != job["id"]
            or record.get("status") != job["status"]
        ):
            raise ValueError("job identity or status mismatch")
    for table in ("control", "receipts"):
        for item in scan(table, TABLES[table], ("id",)):
            json.loads(item["response"] if table == "receipts" else item["record"])
    for item in scan("backups", TABLES["backups"], ("id",)):
        backup = json.loads(item["record"])
        if (
            not isinstance(backup, dict)
            or backup.get("format") != "CREATOR_WEB_BACKUP_V1"
            or backup.get("version") != "B-web/0.2.0-beta.1"
            or not isinstance(backup.get("checkpoint"), list)
            or len(backup["checkpoint"]) != 1
            or not isinstance(backup.get("objects"), list)
        ):
            raise ValueError("invalid historical backup")
        historical_objects = {}
        for obj in backup["objects"]:
            if not isinstance(obj, dict) or set(obj) != {"sha256", "size"}:
                raise ValueError("invalid historical backup media")
            key, size = obj["sha256"], obj["size"]
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-f0-9]{64}", key)
                or type(size) is not int
                or size < 0
                or key in historical_objects
            ):
                raise ValueError("invalid historical backup media identity")
            if key not in objects or objects[key][0] != size:
                raise ValueError("historical backup references missing media")
            historical_objects[key] = size
        for key, size in _checkpoint_media_references(backup["checkpoint"][0]).items():
            if historical_objects.get(key) != size:
                raise ValueError("historical checkpoint references missing media")
    archives = {}
    for item in scan(
        "preservation_archives", TABLES["preservation_archives"], ("sha256",)
    ):
        record = json.loads(item["record"])
        if (
            not isinstance(record, dict)
            or record.get("sha256") != item["sha256"]
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or record.get("status") not in ("STAGING", "VERIFIED")
            or type(record.get("bytes")) is not int
            or not 0 < record["bytes"] <= 64 * 1024 * 1024
            or not isinstance(record.get("source_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", record["source_commit"])
        ):
            raise ValueError("invalid preservation archive")
        hashes = record.get("chunk_hashes")
        if (
            not isinstance(hashes, list)
            or len(hashes) != (record["bytes"] + 524287) // 524288
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in hashes
            )
        ):
            raise ValueError("invalid preservation manifest")
        archives[item["sha256"]] = (record, hashlib.sha256(), 0)
    for item in scan(
        "preservation_chunks", TABLES["preservation_chunks"], ("sha256", "position")
    ):
        key = item["sha256"]
        if key not in archives:
            raise ValueError("orphan preservation chunk")
        record, digest_state, count = archives[key]
        position = item["position"]
        if type(position) is not int or not 0 <= position < len(record["chunk_hashes"]):
            raise ValueError("invalid preservation position")
        content = base64.b64decode(item["content"], validate=True)
        if (
            len(content) != min(524288, record["bytes"] - position * 524288)
            or hashlib.sha256(content).hexdigest() != record["chunk_hashes"][position]
        ):
            raise ValueError("preservation chunk mismatch")
        digest_state.update(content)
        archives[key] = (record, digest_state, count + 1)
    for key, (record, digest_state, count) in archives.items():
        if record["status"] == "VERIFIED" and (
            count != len(record["chunk_hashes"]) or digest_state.hexdigest() != key
        ):
            raise ValueError("verified preservation archive incomplete or mismatched")


def capture(
    rows: Callable[..., list[dict[str, Any]]], limit: int = MAX_BYTES
) -> dict[str, Any]:
    """Capture ordered rows inside caller's transaction; no pagination or awaits.

    SQL snapshot does not include alarm state, sessions, or
    effects/receipts which arrive after the transaction.
    """
    if type(limit) is not int or not 0 < limit <= MAX_BYTES:
        raise ValueError("invalid snapshot size bound")
    tables: dict[str, Any] = {}
    used = 0
    for table, columns in TABLES.items():
        # Check serialized source size before materializing a table. Never use a
        # truncated result as a successful backup. The later JSON-size check
        # accounts for escaping and manifest overhead.
        lengths = "+".join(
            "COALESCE(length(CAST(" + c + " AS BLOB)),0)" for c in columns
        )
        summary = rows(
            "SELECT COUNT(*) AS n, COALESCE(SUM("
            + lengths
            + "),0) AS bytes FROM "
            + table
        )
        if len(summary) != 1 or summary[0]["bytes"] > limit - used:
            raise ValueError("snapshot exceeds bound; nothing was exported")
        ordering = ",".join(
            columns[:2]
            if table in ("object_chunks", "preservation_chunks")
            else columns[:1]
        )
        values = rows(
            "SELECT " + ",".join(columns) + " FROM " + table + " ORDER BY " + ordering
        )
        for item in values:
            if set(item) != set(columns):
                raise ValueError("unexpected snapshot columns")
            used += len(_bytes(item))
            if used > limit:
                raise ValueError("snapshot exceeds bound; nothing was exported")
        tables[table] = {
            "rows": values,
            "count": len(values),
            "sha256": hashlib.sha256(_bytes(values)).hexdigest(),
        }
    payload = {
        "format": FORMAT,
        "tables": tables,
        "alarm": "NOT_CAPTURED",
        "sessions": "EXCLUDED",
        "backup_history": "INCLUDED",
        "late_writes_after_snapshot": "NOT_CAPTURED",
    }
    if len(_bytes(payload)) > limit:
        raise ValueError("snapshot exceeds bound; nothing was exported")
    validate(payload, limit)
    return payload


def validate(snapshot: Any, limit: int = MAX_BYTES) -> None:
    if (
        type(limit) is not int
        or not 0 < limit <= MAX_BYTES
        or len(_bytes(snapshot)) > limit
    ):
        raise ValueError("invalid snapshot size")
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "format",
        "tables",
        "alarm",
        "sessions",
        "backup_history",
        "late_writes_after_snapshot",
    }:
        raise ValueError("invalid snapshot envelope")
    if (
        snapshot["format"],
        snapshot["alarm"],
        snapshot["sessions"],
        snapshot["backup_history"],
        snapshot["late_writes_after_snapshot"],
    ) != (FORMAT, "NOT_CAPTURED", "EXCLUDED", "INCLUDED", "NOT_CAPTURED"):
        raise ValueError("invalid snapshot scope")
    tables = snapshot["tables"]
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("missing snapshot table")
    for name, columns in TABLES.items():
        table = tables[name]
        if not isinstance(table, dict) or set(table) != {"rows", "count", "sha256"}:
            raise ValueError("invalid table envelope")
        values = table["rows"]
        if (
            not isinstance(values, list)
            or type(table["count"]) is not int
            or table["count"] != len(values)
        ):
            raise ValueError("invalid table count")
        if any(
            not isinstance(item, dict) or set(item) != set(columns) for item in values
        ):
            raise ValueError("invalid table columns")
        if table["sha256"] != hashlib.sha256(_bytes(values)).hexdigest():
            raise ValueError("invalid table hash")
    checkpoints = tables["checkpoint"]["rows"]
    if len(checkpoints) != 1 or checkpoints[0]["id"] != 1:
        raise ValueError("invalid checkpoint")
    checkpoint = checkpoints[0]
    stream = zlib.decompressobj()
    raw = stream.decompress(
        base64.b64decode(checkpoint["payload"], validate=True), 16 * 1024 * 1024 + 1
    )
    if (
        len(raw) > 16 * 1024 * 1024
        or not stream.eof
        or stream.unused_data
        or stream.unconsumed_tail
        or hashlib.sha256(raw).hexdigest() != checkpoint["sha256"]
    ):
        raise ValueError("checkpoint hash or size invalid")
    objects = {item["sha256"]: item["size"] for item in tables["objects"]["rows"]}
    if len(objects) != len(tables["objects"]["rows"]):
        raise ValueError("duplicate media hash")
    parts: dict[str, list[dict[str, Any]]] = {key: [] for key in objects}
    for part in tables["object_chunks"]["rows"]:
        if part["sha256"] not in parts:
            raise ValueError("orphan media chunk")
        parts[part["sha256"]].append(part)
    for key, chunks in parts.items():
        if [chunk["position"] for chunk in chunks] != list(range(len(chunks))):
            raise ValueError("missing media chunk")
        media = b"".join(
            base64.b64decode(chunk["content"], validate=True) for chunk in chunks
        )
        if len(media) != objects[key] or hashlib.sha256(media).hexdigest() != key:
            raise ValueError("media digest or size invalid")
    try:
        # A serialized WAL-mode SQLite file may not open after deserialize()
        # into :memory:. Use a disposable isolated file, never the source DB.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.sqlite3"
            path.write_bytes(raw)
            db = sqlite3.connect(path)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (10, 11):
                    raise ValueError("unsupported checkpoint schema")
                from creator_loop.backend.runtime.database import (
                    validate as validate_domain,
                )

                validate_domain(db, expected_version=version)
                for key, sha, size in db.execute(
                    "SELECT storage_key,sha256,byte_size FROM asset_files WHERE role='ORIGINAL'"
                ):
                    if key != "storage/originals/" + sha or objects.get(sha) != size:
                        raise ValueError("checkpoint references missing media")
            finally:
                db.close()
    except (sqlite3.Error, RuntimeError) as exc:
        raise ValueError("invalid checkpoint database") from exc
    for job in tables["jobs"]["rows"]:
        record = json.loads(job["record"])
        if (
            not isinstance(record, dict)
            or record.get("id") != job["id"]
            or record.get("status") != job["status"]
        ):
            raise ValueError("job identity or status mismatch")
    archives = {}
    for item in tables["preservation_archives"]["rows"]:
        record = json.loads(item["record"])
        if (
            not isinstance(record, dict)
            or record.get("sha256") != item["sha256"]
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or record.get("status") not in ("STAGING", "VERIFIED")
            or type(record.get("bytes")) is not int
            or not 0 < record["bytes"] <= 64 * 1024 * 1024
            or not isinstance(record.get("source_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", record["source_commit"])
        ):
            raise ValueError("invalid preservation archive")
        hashes = record.get("chunk_hashes")
        if (
            not isinstance(hashes, list)
            or len(hashes) != (record["bytes"] + 524287) // 524288
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in hashes
            )
        ):
            raise ValueError("invalid preservation manifest")
        if item["sha256"] in archives:
            raise ValueError("duplicate preservation archive")
        archives[item["sha256"]] = (record, {})
    for item in tables["preservation_chunks"]["rows"]:
        if item["sha256"] not in archives:
            raise ValueError("orphan preservation chunk")
        record, chunks = archives[item["sha256"]]
        position = item["position"]
        if (
            type(position) is not int
            or not 0 <= position < len(record["chunk_hashes"])
            or position in chunks
        ):
            raise ValueError("invalid preservation position")
        content = base64.b64decode(item["content"], validate=True)
        if (
            len(content) != min(524288, record["bytes"] - position * 524288)
            or hashlib.sha256(content).hexdigest() != record["chunk_hashes"][position]
        ):
            raise ValueError("preservation chunk mismatch")
        chunks[position] = content
    for key, (record, chunks) in archives.items():
        if record["status"] == "VERIFIED":
            if len(chunks) != len(record["chunk_hashes"]):
                raise ValueError("verified preservation archive incomplete")
            sha = hashlib.sha256()
            for index in range(len(chunks)):
                sha.update(chunks[index])
            if sha.hexdigest() != key:
                raise ValueError("preservation archive digest mismatch")


def restore_empty(
    snapshot: Any,
    rows: Callable[..., list[dict[str, Any]]],
    execute: Callable[..., Any],
) -> None:
    """Restore into an isolated, empty coordination database in one transaction.

    Caller must enforce isolation, observation-only runtime and transaction
    rollback; this function does not restore alarms or validate grant semantics.
    """
    validate(snapshot)
    for table in TABLES:
        if rows("SELECT 1 FROM " + table + " LIMIT 1"):
            raise ValueError("target is not empty")
    for table, columns in TABLES.items():
        statement = (
            "INSERT INTO "
            + table
            + " ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")"
        )
        for item in snapshot["tables"][table]["rows"]:
            execute(statement, *(item[column] for column in columns))
