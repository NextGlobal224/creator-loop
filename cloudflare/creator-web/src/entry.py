"""Private B Web: one Worker, durable serialized SQLite, immutable media objects."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import time
import zipfile
import zlib
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from creator_loop.backend.analysis.generation_contract import response_schema
from creator_loop.backend.analysis.generation_prompt import generation_prompt
from creator_loop.backend.analysis.web_analysis_audit import validate_analysis_audit
from creator_loop.backend.creator.web_domain import (
    WebDomain,
    WebInputError,
    request_from_json,
)
from creator_loop.backend.knowledge.cloud_audio import (
    WHISPER_MODEL,
    probe_pcm_wav,
    store_cloud_transcript,
)
from creator_loop.backend.knowledge.cloud_image import (
    IMAGE_MODEL,
    probe_image,
    store_cloud_image,
)
from creator_loop.backend.runtime.database import (
    MIGRATIONS,
    _connect_write,
    _migrate_locked,
    initialize,
    validate,
)
from workers import (
    DurableObject,
    Response,
    WorkerEntrypoint,
    import_from_javascript,
    python_to_rpc,
)

MAX_BODY = 16 * 1024 * 1024
MAX_MEDIA_TOTAL = 10 * 1024 * 1024
MAX_MEDIA_OBJECTS = 512
PRESERVATION_CHUNK = 512 * 1024
PRESERVATION_MAX = 64 * 1024 * 1024
PRESERVATION_RESTORE_MAX = 88 * 1024 * 1024
VERSION = "B-web/0.2.0-beta.1"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def valid_included_grant(data, model, now):
    """Paid is not unlimited: retain a bounded included-only, fresh reservation."""
    return (
        isinstance(data, dict)
        and data.get("grant") == "B-finish-20261017-free"
        and data.get("model") == model
        and data.get("account_plan") in ("Free", "Paid")
        and (
            data["account_plan"] == "Free"
            or (
                data.get("included_only") is True
                and type(data.get("max_extra_charge_usd")) in (int, float)
                and data["max_extra_charge_usd"] == 0
            )
        )
        and all(
            type(data.get(k)) in (int, float)
            for k in (
                "observed_at",
                "expires_at",
                "remaining_neurons",
                "estimated_chain_neurons",
            )
        )
        and 0 <= now - data["observed_at"] <= 120
        and int(now // 86400) == int(data["observed_at"] // 86400)
        and now < data["expires_at"] <= now + 300
        and type(data.get("remaining_calls")) is int
        and 0 <= data["remaining_calls"] <= 4
        and 5000
        <= data["estimated_chain_neurons"]
        <= data["remaining_neurons"]
        <= 10000
    )


def unpack_database(encoded):
    packed = base64.b64decode(encoded, validate=True)
    stream = zlib.decompressobj()
    raw = stream.decompress(packed, 16 * 1024 * 1024 + 1)
    if (
        len(raw) > 16 * 1024 * 1024
        or not stream.eof
        or stream.unused_data
        or stream.unconsumed_tail
    ):
        raise ValueError("Unsupported/oversized SQLite checkpoint")
    return raw


def reply(value, status=200, headers=None):
    return Response(
        json.dumps(value, ensure_ascii=False),
        status=status,
        headers={
            **HEADERS,
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
    )


class PreservationReader:
    """Seekable immutable archive, one bounded durable chunk cached at a time."""

    def __init__(self, record, read_chunk):
        self.record, self.read_chunk = record, read_chunk
        self.position, self.cached_position, self.cached = 0, -1, b""

    def tell(self):
        return self.position

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        position = offset + {0: 0, 1: self.position, 2: self.record["bytes"]}[whence]
        if not 0 <= position <= self.record["bytes"]:
            raise ValueError("Invalid archive seek")
        self.position = position
        return position

    def read(self, size=-1):
        size = min(
            self.record["bytes"] - self.position,
            size if size >= 0 else self.record["bytes"],
        )
        if size > 4 * 1024 * 1024:
            raise WebInputError(
                "Archive metadata read exceeds cloud memory budget", 413
            )
        output = []
        while size:
            index, offset = divmod(self.position, PRESERVATION_CHUNK)
            if index != self.cached_position:
                raw = self.read_chunk(index)
                expected_size = min(
                    PRESERVATION_CHUNK,
                    self.record["bytes"] - index * PRESERVATION_CHUNK,
                )
                if (
                    len(raw) != expected_size
                    or digest(raw) != self.record["chunk_hashes"][index]
                ):
                    raise ValueError("Durable archive chunk differs")
                self.cached_position, self.cached = index, raw
            part = self.cached[offset : offset + size]
            if not part:
                raise ValueError("Incomplete durable archive")
            output.append(part)
            self.position += len(part)
            size -= len(part)
        return b"".join(output)


def preservation_restore_preflight(reader):
    """Bound expanded files plus locator copies, leaving 40 MiB runtime headroom.

    This is a capacity refusal, not a license to omit canonical data. The desktop
    archive contract remains unchanged; deployed QA must still verify the real
    archive fits this isolate, or a different bounded restore method is needed.
    """
    with zipfile.ZipFile(reader) as archive:
        entries = archive.infolist()
        if (
            len(entries) > 5000
            or sum(i.file_size for i in entries) > PRESERVATION_RESTORE_MAX
        ):
            raise WebInputError(
                "Archive restore exceeds cloud memory budget; original data/upload preserved",
                413,
            )
        manifest_entry = archive.getinfo("preservation.json")
        if manifest_entry.file_size > 2 * 1024 * 1024:
            raise WebInputError(
                "Archive manifest exceeds cloud verification budget", 413
            )
        manifest = json.loads(archive.read(manifest_entry))
        locators = {}
        for item in manifest["media"] + manifest.get("render_media", []):
            key, size = item["storage_key"], item["bytes"]
            if not isinstance(key, str) or type(size) is not int or size < 0:
                raise ValueError("Invalid preserved media size")
            if key in locators and locators[key] != size:
                raise ValueError("Conflicting preserved media locator")
            locators[key] = size
        projected = sum(i.file_size for i in entries) + sum(locators.values())
        if projected > PRESERVATION_RESTORE_MAX:
            raise WebInputError(
                "Archive plus restored media exceeds cloud memory budget; original data/upload preserved",
                413,
            )
    reader.seek(0)
    return projected


class Database(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.sql = ctx.storage.sql
        # Coordination rows are not an alternative domain history/schema.
        for statement in (
            "CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS objects (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS object_chunks (sha256 TEXT NOT NULL, position INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY(sha256,position))",
            "CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS attempts (ip TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS control (id TEXT PRIMARY KEY, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, created_at REAL NOT NULL, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS preservation_archives (sha256 TEXT PRIMARY KEY, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS preservation_chunks (sha256 TEXT NOT NULL, position INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY(sha256,position))",
        ):
            self.sql.exec(statement)

        ctx.blockConcurrencyWhile(self.recover_wakeup)

    async def recover_wakeup(self):
        pending = self.rows(
            "SELECT record,status FROM jobs WHERE status IN ('QUEUED','RUNNING') ORDER BY rowid LIMIT 1"
        )
        if pending and await self.ctx.storage.getAlarm() is None:
            record = json.loads(pending[0]["record"])
            at = (
                time.time() + 0.1
                if pending[0]["status"] == "QUEUED"
                else max(time.time() + 0.1, record.get("lease_until", time.time()))
            )
            await self.ctx.storage.setAlarm(int(at * 1000))

    def rows(self, statement, *args):
        value = self.sql.exec(statement, *args).toArray()
        return value.to_py() if hasattr(value, "to_py") else value

    def load(self):
        baseline = Path("/tmp/creator-web-baseline")
        baseline.mkdir(exist_ok=True)
        migrations = baseline / "migrations"
        migrations.mkdir(exist_ok=True)
        for name in MIGRATIONS:
            dest = migrations / (name + ".sql")
            if not dest.exists():
                dest.write_text(
                    import_from_javascript("migrations/" + name + ".sql").default,
                    "utf-8",
                )
        sys._MEIPASS = str(baseline)
        # Fresh projection each time: a rejected/unacknowledged operation cannot
        # leak mutated filesystem state into the next request.
        root = Path("/tmp") / ("creator-web-" + uuid4().hex)
        root.mkdir(parents=True)
        path = root / "creator_loop.sqlite3"
        checkpoint = self.rows("SELECT * FROM checkpoint WHERE id=1")
        revision = checkpoint[0]["revision"] if checkpoint else 0
        if checkpoint:
            raw = unpack_database(checkpoint[0]["payload"])
            if digest(raw) != checkpoint[0]["sha256"] or len(raw) > 16 * 1024 * 1024:
                raise RuntimeError("Invalid durable SQLite checkpoint")
            path.write_bytes(raw)
        else:
            initialize(path)
        with closing(_connect_write(path)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (10, 11):
                raise ValueError("Unsupported Web checkpoint schema")
            validate(db, expected_version=version)
            originals = db.execute(
                "SELECT storage_key,sha256,byte_size FROM asset_files WHERE role='ORIGINAL'"
            ).fetchall()
        for key, expected, size in originals:
            if key != "storage/originals/" + expected:
                raise ValueError(
                    "Registered/local storage requires explicit migration; never remap"
                )
            found = self.rows("SELECT size FROM objects WHERE sha256=?", expected)
            if len(found) != 1 or found[0]["size"] != size:
                raise RuntimeError("Durable media is missing")
            raw = self.object_bytes(expected)
            if len(raw) != size or digest(raw) != expected:
                raise RuntimeError("Durable media digest mismatch")
            dest = root / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        return root, revision

    def object_bytes(self, expected):
        chunks = self.rows(
            "SELECT position,content FROM object_chunks WHERE sha256=? ORDER BY position",
            expected,
        )
        if [r["position"] for r in chunks] != list(range(len(chunks))):
            raise RuntimeError("Missing immutable object chunk")
        return b"".join(base64.b64decode(r["content"], validate=True) for r in chunks)

    def put_object(self, expected, raw):
        if digest(raw) != expected:
            raise ValueError("Object digest mismatch")
        if self.rows("SELECT 1 FROM objects WHERE sha256=?", expected):
            if self.object_bytes(expected) != raw:
                raise ValueError("Immutable object replacement refused")
            return
        usage = self.rows(
            "SELECT COALESCE(SUM(size),0) AS size,COUNT(*) AS count FROM objects"
        )[0]
        total = usage["size"]
        if usage["count"] >= MAX_MEDIA_OBJECTS:
            raise WebInputError(
                "Kho media beta đã đạt 512 đối tượng; xuất backup, không tự xóa dữ liệu."
            )
        if total + len(raw) > MAX_MEDIA_TOTAL:
            raise WebInputError(
                "Kho media beta đã đạt giới hạn tổng 10 MiB. Xuất backup; không tự xóa dữ liệu hoặc nâng gói."
            )
        self.sql.exec("INSERT INTO objects VALUES(?,?)", expected, len(raw))
        for position, offset in enumerate(range(0, len(raw), 48000)):
            self.sql.exec(
                "INSERT INTO object_chunks VALUES(?,?,?)",
                expected,
                position,
                base64.b64encode(raw[offset : offset + 48000]).decode(),
            )

    def discard_projection(self, root):
        # Only this invocation's fresh direct /tmp child; never a user root.
        if root.parent != Path("/tmp") or not root.name.startswith("creator-web-"):
            raise RuntimeError("Invalid disposable projection")
        for p in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if p.is_symlink():
                raise RuntimeError("Unexpected projection symlink")
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        root.rmdir()

    def commit(self, root, revision):
        with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
            validate(db)
            # serialize includes committed WAL pages; copying .sqlite3 is unsafe.
            raw = db.serialize()
        compressed = base64.b64encode(zlib.compress(raw)).decode()
        if len(raw) > 16 * 1024 * 1024 or len(compressed) > 1800000:
            raise WebInputError(
                "DB beta đã đạt giới hạn lưu trữ. Xuất backup; chưa xác nhận lưu thay đổi."
            )
        changed = self.rows(
            "INSERT INTO checkpoint VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,sha256=excluded.sha256 WHERE checkpoint.revision=? RETURNING revision",
            revision + 1,
            compressed,
            digest(raw),
            revision,
        )
        if len(changed) != 1:
            raise RuntimeError("Concurrent durable revision changed")

    def import_preserved_b(self, root, revision, data):
        """Operator append, guarded by exact preimage backup/revision and receipts.

        Source lineage is operator supplied and must be independently reconciled
        against the verified cloud archive. Hash syntax is not archive proof.
        No existing row is replaced; this method cannot grant AI or publication.
        """
        from creator_loop.backend.runtime.cloud_import import append_preserved_b_rows

        if (
            set(data)
            != {
                "format",
                "expected_revision",
                "expected_checkpoint_sha256",
                "backup_id",
                "source_archive_sha256",
                "source_database_sha256",
                "source_schema_version",
                "source_cloud_receipt_sha256",
                "source_rows_sha256",
                "tables",
                "objects",
            }
            or data["format"] != "B_CLOUD_IMPORT_V1"
        ):
            raise WebInputError("Gói migration B không tương thích; chưa nhập dữ liệu.")
        for field in (
            "expected_checkpoint_sha256",
            "source_archive_sha256",
            "source_database_sha256",
            "source_cloud_receipt_sha256",
            "source_rows_sha256",
        ):
            if not isinstance(data[field], str) or not re.fullmatch(
                "[a-f0-9]{64}", data[field]
            ):
                raise WebInputError("Thiếu định danh manifest migration B.")
        if type(data["source_schema_version"]) is not int or data[
            "source_schema_version"
        ] not in (9, 10):
            raise WebInputError("Migration chỉ hỗ trợ schema nguồn 9 hoặc 10.")
        checkpoint = self.rows("SELECT * FROM checkpoint WHERE id=1")
        if (
            type(data["expected_revision"]) is not int
            or data["expected_revision"] != revision
            or len(checkpoint) != 1
            or checkpoint[0]["revision"] != revision
            or checkpoint[0]["sha256"] != data["expected_checkpoint_sha256"]
        ):
            raise WebInputError(
                "Revision cloud đã đổi; đọc lại và đối chiếu trước khi nhập.", 409
            )
        if self.rows("SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"):
            raise WebInputError("Chờ tác vụ cloud kết thúc trước migration.", 409)
        if not isinstance(data["backup_id"], str) or not re.fullmatch(
            "[a-f0-9]{32}", data["backup_id"]
        ):
            raise WebInputError("Cần backup cloud đúng revision trước migration.")
        backups = self.rows("SELECT record FROM backups WHERE id=?", data["backup_id"])
        if not backups:
            raise WebInputError("Chưa có backup cloud được chỉ định.", 409)
        backup = json.loads(backups[0]["record"])
        if backup.get("checkpoint") != checkpoint:
            raise WebInputError(
                "Backup không khớp checkpoint hiện tại; chưa nhập dữ liệu.", 409
            )

        tables = data["tables"]
        if (
            not isinstance(tables, dict)
            or digest(
                json.dumps(
                    tables,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            )
            != data["source_rows_sha256"]
        ):
            raise WebInputError("Hash tập hàng nguồn không khớp manifest migration.")
        tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}
        expected = {}
        original_hashes = set()
        locators = []
        for row in tables.get("asset_files", []):
            sha, size = row.get("sha256"), row.get("byte_size")
            if (
                not isinstance(sha, str)
                or not re.fullmatch("[a-f0-9]{64}", sha)
                or type(size) is not int
                or not 0 < size <= 8 * 1024 * 1024
                or sha in expected
                and expected[sha] != size
            ):
                raise WebInputError(
                    "Định danh hoặc kích thước media nguồn không hợp lệ."
                )
            expected[sha] = size
            if row.get("role") == "ORIGINAL":
                key = "storage/originals/" + sha
                locators.append(
                    {
                        "file_id": row["file_id"],
                        "source_storage_key": row["storage_key"],
                        "target_storage_key": key,
                        "sha256": sha,
                    }
                )
                row["storage_key"] = key
                original_hashes.add(sha)
        objects = data["objects"]
        if not isinstance(objects, list) or any(
            not isinstance(o, dict) for o in objects
        ):
            raise WebInputError("Thiếu danh sách media migration.")
        hashes = [o.get("sha256") for o in objects]
        if len(hashes) != len(set(hashes)) or set(hashes) != set(expected):
            raise WebInputError(
                "Tập media phải khớp đầy đủ Asset File, không trùng hoặc thiếu."
            )
        for obj in objects:
            if (
                set(obj) != {"sha256", "size", "content_base64"}
                or obj["size"] != expected[obj["sha256"]]
            ):
                raise WebInputError("Manifest media migration không khớp.")
            raw = base64.b64decode(obj["content_base64"], validate=True)
            if len(raw) != obj["size"] or digest(raw) != obj["sha256"]:
                raise WebInputError("Byte media không khớp SHA-256; đã hủy migration.")
            self.put_object(obj["sha256"], raw)
            if obj["sha256"] in original_hashes:
                destination = root / "storage/originals" / obj["sha256"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.read_bytes() != raw:
                    raise WebInputError("Original cloud khác byte migration.", 409)
                if not destination.exists():
                    destination.write_bytes(raw)
        with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
            db.execute("BEGIN IMMEDIATE")
            result = append_preserved_b_rows(db, tables)
            db.commit()
        return {
            "format": "B_CLOUD_IMPORT_RESULT_V1",
            **result,
            "source_archive_sha256": data["source_archive_sha256"],
            "source_database_sha256": data["source_database_sha256"],
            "source_cloud_receipt_sha256": data["source_cloud_receipt_sha256"],
            "source_rows_sha256": data["source_rows_sha256"],
            "source_lineage": "operator manifest reconciled out of band, not inferred from hash syntax",
            "backup_id": data["backup_id"],
            "locator_mapping": locators,
            "historical_render_media_operational": False,
            "historical_render_archive_mapping": [
                {
                    "render_run_id": row["render_run_id"],
                    "source_storage_key": row["storage_key"],
                    "sha256": row["sha256"],
                    "bytes": row["byte_size"],
                    "source_archive_sha256": data["source_archive_sha256"],
                    "archive_member": "media/" + row["sha256"],
                    "availability": "private canonical archive; operator must verify manifest",
                }
                for row in tables.get("draft_render_outputs", [])
            ],
            "existing_rows_replaced": False,
            "runtime_executed": False,
            "publication_approval_created": False,
        }

    def authenticated(self, request):
        cookie = request.headers.get("Cookie") or ""
        values = [
            p.strip()[len("__Host-creator=") :]
            for p in cookie.split(";")
            if p.strip().startswith("__Host-creator=")
        ]
        if len(values) != 1 or len(values[0]) != 64:
            return False
        return bool(
            self.rows(
                "SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                digest(values[0].encode()),
                time.time(),
            )
        )

    def preservation_api(self, path, method, data):
        """Operator-only immutable archive; never replace the active checkpoint."""
        base = "/api/preservation"
        if path == base:
            if method == "GET":
                return reply(
                    {
                        "archives": [
                            json.loads(row["record"])
                            for row in self.rows(
                                "SELECT record FROM preservation_archives ORDER BY sha256"
                            )
                        ],
                        "limit": 1,
                    }
                )
            if set(data) != {"sha256", "bytes", "chunk_hashes", "source_commit"}:
                raise ValueError("Archive identity required")
            key, size, hashes, commit = (
                data[k] for k in ("sha256", "bytes", "chunk_hashes", "source_commit")
            )
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-f0-9]{64}", key)
                or type(size) is not int
                or not 0 < size <= PRESERVATION_MAX
                or not isinstance(hashes, list)
                or len(hashes) != (size + PRESERVATION_CHUNK - 1) // PRESERVATION_CHUNK
                or any(
                    not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h)
                    for h in hashes
                )
                or not isinstance(commit, str)
                or not re.fullmatch(r"[a-f0-9]{40}", commit)
            ):
                raise ValueError("Unsupported archive identity/size")

            def begin():
                existing = self.rows(
                    "SELECT record FROM preservation_archives WHERE sha256=?", key
                )
                if existing:
                    record = json.loads(existing[0]["record"])
                    if any(record[k] != v for k, v in data.items()):
                        raise WebInputError(
                            "Archive identity conflicts; preserved existing upload", 409
                        )
                    return record
                if self.rows("SELECT 1 FROM preservation_archives LIMIT 1"):
                    raise WebInputError(
                        "One canonical archive already reserved; no automatic replacement",
                        409,
                    )
                record = {**data, "status": "STAGING", "created_at": time.time()}
                self.sql.exec(
                    "INSERT INTO preservation_archives VALUES(?,?)",
                    key,
                    json.dumps(record),
                )
                return record

            return reply(self.ctx.storage.transactionSync(begin))
        pieces = path[len(base) + 1 :].split("/")
        key = pieces[0]
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid archive key")
        found = self.rows(
            "SELECT record FROM preservation_archives WHERE sha256=?", key
        )
        if not found:
            return reply({"error": "Archive not found"}, 404)
        record = json.loads(found[0]["record"])
        if len(pieces) == 1 and method == "GET":
            return reply(
                {
                    **record,
                    "uploaded_positions": [
                        r["position"]
                        for r in self.rows(
                            "SELECT position FROM preservation_chunks WHERE sha256=? ORDER BY position",
                            key,
                        )
                    ],
                }
            )
        if (
            len(pieces) == 3
            and pieces[1] == "chunks"
            and re.fullmatch(r"0|[1-9][0-9]{0,2}", pieces[2])
        ):
            index = int(pieces[2])
            if index >= len(record["chunk_hashes"]):
                raise ValueError("Chunk position outside archive")
            if method == "GET":
                return reply(
                    {
                        "position": index,
                        "sha256": record["chunk_hashes"][index],
                        "content_base64": base64.b64encode(
                            self.preservation_chunk(key, index)
                        ).decode(),
                    }
                )
            if (
                set(data) != {"content_base64"}
                or not isinstance(data["content_base64"], str)
                or len(data["content_base64"]) > 4 * ((PRESERVATION_CHUNK + 2) // 3)
            ):
                raise ValueError("Bounded chunk required")
            raw = base64.b64decode(data["content_base64"], validate=True)
            if (
                len(raw)
                != min(PRESERVATION_CHUNK, record["bytes"] - index * PRESERVATION_CHUNK)
                or digest(raw) != record["chunk_hashes"][index]
            ):
                raise ValueError("Chunk bytes differ")

            def retain():
                existing = self.rows(
                    "SELECT content FROM preservation_chunks WHERE sha256=? AND position=?",
                    key,
                    index,
                )
                if existing:
                    if base64.b64decode(existing[0]["content"], validate=True) != raw:
                        raise ValueError("Immutable chunk replacement refused")
                elif record["status"] != "STAGING":
                    raise ValueError("Completed archive cannot accept new chunks")
                else:
                    self.sql.exec(
                        "INSERT INTO preservation_chunks VALUES(?,?,?)",
                        key,
                        index,
                        base64.b64encode(raw).decode(),
                    )
                return {"position": index, "stored": True, "sha256": digest(raw)}

            return reply(self.ctx.storage.transactionSync(retain))
        if len(pieces) == 2 and pieces[1] == "verify" and method == "POST" and not data:
            if record["status"] == "VERIFIED":
                return reply(record)
            reader = PreservationReader(
                record, lambda index: self.preservation_chunk(key, index)
            )
            checksum = hashlib.sha256()
            while chunk := reader.read(PRESERVATION_CHUNK):
                checksum.update(chunk)
            if checksum.hexdigest() != key:
                raise ValueError("Whole archive checksum differs")
            reader.seek(0)
            projected_restore_bytes = preservation_restore_preflight(reader)
            from creator_loop.backend.runtime.migration_preservation import (
                verify_preservation_archive,
            )

            isolated = Path("/tmp") / ("creator-web-preservation-" + uuid4().hex)
            try:
                proof = verify_preservation_archive(reader, isolated)
            finally:
                if isolated.exists():
                    self.discard_projection(isolated)
            record.update(
                status="VERIFIED",
                verified_at=time.time(),
                deployment=str(self.env.CREATOR_VERSION.id),
                projected_restore_bytes=projected_restore_bytes,
                proof={
                    **proof,
                    "cloud_restore_verified": True,
                    "permits_migration_or_cleanup": False,
                },
                verification_scope="isolated canonical archive reopen, not operational migration or cleanup",
            )
            self.sql.exec(
                "UPDATE preservation_archives SET record=? WHERE sha256=?",
                json.dumps(record),
                key,
            )
            return reply(record)
        return reply({"error": "Unsupported archive operation"}, 404)

    def preservation_chunk(self, key, index):
        found = self.rows(
            "SELECT content FROM preservation_chunks WHERE sha256=? AND position=?",
            key,
            index,
        )
        if len(found) != 1:
            raise WebInputError(
                "Archive incomplete; resume missing chunk before verify", 409
            )
        return base64.b64decode(found[0]["content"], validate=True)

    async def fetch(self, request):
        url = urlsplit(request.url)
        path, method = url.path, request.method
        if method not in ("GET", "POST"):
            return reply({"error": "Method refused"}, 405)
        if method == "POST":
            if (
                request.headers.get("Origin") != url.scheme + "://" + url.netloc
                or request.headers.get("Content-Type") != "application/json"
            ):
                return reply({"error": "Same-origin JSON required"}, 403)
            length = request.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_BODY):
                return reply({"error": "Body too large"}, 413)
            text = await request.text()
            if len(text.encode()) > MAX_BODY:
                return reply({"error": "Body too large"}, 413)
            try:
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                return reply({"error": "JSON object required"}, 400)
        else:
            data = {}
        if path == "/api/login" and method == "POST":
            ip = digest((request.headers.get("CF-Connecting-IP") or "local").encode())
            observed = self.rows("SELECT * FROM attempts WHERE ip=?", ip)
            if (
                observed
                and observed[0]["started"] > time.time() - 900
                and observed[0]["count"] >= 10
            ):
                return reply({"error": "Thử lại sau 15 phút"}, 429)
            self.sql.exec(
                "INSERT INTO attempts VALUES(?,?,1) ON CONFLICT(ip) DO UPDATE SET count=CASE WHEN started<? THEN 1 ELSE count+1 END,started=CASE WHEN started<? THEN excluded.started ELSE started END",
                ip,
                time.time(),
                time.time() - 900,
                time.time() - 900,
            )
            password = data.get("password", "")
            expected = getattr(self.env, "LOGIN_PASSWORD_HASH", "")
            if (
                not isinstance(password, str)
                or len(password) > 200
                or len(expected) != 64
                or not hmac.compare_digest(digest(password.encode()), expected)
            ):
                return reply({"error": "Thông tin đăng nhập không hợp lệ"}, 401)
            session = secrets.token_hex(32)
            self.sql.exec("DELETE FROM sessions WHERE expires<=?", time.time())
            self.sql.exec(
                "INSERT INTO sessions VALUES(?,?)",
                digest(session.encode()),
                time.time() + 43200,
            )
            return reply(
                {"ok": True},
                headers={
                    "Set-Cookie": f"__Host-creator={session}; Path=/; Max-Age=43200; Secure; HttpOnly; SameSite=Strict"
                },
            )
        if not self.authenticated(request):
            if (
                path in ("/login", "/login.html", "/login.js", "/style.css")
                and method == "GET"
            ):
                return await self.env.ASSETS.fetch(request)
            if path.startswith("/api/") or path.startswith("/media/"):
                return reply({"error": "Đăng nhập để mở dữ liệu riêng"}, 401)
            return Response("", status=303, headers={**HEADERS, "Location": "/login"})
        try:
            return await self.authorized(request, path, method, data)
        except WebInputError as error:
            return reply({"error": str(error)}, error.status)
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            return reply(
                {
                    "error": "Yêu cầu không hợp lệ hoặc nguồn/phiên bản đã thay đổi; không có Draft mới được xác nhận"
                },
                400,
            )
        except Exception:
            # No private data/credential/exception text in logs or public errors.
            return reply(
                {
                    "error": "Không thể xác nhận thao tác. Giữ request ID và kiểm tra trạng thái trước khi thử lại."
                },
                503,
            )

    async def authorized(self, request, path, method, data):
        if path.startswith("/api/restore-receipts/"):
            secret = getattr(self.env, "OPERATOR_KEY", "")
            if not secret or not hmac.compare_digest(
                request.headers.get("X-Operator-Key") or "", secret
            ):
                return reply({"error": "Operator verification required"}, 403)
            key = path.removeprefix("/api/restore-receipts/")
            if method != "GET" or not re.fullmatch("[a-f0-9]{32}", key):
                return reply({"error": "Invalid restore receipt request"}, 400)
            prior = self.rows("SELECT response FROM receipts WHERE id=?", key)
            preimage = self.rows(
                "SELECT 1 FROM control WHERE id=?", "pre-restore-" + key
            )
            if not prior or not preimage:
                return reply({"recorded": False, "request_id": key}, 404)
            return reply(
                {
                    "recorded": True,
                    "request_id": key,
                    "receipt": json.loads(prior[0]["response"]),
                }
            )
        if path in ("/api/import", "/api/schema-migrate") or path.startswith(
            ("/api/imports/", "/api/schema-migrations/")
        ):
            secret = getattr(self.env, "OPERATOR_KEY", "")
            if not secret or not hmac.compare_digest(
                request.headers.get("X-Operator-Key") or "", secret
            ):
                return reply({"error": "Operator verification required"}, 403)
            if path.startswith("/api/imports/"):
                key = path.removeprefix("/api/imports/")
                if method != "GET" or not re.fullmatch("[a-f0-9]{32}", key):
                    return reply({"error": "Invalid migration receipt request"}, 400)
                found = self.rows("SELECT response FROM receipts WHERE id=?", key)
                receipt = json.loads(found[0]["response"]) if found else {}
                if receipt.get("format") != "B_CLOUD_IMPORT_RESULT_V1":
                    return reply(
                        {
                            "error": "Migration not recorded; inspect the same request before resubmitting"
                        },
                        404,
                    )
                if receipt.get("restored_away"):
                    return reply(
                        {"error": "Migration belongs to a state before restore"}, 409
                    )
                return reply(receipt)
        if path == "/api/preservation" or path.startswith("/api/preservation/"):
            secret = getattr(self.env, "OPERATOR_KEY", "")
            if not secret or not hmac.compare_digest(
                request.headers.get("X-Operator-Key") or "", secret
            ):
                return reply({"error": "Operator verification required"}, 403)
            return self.preservation_api(path, method, data)
        if path == "/api/source-grant-status" and method == "GET":
            media_type = parse_qs(urlsplit(request.url).query).get(
                "media_type", ["AUDIO"]
            )[0]
            if media_type not in ("AUDIO", "IMAGE"):
                raise WebInputError("Chọn nguồn ghi âm hoặc ảnh trước khi kiểm quyền.")
            model = IMAGE_MODEL if media_type == "IMAGE" else WHISPER_MODEL
            found = self.rows("SELECT record FROM control WHERE id='grant'")
            grant = json.loads(found[0]["record"]) if found else {}
            available = (
                valid_included_grant(grant, model, time.time())
                and grant["remaining_calls"] == 1
            )
            return reply(
                {
                    "available": available,
                    "model": model,
                    "message": "Quyền đọc nguồn operator còn hiệu lực; không phải phép đo quota trực tiếp."
                    if available
                    else "Chưa có quyền đọc nguồn operator còn hiệu lực. Cần kiểm quota trong gói và dự phòng trước khi cấp đúng model ghi âm/ảnh; chưa gửi AI.",
                }
            )
        if path == "/api/grant-status" and method == "GET":
            found = self.rows("SELECT record FROM control WHERE id='grant'")
            grant = json.loads(found[0]["record"]) if found else {}
            now = time.time()
            available = (
                valid_included_grant(grant, self.env.CREATOR_MODEL, now)
                and grant["remaining_calls"] == 4
            )
            return reply(
                {
                    "available": available,
                    "valid_until": min(
                        grant.get("expires_at", 0), grant.get("observed_at", 0) + 120
                    )
                    if available
                    else None,
                    "message": "Quyền operator còn hiệu lực; đây không phải phép đo quota trực tiếp."
                    if available
                    else "Chưa có quyền operator còn hiệu lực. Mở Bước 4, kiểm đúng tài khoản/Workers plan và còn ít nhất 5.000 Neurons trong phần bao gồm sau dự phòng. Paid vẫn có thể tính phí vượt mức: chỉ cho phép khi phí ngoài gói $0. Thiếu quota thì không gửi AI.",
                }
            )
        if path == "/api/runtime" and method == "GET":
            sdk = import_from_javascript("creator_ai.mjs")
            return reply(
                {
                    "components": json.loads(sdk.components()),
                    "version": VERSION,
                    "deployment": str(self.env.CREATOR_VERSION.id),
                    "model": self.env.CREATOR_MODEL,
                }
            )
        if path == "/api/logout" and method == "POST":
            self.sql.exec("DELETE FROM sessions")
            return reply(
                {"ok": True},
                headers={
                    "Set-Cookie": "__Host-creator=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
                },
            )
        if path == "/api/grant" and method == "POST":
            secret = getattr(self.env, "OPERATOR_KEY", "")
            if not secret or not hmac.compare_digest(
                request.headers.get("X-Operator-Key") or "", secret
            ):
                return reply({"error": "Operator verification required"}, 403)
            if (
                set(data) - {"included_only", "max_extra_charge_usd"}
                != {
                    "grant",
                    "observed_at",
                    "expires_at",
                    "remaining_calls",
                    "model",
                    "account_plan",
                    "remaining_neurons",
                    "estimated_chain_neurons",
                }
                or data["grant"] != "B-finish-20261017-free"
                or data["account_plan"] not in ("Free", "Paid")
                or data["model"]
                not in (self.env.CREATOR_MODEL, WHISPER_MODEL, IMAGE_MODEL)
                or (
                    data["model"] in (WHISPER_MODEL, IMAGE_MODEL)
                    and data["remaining_calls"] != 1
                )
            ):
                raise WebInputError(
                    "Cần bằng chứng đúng account/Workers plan và phần bao gồm; chưa tạo quyền AI."
                )
            if not valid_included_grant(data, data["model"], time.time()):
                raise WebInputError(
                    "Chưa đủ phần bao gồm, dự phòng 5.000 hoặc bằng chứng đã hết hạn. Paid phải xác nhận phí ngoài gói $0; chưa tạo quyền AI."
                )
            self.sql.exec(
                "INSERT INTO control VALUES('grant',?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                json.dumps(data),
            )
            return reply({"ok": True, "grant": data})
        if path.startswith("/api/jobs/") and method == "GET":
            found = self.rows(
                "SELECT record FROM jobs WHERE id=?", path.rsplit("/", 1)[1]
            )
            return (
                reply(json.loads(found[0]["record"]))
                if found
                else reply({"error": "Job not found"}, 404)
            )
        if path == "/api/cancel" and method == "POST":
            found = self.rows(
                "SELECT record,status FROM jobs WHERE id=?", data.get("job_id", "")
            )
            if not found:
                raise ValueError("Job not found")
            record = json.loads(found[0]["record"])
            if found[0]["status"] in ("QUEUED", "RUNNING"):
                record.update(
                    status="CANCELLED",
                    message="Đã dừng các bước tiếp theo; lời gọi đã gửi có thể vẫn tiêu thụ quota",
                )
                self.sync_run(record)
            return reply(record)
        if path.startswith("/media/") and method == "GET":
            fid = path.rsplit("/", 1)[1]
            root, _ = self.load()
            try:
                with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                    row = db.execute(
                        """SELECT f.storage_key,f.sha256,f.mime_type,a.media_type
                        FROM asset_files f JOIN assets a USING(asset_id)
                        WHERE f.file_id=? AND f.role='ORIGINAL' AND a.deleted_at IS NULL""",
                        (fid,),
                    ).fetchone()
                if not row:
                    return reply({"error": "Media not found"}, 404)
                raw = (root / row[0]).read_bytes()
                if digest(raw) != row[1]:
                    raise ValueError("Media changed")
                mime = row[2]
                verified_pcm = False
                if row[3] == "AUDIO":
                    mime = "application/octet-stream"
                    from creator_loop.backend.knowledge.cloud_audio import probe_pcm_wav

                    try:
                        probe_pcm_wav(raw)
                    except ValueError:
                        pass  # Opaque/unsupported audio remains a download.
                    else:
                        mime = "audio/wav"
                        verified_pcm = True
                headers = {
                    **HEADERS,
                    "Content-Type": mime,
                    "Content-Disposition": "inline"
                    if mime.startswith("image/") or verified_pcm
                    else "attachment",
                }
                if verified_pcm:
                    headers["Accept-Ranges"] = "bytes"
                    headers["Content-Length"] = str(len(raw))
                    value = request.headers.get("Range")
                    if value is not None:
                        match = (
                            re.fullmatch(r"bytes=(\d*)-(\d*)", value)
                            if len(value) <= 128
                            else None
                        )
                        start, end = 0, len(raw) - 1
                        valid = bool(match and any(match.groups()))
                        if valid and match:
                            first, last = match.groups()
                            if first:
                                start = int(first)
                                if last:
                                    end = min(int(last), end)
                            else:
                                suffix = int(last)
                                valid = suffix > 0
                                start = max(0, len(raw) - suffix)
                            valid = valid and start <= end and start < len(raw)
                        if not valid:
                            headers["Content-Range"] = "bytes */" + str(len(raw))
                            headers["Content-Length"] = "0"
                            return Response(b"", status=416, headers=headers)
                        body = raw[start : end + 1]
                        headers["Content-Range"] = f"bytes {start}-{end}/{len(raw)}"
                        headers["Content-Length"] = str(len(body))
                        return Response(body, status=206, headers=headers)
                return Response(raw, headers=headers)
            finally:
                self.discard_projection(root)
        if path == "/api/backup" and method == "GET":
            # A single synchronous revision; media objects are immutable.
            cp = self.rows("SELECT * FROM checkpoint WHERE id=1")
            return reply(
                {
                    "format": "CREATOR_WEB_BACKUP_V1",
                    "version": VERSION,
                    "checkpoint": cp,
                    "objects": [
                        {
                            **o,
                            "content_base64": base64.b64encode(
                                self.object_bytes(o["sha256"])
                            ).decode(),
                        }
                        for o in self.rows("SELECT * FROM objects")
                    ],
                }
            )
        if (
            path.startswith("/api/schema-migrations/")
            and path.endswith("/backup")
            and method == "GET"
        ):
            migration_id = path.split("/")[-2]
            found = self.rows(
                "SELECT record FROM control WHERE id=?", "pre-schema11-" + migration_id
            )
            if not found:
                return reply({"error": "Không tìm thấy preimage migration"}, 404)
            backup = json.loads(found[0]["record"])
            if (
                not isinstance(backup, dict)
                or backup.get("format") != "CREATOR_WEB_BACKUP_V1"
            ):
                return reply({"error": "Preimage cũ chưa có manifest media"}, 409)
            for obj in backup["objects"]:
                raw = self.object_bytes(obj["sha256"])
                if len(raw) != obj["size"] or digest(raw) != obj["sha256"]:
                    raise WebInputError(
                        "Media preimage không khớp; chưa tạo gói recovery"
                    )
                obj["content_base64"] = base64.b64encode(raw).decode()
            return reply(backup)
        if path == "/api/backups" and method == "GET":
            return reply(
                {
                    "backups": [
                        {
                            "id": row["id"],
                            "created_at": row["created_at"],
                            "revision": json.loads(row["record"])["checkpoint"][0][
                                "revision"
                            ],
                        }
                        for row in self.rows(
                            "SELECT * FROM backups ORDER BY created_at DESC"
                        )
                    ],
                    "limit": 3,
                }
            )
        if path.startswith("/api/backups/") and method == "GET":
            found = self.rows(
                "SELECT record FROM backups WHERE id=?", path.rsplit("/", 1)[1]
            )
            if not found:
                return reply({"error": "Không tìm thấy backup cloud"}, 404)
            backup = json.loads(found[0]["record"])
            for obj in backup["objects"]:
                obj["content_base64"] = base64.b64encode(
                    self.object_bytes(obj["sha256"])
                ).decode()
            return reply(backup)
        if path == "/api/backups" and method == "POST":
            key = request.headers.get("Idempotency-Key") or ""
            if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                raise WebInputError("Thiếu ID thao tác backup hợp lệ")

            def snapshot():
                prior = self.rows("SELECT record FROM backups WHERE id=?", key)
                if prior:
                    saved = json.loads(prior[0]["record"])
                    if saved["checkpoint"][0]["revision"] != data.get(
                        "expected_revision"
                    ):
                        raise WebInputError("ID backup đã dùng cho revision khác", 409)
                    return {"id": key, "saved_on_cloud": True}
                cp = self.rows("SELECT * FROM checkpoint WHERE id=1")
                if (
                    not cp
                    or type(data.get("expected_revision")) is not int
                    or cp[0]["revision"] != data["expected_revision"]
                ):
                    raise WebInputError(
                        "Dữ liệu đã đổi; tải lại trước khi lưu backup cloud", 409
                    )
                if self.rows("SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING')"):
                    raise WebInputError("Chờ tác vụ kết thúc trước khi backup", 409)
                if len(self.rows("SELECT id FROM backups")) >= 3:
                    raise WebInputError(
                        "Đã giữ 3 backup cloud; không tự xóa hoặc tăng giới hạn", 409
                    )
                objects = self.rows("SELECT * FROM objects")
                # Immutable media are retained in this DO; snapshot pins their exact
                # hashes without duplicating bytes. This is not an off-account copy.
                for obj in objects:
                    raw = self.object_bytes(obj["sha256"])
                    if len(raw) != obj["size"] or digest(raw) != obj["sha256"]:
                        raise WebInputError("Media không khớp; chưa lưu backup")
                record = {
                    "format": "CREATOR_WEB_BACKUP_V1",
                    "version": VERSION,
                    "checkpoint": cp,
                    "objects": objects,
                }
                self.sql.exec(
                    "INSERT INTO backups VALUES(?,?,?)",
                    key,
                    time.time(),
                    json.dumps(record),
                )
                return {"id": key, "saved_on_cloud": True}

            return reply(self.ctx.storage.transactionSync(snapshot))
        if not path.startswith("/api/") and method == "GET":
            return await self.env.ASSETS.fetch(request)
        root, revision = self.load()
        try:
            domain = WebDomain(root)
            if method == "GET":
                if path == "/api/source-review":
                    query = parse_qs(urlsplit(request.url).query)
                    review = domain.source_review(
                        query.get("project_id", [""])[0],
                        query.get("file_id", [""])[0],
                    )
                    artifact = review.pop("latest_transcript_artifact", None)
                    if artifact:
                        raw = self.object_bytes(artifact["sha256"])
                        if (
                            len(raw) != artifact["byte_size"]
                            or len(raw) > 256 * 1024
                            or digest(raw) != artifact["sha256"]
                        ):
                            raise WebInputError(
                                "Bằng chứng transcript không khớp; giữ bản gốc để đối chiếu."
                            )
                        content = json.loads(raw)
                        if (
                            content.get("format")
                            != (
                                "CREATOR_CLOUD_IMAGE_V1"
                                if review["media_type"] == "IMAGE"
                                else "CREATOR_CLOUD_TRANSCRIPT_V1"
                            )
                            or content.get("model")
                            != (
                                IMAGE_MODEL
                                if review["media_type"] == "IMAGE"
                                else WHISPER_MODEL
                            )
                            or content.get("run_id") != artifact["run_id"]
                            or content.get("input_file_id") != review["file_id"]
                            or content.get("input_sha256") != artifact["input_sha256"]
                        ):
                            raise WebInputError(
                                "Transcript không gắn đúng nguồn/run; chưa dùng kết quả."
                            )
                        review["source_extraction"] = {
                            "run_id": artifact["run_id"],
                            "model": content["model"],
                            "parsed": content["parsed"],
                            "artifact_sha256": artifact["sha256"],
                        }
                    return reply(review)
                if path == "/api/state":
                    await self.recover_wakeup()
                    project_id = parse_qs(urlsplit(request.url).query).get(
                        "project_id", [None]
                    )[0]
                    return reply(
                        {
                            **domain.state(project_id),
                            "revision": revision,
                            "version": VERSION,
                            "jobs": [
                                json.loads(r["record"])
                                for r in self.rows(
                                    "SELECT record FROM jobs ORDER BY rowid DESC LIMIT 30"
                                )
                            ],
                        }
                    )
                if path.startswith("/api/workflows/"):
                    wid = path.rsplit("/", 1)[1]
                    workflow = domain.pipeline.load(wid)
                    return reply(
                        {
                            **workflow,
                            "versions": [
                                asdict(domain.pipeline.draft(v))
                                for v in workflow["drafts"]
                            ],
                        }
                    )
                return reply({"error": "Not found"}, 404)
            key = request.headers.get("Idempotency-Key") or ""
            if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                raise ValueError("Explicit idempotency key required")
            fingerprint = digest(
                (
                    path + "\n" + json.dumps(data, sort_keys=True, ensure_ascii=False)
                ).encode()
            )
            prior = self.rows("SELECT * FROM receipts WHERE id=?", key)
            if prior:
                if prior[0]["fingerprint"] != fingerprint:
                    return reply(
                        {"error": "Request ID already belongs to different input"}, 409
                    )
                receipt = json.loads(prior[0]["response"])
                if receipt.get("restored_away"):
                    return reply(
                        {
                            "error": "Thao tác thuộc trạng thái trước restore; mở lại dữ liệu hiện hành, không tự gửi lại request cũ"
                        },
                        409,
                    )
                if path == "/api/jobs":
                    await self.recover_wakeup()
                return reply(receipt)

            def mutate():
                with closing(_connect_write(root / "creator_loop.sqlite3")) as check:
                    schema_version = check.execute("PRAGMA user_version").fetchone()[0]
                if schema_version == 10 and path != "/api/schema-migrate":
                    raise WebInputError(
                        "Dữ liệu đang ở bản schema trước. Operator phải lưu backup cloud và nâng schema có kiểm tra trước khi ghi; bản gốc vẫn giữ.",
                        409,
                    )
                if path == "/api/schema-migrate":
                    if (
                        schema_version != 10
                        or set(data) - {"backup_id", "backup_mode"}
                        != {
                            "expected_revision",
                            "expected_checkpoint_sha256",
                            "target_schema",
                            "confirm_additive_source_use_migration",
                        }
                        or data["target_schema"] != 11
                        or data["confirm_additive_source_use_migration"] is not True
                        or ("backup_id" in data) == ("backup_mode" in data)
                        or (
                            "backup_mode" in data
                            and data["backup_mode"] != "PIN_CURRENT_PREIMAGE"
                        )
                    ):
                        raise WebInputError(
                            "Chỉ cho phép nâng schema10→11 có xác nhận riêng."
                        )
                    checkpoint = self.rows("SELECT * FROM checkpoint WHERE id=1")
                    saved = (
                        self.rows(
                            "SELECT record FROM backups WHERE id=?", data["backup_id"]
                        )
                        if "backup_id" in data
                        else None
                    )
                    if (
                        len(checkpoint) != 1
                        or data["expected_revision"] != revision
                        or checkpoint[0]["sha256"] != data["expected_checkpoint_sha256"]
                        or (
                            saved is not None
                            and (
                                len(saved) != 1
                                or json.loads(saved[0]["record"])["checkpoint"]
                                != checkpoint
                            )
                        )
                        or self.rows(
                            "SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING')"
                        )
                    ):
                        raise WebInputError(
                            "Cần backup cloud đúng checkpoint và không còn job đang chạy; chưa nâng schema.",
                            409,
                        )
                    objects = self.rows("SELECT * FROM objects")
                    for obj in objects:
                        raw = self.object_bytes(obj["sha256"])
                        if len(raw) != obj["size"] or digest(raw) != obj["sha256"]:
                            raise WebInputError(
                                "Media preimage không khớp; chưa nâng schema"
                            )
                    self.sql.exec(
                        "INSERT INTO control VALUES(?,?)",
                        "pre-schema11-" + key,
                        json.dumps(
                            {
                                "format": "CREATOR_WEB_BACKUP_V1",
                                "version": VERSION,
                                "checkpoint": checkpoint,
                                "objects": objects,
                            }
                        ),
                    )
                    with closing(
                        _connect_write(root / "creator_loop.sqlite3")
                    ) as check:
                        with check:
                            check.execute("BEGIN IMMEDIATE")
                            _migrate_locked(check, 10)
                    result = {
                        "migration": "SOURCE_USE_SCHEMA11",
                        "previous_schema": 10,
                        "schema_version": 11,
                        "backup_id": data.get("backup_id"),
                        "preimage_backup_path": "/api/schema-migrations/"
                        + key
                        + "/backup",
                        "previous_checkpoint_sha256": checkpoint[0]["sha256"],
                        "previous_checkpoint_retained": True,
                        "no_source_selection_granted": True,
                    }
                elif path == "/api/import":
                    result = self.import_preserved_b(root, revision, data)
                elif path == "/api/projects":
                    result = domain.project(data.get("title", ""))
                elif path == "/api/materials":
                    if not data.get("project_id"):
                        raise WebInputError("Chọn Project trước khi lưu tư liệu.")
                    raw = base64.b64decode(data.pop("content_base64"), validate=True)
                    result = domain.intake(data, raw)
                    self.put_object(result["sha256"], raw)
                elif path == "/api/source-jobs":
                    if set(data) != {"project_id", "file_id"}:
                        raise WebInputError("Chọn đúng Project và nguồn ghi âm/ảnh.")
                    if self.rows(
                        "SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                    ):
                        raise WebInputError(
                            "Còn tác vụ đang chạy; mở trạng thái hoặc Dừng trước.", 409
                        )
                    grants = self.rows("SELECT record FROM control WHERE id='grant'")
                    grant = json.loads(grants[0]["record"]) if grants else {}
                    media_type = domain._project_original(
                        data["project_id"], data["file_id"]
                    )["media_type"]
                    if media_type not in ("AUDIO", "IMAGE"):
                        raise WebInputError(
                            "Đọc nguồn hiện hỗ trợ ghi âm và ảnh; nguồn khác cần bộ đọc phù hợp, chưa gửi AI."
                        )
                    model = IMAGE_MODEL if media_type == "IMAGE" else WHISPER_MODEL
                    if (
                        not valid_included_grant(grant, model, time.time())
                        or grant["remaining_calls"] != 1
                    ):
                        raise WebInputError(
                            "Chưa có quyền operator đọc nguồn đúng model còn hiệu lực với quota/dự phòng trong gói. Chưa gửi AI."
                        )
                    source = (
                        domain.begin_source_image(data, key, VERSION)
                        if media_type == "IMAGE"
                        else domain.begin_source_transcription(data, key, VERSION)
                    )
                    grant["remaining_calls"] = 0
                    grant["reserved_pipeline"] = key
                    self.sql.exec(
                        "UPDATE control SET record=? WHERE id='grant'",
                        json.dumps(grant),
                    )
                    result = {
                        "id": key,
                        "kind": "SOURCE_IMAGE_ANALYSIS"
                        if media_type == "IMAGE"
                        else "SOURCE_TRANSCRIPTION",
                        "status": "QUEUED",
                        "stage": 0,
                        "request": data,
                        "sources": [source | {"file_id": data["file_id"]}],
                        "input_fingerprint": fingerprint,
                        "created_at": time.time(),
                        "calls": [],
                        "grant": grant,
                    }
                    self.save_job(result)
                elif path == "/api/material-links":
                    result = domain.link_material(
                        data.get("project_id", ""), data.get("material_id", "")
                    )
                elif path in (
                    "/api/source-ranges",
                    "/api/source-audio-ranges",
                    "/api/source-image-regions",
                    "/api/source-use",
                ):
                    if self.rows(
                        """SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') AND
                        (json_extract(record,'$.request.project_id')=? OR EXISTS(
                          SELECT 1 FROM json_each(jobs.record,'$.sources') source
                          WHERE json_extract(source.value,'$.file_id')=?)) LIMIT 1""",
                        data.get("project_id", ""),
                        data.get("file_id", ""),
                    ):
                        raise WebInputError(
                            "Project hoặc nguồn dùng chung đang có tác vụ. Dừng và chờ kết thúc trước khi sửa/chọn lại nguồn.",
                            409,
                        )
                    if path == "/api/source-ranges":
                        result = domain.source_text_range(data)
                    elif path == "/api/source-audio-ranges":
                        result = domain.source_audio_range(data)
                    elif path == "/api/source-image-regions":
                        result = domain.source_image_region(data)
                    else:
                        result = domain.source_use(data)
                elif path == "/api/restore":
                    if data.get("confirm_data_after_backup_may_be_lost") is not True:
                        raise WebInputError(
                            "Đọc cảnh báo và xác nhận trước khi khôi phục backup."
                        )
                    if data.get("expected_revision") != revision:
                        raise WebInputError(
                            "Dữ liệu cloud đã thay đổi sau lần mở trang. Tải lại, kiểm tra dữ liệu hiện tại rồi chọn và xác nhận backup lại; chưa khôi phục gì.",
                            409,
                        )
                    if self.rows(
                        "SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING')"
                    ):
                        raise WebInputError(
                            "Còn tác vụ AI đang chạy. Kiểm tra/dừng và chờ tác vụ kết thúc trước khi khôi phục.",
                            409,
                        )
                    incoming = data.get("backup", {})
                    if "schema_migration_id" in data:
                        secret = getattr(self.env, "OPERATOR_KEY", "")
                        if not secret or not hmac.compare_digest(
                            request.headers.get("X-Operator-Key") or "", secret
                        ):
                            raise WebInputError("Operator verification required", 403)
                        migration_id = data["schema_migration_id"]
                        expected_hash = data.get("expected_backup_sha256")
                        if (
                            "backup" in data
                            or not isinstance(migration_id, str)
                            or not re.fullmatch("[a-f0-9]{32}", migration_id)
                            or not isinstance(expected_hash, str)
                            or not re.fullmatch("[a-f0-9]{64}", expected_hash)
                        ):
                            raise WebInputError("Tham chiếu backup cloud không hợp lệ.")
                        pinned = self.rows(
                            "SELECT record FROM control WHERE id=?",
                            "pre-schema11-" + migration_id,
                        )
                        if not pinned:
                            raise WebInputError(
                                "Không tìm thấy preimage migration.", 404
                            )
                        incoming = json.loads(pinned[0]["record"])
                        if not isinstance(incoming, dict) or not isinstance(
                            incoming.get("objects"), list
                        ):
                            raise WebInputError("Preimage thiếu manifest media.")
                        for obj in incoming["objects"]:
                            raw = self.object_bytes(obj["sha256"])
                            if len(raw) != obj["size"] or digest(raw) != obj["sha256"]:
                                raise WebInputError("Media preimage không khớp.")
                            obj["content_base64"] = base64.b64encode(raw).decode()
                        if (
                            digest(
                                json.dumps(
                                    incoming,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode()
                            )
                            != expected_hash
                        ):
                            raise WebInputError("Checksum gói preimage không khớp.")
                    if (
                        not isinstance(incoming, dict)
                        or incoming.get("format") != "CREATOR_WEB_BACKUP_V1"
                        or incoming.get("version") != VERSION
                        or len(incoming.get("checkpoint", [])) != 1
                    ):
                        raise WebInputError(
                            "Backup không tương thích hoặc chưa có checkpoint. Chọn gói DB + media do bản Web này xuất; không dùng tệp tư liệu."
                        )
                    snapshot = incoming["checkpoint"][0]
                    try:
                        restored = unpack_database(snapshot["payload"])
                    except (ValueError, KeyError, TypeError, zlib.error):
                        raise WebInputError(
                            "Backup hỏng hoặc vượt giới hạn DB; dữ liệu hiện tại được giữ nguyên."
                        ) from None
                    if (
                        len(restored) > 16 * 1024 * 1024
                        or digest(restored) != snapshot["sha256"]
                    ):
                        raise WebInputError(
                            "Checksum DB của backup không khớp; chưa khôi phục dữ liệu."
                        )
                    incoming_objects = incoming.get("objects")
                    if not isinstance(incoming_objects, list) or any(
                        not isinstance(o, dict) for o in incoming_objects
                    ):
                        raise WebInputError(
                            "Backup thiếu danh sách media; chưa khôi phục dữ liệu."
                        )
                    incoming_hashes = {o.get("sha256") for o in incoming_objects}
                    if len(incoming_hashes) != len(incoming_objects):
                        raise WebInputError(
                            "Backup có media trùng hoặc không hợp lệ; chưa khôi phục dữ liệu."
                        )
                    # Preserve the previous exact DB revision before replacement;
                    # immutable object bytes remain available for rollback.
                    previous = self.rows("SELECT * FROM checkpoint WHERE id=1")
                    self.sql.exec(
                        "INSERT INTO control VALUES(?,?)",
                        "pre-restore-" + key,
                        json.dumps(previous),
                    )
                    for o in incoming_objects:
                        raw = base64.b64decode(o["content_base64"], validate=True)
                        if len(raw) != o["size"]:
                            raise WebInputError(
                                "Kích thước media trong backup không khớp; đã hủy khôi phục, dữ liệu hiện tại giữ nguyên."
                            )
                        self.put_object(o["sha256"], raw)
                    # Remove disposable WAL/SHM by closing all projection readers.
                    for suffix in ("-wal", "-shm"):
                        (root / ("creator_loop.sqlite3" + suffix)).unlink(
                            missing_ok=True
                        )
                    (root / "creator_loop.sqlite3").write_bytes(restored)
                    with closing(
                        _connect_write(root / "creator_loop.sqlite3")
                    ) as check:
                        source_schema = check.execute("PRAGMA user_version").fetchone()[
                            0
                        ]
                        if source_schema == 10:
                            if (
                                data.get("confirm_schema10_additive_upgrade")
                                is not True
                            ):
                                raise WebInputError(
                                    "Backup schema10 cần xác nhận nâng cấu trúc nguồn lên11 khi khôi phục; bản gốc/byte media và backup vẫn được giữ."
                                )
                            validate(check, expected_version=10)
                            with check:
                                check.execute("BEGIN IMMEDIATE")
                                _migrate_locked(check, 10)
                        validate(check)
                        check.execute(
                            "UPDATE processing_runs SET status='FAILED',finished_at=?,error_code='RESTORED_INTERRUPTED_WEB_RUN' WHERE tool_name IN ('CLOUDFLARE_WORKERS_AI_WEB','CLOUDFLARE_WORKERS_AI_SOURCE') AND status IN ('QUEUED','RUNNING')",
                            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
                        )
                        check.commit()
                        for storage_key, sha, size in check.execute(
                            "SELECT storage_key,sha256,byte_size FROM asset_files WHERE role='ORIGINAL'"
                        ):
                            if sha not in incoming_hashes:
                                raise WebInputError(
                                    "Gói backup thiếu media được DB tham chiếu; đã hủy toàn bộ khôi phục."
                                )
                            if storage_key != "storage/originals/" + sha:
                                raise WebInputError(
                                    "Backup dùng đường dẫn media không tương thích; đã hủy khôi phục."
                                )
                            raw = self.object_bytes(sha)
                            if len(raw) != size or digest(raw) != sha:
                                raise WebInputError(
                                    "Backup thiếu media hoặc checksum không khớp; đã hủy khôi phục."
                                )
                    self.sql.exec(
                        "UPDATE receipts SET response=?",
                        json.dumps({"restored_away": True, "restore_id": key}),
                    )
                    for old in self.rows("SELECT record FROM jobs"):
                        historic = json.loads(old["record"])
                        historic.update(
                            original_status=historic["status"],
                            status="HISTORICAL_PRE_RESTORE",
                            message="Kết quả thuộc trạng thái trước restore; không tự gửi lại",
                        )
                        self.save_job(historic)
                    result = {
                        "restored": True,
                        "revision": revision + 1,
                        "previous_checkpoint_retained": True,
                    }
                elif path == "/api/drafts":
                    result = domain.draft(data)
                elif path == "/api/jobs":
                    choices = data.pop("draft_choices", {})
                    creator = request_from_json(data)
                    materials = domain.prepare(creator)
                    if creator.output_format == "IMAGE_CAPTION":
                        if (
                            not isinstance(choices, dict)
                            or set(choices)
                            != {"image_file_id", "image_provenance", "image_usage_note"}
                            or any(
                                not isinstance(v, str) or not v.strip() or len(v) > 2000
                                for v in choices.values()
                            )
                        ):
                            raise ValueError(
                                "Choose an uploaded image and state its source/usage basis before generation"
                            )
                        domain.require_project_image(
                            creator.project_id, choices["image_file_id"]
                        )
                        from creator_loop.backend.library.originals import (
                            verify_original_file,
                        )

                        with closing(
                            _connect_write(root / "creator_loop.sqlite3")
                        ) as check:
                            verify_original_file(check, choices["image_file_id"], root)
                            if not check.execute(
                                "SELECT 1 FROM assets a JOIN asset_files f USING(asset_id) WHERE f.file_id=? AND a.media_type='IMAGE' AND a.deleted_at IS NULL",
                                (choices["image_file_id"],),
                            ).fetchone():
                                raise ValueError("A live original IMAGE is required")
                    elif choices:
                        raise ValueError("Image selection belongs to IMAGE_CAPTION")
                    if self.rows(
                        "SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
                    ):
                        raise ValueError("Only one active B job")
                    grants = self.rows("SELECT record FROM control WHERE id='grant'")
                    grant = json.loads(grants[0]["record"]) if grants else {}
                    if (
                        not valid_included_grant(
                            grant, self.env.CREATOR_MODEL, time.time()
                        )
                        or grant["remaining_calls"] != 4
                    ):
                        raise WebInputError(
                            "Cần quyền operator còn hiệu lực cho phần bao gồm và đủ dự phòng trước khi gọi AI; không cho phép phí ngoài gói."
                        )
                    grant["remaining_calls"] -= 4
                    grant["reserved_pipeline"] = key
                    self.sql.exec(
                        "UPDATE control SET record=? WHERE id='grant'",
                        json.dumps(grant),
                    )
                    result = {
                        "id": key,
                        "status": "QUEUED",
                        "stage": 0,
                        "request": asdict(creator),
                        "sources": [asdict(m) for m in materials],
                        "input_fingerprint": fingerprint,
                        "created_at": time.time(),
                        "calls": [],
                        "grant": grant,
                        "draft_choices": choices,
                    }
                    domain.begin_run(creator, key, VERSION, self.env.CREATOR_MODEL)
                    self.save_job(result)
                else:
                    raise ValueError("Unknown mutation")
                self.commit(root, revision)
                result["revision"] = revision + 1
                self.sql.exec(
                    "INSERT INTO receipts VALUES(?,?,?)",
                    key,
                    fingerprint,
                    json.dumps(result, ensure_ascii=False),
                )
                return result

            if path in ("/api/jobs", "/api/source-jobs"):
                # Persist the wake-up BEFORE the queued-job transaction. Crash
                # before commit leaves an idle alarm; crash after commit cannot
                # strand a durable job without a durable wake-up. Revision CAS
                # still refuses any projection made stale during this await.
                await self.ctx.storage.setAlarm(int(time.time() * 1000) + 1000)
            try:
                result = self.ctx.storage.transactionSync(mutate)
            except WebInputError:
                raise
            except (
                ValueError,
                KeyError,
                TypeError,
                sqlite3.Error,
                zlib.error,
                RuntimeError,
            ):
                if path == "/api/restore":
                    raise WebInputError(
                        "Backup hỏng, sai schema hoặc thiếu media. Đã hủy toàn bộ khôi phục; DB/media hiện tại giữ nguyên."
                    ) from None
                raise
            return reply(result)
        finally:
            self.discard_projection(root)

    async def extract_source(self, job):
        image = job.get("kind") == "SOURCE_IMAGE_ANALYSIS"
        model = IMAGE_MODEL if image else WHISPER_MODEL
        root, _ = self.load()
        try:
            material = WebDomain(root)._project_original(
                job["request"]["project_id"], job["request"]["file_id"]
            )
            if material["media_type"] != ("IMAGE" if image else "AUDIO"):
                raise ValueError("Source media type changed")
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                row = db.execute(
                    "SELECT storage_key,sha256 FROM asset_files WHERE file_id=? AND role='ORIGINAL'",
                    (job["request"]["file_id"],),
                ).fetchone()
            if row is None or row[0] != "storage/originals/" + row[1]:
                raise ValueError("Source original changed")
            raw = (root / row[0]).read_bytes()
            probe = probe_image(raw) if image else probe_pcm_wav(raw)
            source = job["sources"][0]
            if probe.sha256 != source["input_sha256"] or (
                probe.mime_type != source["mime_type"]
                if image
                else probe.duration_ms != source["duration_ms"]
            ):
                raise ValueError("Source identity changed after reservation")
            packet = {
                **{k: v for k, v in source.items() if k != "file_id"},
                ("image" if image else "audio"): base64.b64encode(raw).decode(),
            }
        finally:
            self.discard_projection(root)
        grant = job.get("grant", {})
        if (
            not valid_included_grant(grant, model, time.time())
            or grant.get("remaining_calls") != 0
            or grant.get("reserved_pipeline") != job["id"]
        ):
            raise WebInputError(
                "Quyền đọc nguồn/quota đã hết hạn trước khi gửi. Chưa gọi AI; giữ reservation, không tự thử lại."
            )
        call = {
            "stage": "image_analysis" if image else "transcription",
            "request_id": job["id"],
            "reserved_at": time.time(),
            "outcome": "UNKNOWN",
            "input_sha256": source["input_sha256"],
        }
        job["calls"].append(call)
        job["stage"] = 1
        self.save_job(job)
        sdk = import_from_javascript("creator_ai.mjs")
        reader = sdk.analyzeSourceImage if image else sdk.transcribeSource
        encoded = await reader(
            python_to_rpc(self.env.AI),
            json.dumps(packet),
            str(self.env.CREATOR_VERSION.id),
        )
        if len(encoded.encode()) > 512 * 1024:
            raise ValueError("Source response exceeds retained protocol")
        response = json.loads(encoded)
        call.update(
            outcome="UNKNOWN" if response.get("unknown_outcome") else "RECEIVED",
            finished_at=time.time(),
            model_calls=response.get("model_calls"),
            measured_usage=response.get("native_usage"),
        )
        if response.get("unknown_outcome"):
            job["unknown_outcome"] = True
        job["stage_outputs"] = [response]
        current = json.loads(
            self.rows("SELECT record FROM jobs WHERE id=?", job["id"])[0]["record"]
        )
        if current["status"] != "RUNNING":
            current.update(calls=job["calls"], stage_outputs=job["stage_outputs"])
            self.save_job(current)
            return
        self.save_job(job)
        if (
            not response.get("ok")
            or response.get("model") != model
            or response.get("worker_version") != str(self.env.CREATOR_VERSION.id)
            or response.get("request_id") != job["id"]
            or response.get("input_sha256") != source["input_sha256"]
            or response.get("model_calls") != 1
        ):
            raise ValueError("Source model/response binding rejected")
        root, revision = self.load()
        try:

            def finish():
                with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                    store = store_cloud_image if image else store_cloud_transcript
                    result = store(
                        db,
                        data_root=root,
                        file_id=job["request"]["file_id"],
                        run_id=job["id"],
                        native_response=response["native_response"],
                    )
                self.put_object(result["sha256"], result["content"])
                self.commit(root, revision)
                job.update(
                    status="SUCCEEDED",
                    finished_at=time.time(),
                    evidence_version_ids=result["evidence_version_ids"],
                    message="Đã trích xuất; cần đối chiếu bản gốc và xác nhận phần nguồn được dùng, chưa duyệt factual/đăng.",
                )
                self.save_job(job)

            self.ctx.storage.transactionSync(finish)
        finally:
            self.discard_projection(root)

    def save_job(self, record):
        self.sql.exec(
            "INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,record=excluded.record",
            record["id"],
            record["status"],
            json.dumps(record, ensure_ascii=False),
        )

    def sync_run(self, record):
        root, revision = self.load()
        try:

            def update():
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                    if record["status"] == "RUNNING":
                        db.execute(
                            "UPDATE processing_runs SET status='RUNNING',started_at=? WHERE run_id=? AND status='QUEUED'",
                            (now, record["id"]),
                        )
                    elif record["status"] in ("CANCELLED", "FAILED"):
                        db.execute(
                            "UPDATE processing_runs SET status=?,finished_at=?,error_code=? WHERE run_id=? AND status IN ('QUEUED','RUNNING')",
                            (
                                record["status"],
                                now,
                                "UNKNOWN_REMOTE_OUTCOME"
                                if record.get("unknown_outcome")
                                else record["status"],
                                record["id"],
                            ),
                        )
                    db.commit()
                self.commit(root, revision)
                self.save_job(record)

            self.ctx.storage.transactionSync(update)
        finally:
            self.discard_projection(root)

    async def alarm(self):
        pending = self.rows(
            "SELECT record FROM jobs WHERE status IN ('QUEUED','RUNNING') ORDER BY rowid LIMIT 1"
        )
        if not pending:
            return
        job = json.loads(pending[0]["record"])
        if job["status"] == "RUNNING":
            if job.get("lease_until", 0) > time.time():
                await self.ctx.storage.setAlarm(int(job["lease_until"] * 1000) + 1)
                return
            job.update(
                status="FAILED",
                unknown_outcome=True,
                message="Kết quả request trước chưa rõ sau gián đoạn; không tự gửi lại",
            )
            self.sync_run(job)
            return
        job.update(status="RUNNING", lease_until=time.time() + 600, stage_outputs=[])
        self.sync_run(job)
        await self.ctx.storage.setAlarm(int(job["lease_until"] * 1000) + 1)
        try:
            if job.get("kind") in ("SOURCE_TRANSCRIPTION", "SOURCE_IMAGE_ANALYSIS"):
                await self.extract_source(job)
                return
            creator = request_from_json(
                {k: v for k, v in job["request"].items() if k != "actor"}
            )
            from creator_loop.shared.workflow import ResearchMaterial

            materials = [
                ResearchMaterial(
                    **{**m, "evidence_version_ids": tuple(m["evidence_version_ids"])}
                )
                for m in job["sources"]
            ]
            prompt = generation_prompt(creator, materials)
            schema = response_schema([m.material_id for m in materials])
            sdk = import_from_javascript("creator_ai.mjs")
            previous = []
            for index, stage in enumerate(
                ("analysis", "writing", "editing", "source_audit")
            ):
                current = json.loads(
                    self.rows("SELECT record FROM jobs WHERE id=?", job["id"])[0][
                        "record"
                    ]
                )
                if current["status"] == "CANCELLED":
                    return
                packet = {
                    "prompt": prompt,
                    "writing_prompt": generation_prompt(
                        creator, materials, only_draft=True
                    ),
                    "schema": schema,
                    "stage": stage,
                    "previous": previous,
                    "format": creator.output_format,
                    "mode": creator.mode,
                }
                encoded = json.dumps(packet, ensure_ascii=False)
                if len(encoded.encode()) > 64 * 1024:
                    raise ValueError("Cloud stage request exceeds bounded protocol")
                call = {
                    "stage": stage,
                    "request_id": uuid4().hex,
                    "reserved_at": time.time(),
                    "outcome": "UNKNOWN",
                    "packet_sha256": digest(encoded.encode()),
                }
                job["calls"].append(call)
                job["stage"] = index + 1
                self.save_job(job)
                result = json.loads(
                    await sdk.stage(
                        python_to_rpc(self.env.AI),
                        self.env.CREATOR_MODEL,
                        encoded,
                        str(self.env.CREATOR_VERSION.id),
                    )
                )
                call.update(
                    outcome="RECEIVED",
                    finished_at=time.time(),
                    output_sha256=digest(
                        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
                    ),
                    model_calls=result["model_calls"],
                    measured_usage=result.get("native_usage"),
                )
                job["stage_outputs"].append(result)
                current = json.loads(
                    self.rows("SELECT record FROM jobs WHERE id=?", job["id"])[0][
                        "record"
                    ]
                )
                if current["status"] == "CANCELLED":
                    current["calls"] = job["calls"]
                    current["stage_outputs"] = job["stage_outputs"]
                    self.save_job(current)
                    return
                self.save_job(job)
                if (
                    not result.get("ok")
                    or result.get("model") != self.env.CREATOR_MODEL
                    or result.get("worker_version") != str(self.env.CREATOR_VERSION.id)
                    or result["model_calls"] != 1
                ):
                    raise ValueError(
                        "Stage rejected or model/deployment identity changed"
                    )
                if stage != "source_audit":
                    item = result.get("stage_result")
                    if not isinstance(item, dict) or item.get("stage") != stage:
                        raise ValueError("Stage chain incomplete/reordered")
                    previous.append(item)
            content = json.loads(result["result"]["response"])
            analysis_receipt = result.get("analysis_audit")
            validate_analysis_audit(
                analysis_receipt, previous[0]["value"], content, job["sources"]
            )
            stages = result.get("stages")
            if (
                not isinstance(stages, list)
                or len(stages) != 4
                or [s.get("stage") for s in stages]
                != ["analysis", "writing", "editing", "source_audit"]
                or content.get("draft_body") != stages[2].get("body")
            ):
                raise ValueError("Audit changed the immutable edited body")
            from creator_loop.backend.analysis.workers_quality import (
                requires_hypothetical_opening,
                validate_quality_audit,
                worker_audit_findings,
            )

            if creator.output_format == "TEXT":
                audit_findings = worker_audit_findings(previous[2])
            else:
                expected = [
                    "PROJECT_NO_UNSOURCED_RULE",
                    "PROJECT_NONREPETITIVE_DEVELOPMENT",
                    "PROJECT_REQUEST_AND_FORMAT",
                ]
                if previous[2].get("task_requirements") != expected:
                    raise ValueError("Format task coverage missing")
                edits = previous[2]["value"]
                audit_findings = list(
                    dict.fromkeys(
                        [
                            *edits["source_issues"],
                            *(
                                e["note"]
                                for e in edits["edits"]
                                if e["support"] == "INSUFFICIENT"
                            ),
                            *expected,
                        ]
                    )
                )
            validate_quality_audit(
                content,
                result.get("quality_audit"),
                audit_findings,
                require_hypothetical=creator.output_format == "TEXT"
                and requires_hypothetical_opening(creator.purpose, creator.direction),
            )
            root, revision = self.load()
            try:

                def finish():
                    domain = WebDomain(root)
                    wid = domain.finish(
                        creator,
                        content,
                        {
                            "version": VERSION,
                            "backend": "CLOUDFLARE_WORKERS_AI_WEB",
                            "model": self.env.CREATOR_MODEL,
                            "run_id": job["id"],
                            "requests": job["calls"],
                            "worker_version": str(self.env.CREATOR_VERSION.id),
                            "quality_audit": result["quality_audit"],
                            "analysis_audit": analysis_receipt,
                            "stages": stages,
                        },
                        job["input_fingerprint"],
                    )
                    saved = None
                    if creator.mode == "AUTOMATIC":
                        saved = domain.draft(
                            {"workflow_id": wid, **job.get("draft_choices", {})}
                        )
                    self.commit(root, revision)
                    job.update(
                        status="SUCCEEDED",
                        workflow_id=wid,
                        finished_at=time.time(),
                        result=content,
                        saved_draft=saved,
                    )
                    self.save_job(job)

                self.ctx.storage.transactionSync(finish)
            finally:
                self.discard_projection(root)
        except Exception as error:
            current = json.loads(
                self.rows("SELECT record FROM jobs WHERE id=?", job["id"])[0]["record"]
            )
            if current["status"] != "CANCELLED":
                job.update(
                    status="FAILED",
                    finished_at=time.time(),
                    message=str(error)
                    if isinstance(error, WebInputError)
                    else "Không hoàn tất; không tự retry/fallback. Kiểm kết quả từng request trước lượt mới.",
                )
                self.sync_run(job)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await self.env.DATABASE.getByName("private-owner-v1").fetch(request)
