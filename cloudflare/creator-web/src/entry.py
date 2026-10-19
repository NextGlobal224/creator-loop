"""Private B Web: one Worker, durable serialized SQLite, immutable media objects."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
import time
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
from creator_loop.backend.runtime.database import (
    MIGRATIONS,
    _connect_write,
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
MAX_MEDIA_TOTAL = 8 * 1024 * 1024
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
            validate(db)
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
        total = self.rows("SELECT COALESCE(SUM(size),0) AS size FROM objects")[0][
            "size"
        ]
        if total + len(raw) > MAX_MEDIA_TOTAL:
            raise WebInputError(
                "Kho media beta đã đạt giới hạn tổng 8 MiB. Xuất backup; không tự xóa dữ liệu hoặc nâng gói."
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
                or data["model"] != self.env.CREATOR_MODEL
            ):
                raise WebInputError(
                    "Cần bằng chứng đúng account/Workers plan và phần bao gồm; chưa tạo quyền AI."
                )
            if not valid_included_grant(data, self.env.CREATOR_MODEL, time.time()):
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
                        "SELECT storage_key,sha256,mime_type FROM asset_files WHERE file_id=? AND role='ORIGINAL'",
                        (fid,),
                    ).fetchone()
                if not row:
                    return reply({"error": "Media not found"}, 404)
                raw = (root / row[0]).read_bytes()
                if digest(raw) != row[1]:
                    raise ValueError("Media changed")
                return Response(
                    raw,
                    headers={
                        **HEADERS,
                        "Content-Type": row[2],
                        "Content-Disposition": "inline"
                        if row[2].startswith("image/")
                        else "attachment",
                    },
                )
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
        if not path.startswith("/api/") and method == "GET":
            return await self.env.ASSETS.fetch(request)
        root, revision = self.load()
        try:
            domain = WebDomain(root)
            if method == "GET":
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
                if path == "/api/projects":
                    result = domain.project(data.get("title", ""))
                elif path == "/api/materials":
                    if not data.get("project_id"):
                        raise WebInputError("Chọn Project trước khi lưu tư liệu.")
                    raw = base64.b64decode(data.pop("content_base64"), validate=True)
                    result = domain.intake(data, raw)
                    self.put_object(result["sha256"], raw)
                elif path == "/api/material-links":
                    result = domain.link_material(
                        data.get("project_id", ""), data.get("material_id", "")
                    )
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
                        validate(check)
                        check.execute(
                            "UPDATE processing_runs SET status='FAILED',finished_at=?,error_code='RESTORED_INTERRUPTED_WEB_RUN' WHERE tool_name='CLOUDFLARE_WORKERS_AI_WEB' AND status IN ('QUEUED','RUNNING')",
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

            if path == "/api/jobs":
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
        except Exception:
            current = json.loads(
                self.rows("SELECT record FROM jobs WHERE id=?", job["id"])[0]["record"]
            )
            if current["status"] != "CANCELLED":
                job.update(
                    status="FAILED",
                    finished_at=time.time(),
                    message="Không hoàn tất; không tự retry/fallback. Kiểm kết quả từng request trước lượt mới.",
                )
                self.sync_run(job)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await self.env.DATABASE.getByName("private-owner-v1").fetch(request)
