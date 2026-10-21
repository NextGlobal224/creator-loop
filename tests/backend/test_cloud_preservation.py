"""Direct archive API tests; SQLite stand-in is not deployed Cloudflare QA."""

import ast
import asyncio
import base64
import hashlib
import hmac
import io
import json
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from creator_loop.backend.creator.web_domain import WebInputError


def load_api():
    source = Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
    tree = ast.parse(source.read_text("utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PreservationReader":
            selected.append(node)
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "preservation_restore_preflight"
        ):
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "Database":
            for method in node.body:
                if isinstance(
                    method, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and method.name in (
                    "preservation_api",
                    "preservation_chunk",
                    "discard_projection",
                    "authorized",
                ):
                    selected.append(method)
    namespace = dict(
        base64=base64,
        hashlib=hashlib,
        json=json,
        re=re,
        time=time,
        Path=Path,
        hmac=hmac,
        uuid4=uuid4,
        WebInputError=WebInputError,
        PRESERVATION_CHUNK=512 * 1024,
        PRESERVATION_MAX=64 * 1024 * 1024,
        PRESERVATION_RESTORE_MAX=88 * 1024 * 1024,
        zipfile=zipfile,
        digest=lambda raw: hashlib.sha256(raw).hexdigest(),
        reply=lambda value, status=200, **_: SimpleNamespace(
            value=value, status=status
        ),
    )
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"),
        namespace,
    )
    return namespace


class CloudPreservationTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_api()
        isolated = tempfile.TemporaryDirectory(prefix="cloud-archive-fixture-")
        self.addCleanup(isolated.cleanup)
        self.ns["Path"] = lambda value: (
            Path(isolated.name) if value == "/tmp" else Path(value)
        )
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE preservation_archives(sha256 TEXT PRIMARY KEY,record TEXT);"
            "CREATE TABLE preservation_chunks(sha256 TEXT,position INTEGER,content TEXT,PRIMARY KEY(sha256,position));"
            "CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,payload TEXT);"
            "INSERT INTO checkpoint VALUES(1,'production unchanged');"
        )

        def transaction(callback):
            with self.db:
                return callback()

        sql = SimpleNamespace(exec=lambda query, *args: self.db.execute(query, args))
        attrs = {
            name: self.ns[name]
            for name in (
                "preservation_api",
                "preservation_chunk",
                "discard_projection",
                "authorized",
            )
        }
        self.api = type("ArchiveTestDatabase", (), attrs)()
        self.api.sql = sql
        self.api.rows = lambda query, *args: [
            dict(r) for r in self.db.execute(query, args)
        ]
        self.api.ctx = SimpleNamespace(
            storage=SimpleNamespace(transactionSync=transaction)
        )
        self.api.env = SimpleNamespace(
            CREATOR_VERSION=SimpleNamespace(id="isolated-fixture"),
            OPERATOR_KEY="fixture",
        )
        self.raw = b"x" * (512 * 1024) + "tư liệu".encode()
        self.record = self.identity(self.raw)
        self.base = "/api/preservation"

    def identity(self, raw):
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "source_commit": "a" * 40,
            "chunk_hashes": [
                hashlib.sha256(raw[i : i + 512 * 1024]).hexdigest()
                for i in range(0, len(raw), 512 * 1024)
            ],
        }

    def call(self, suffix="", method="GET", data=None):
        return self.api.preservation_api(self.base + suffix, method, data or {})

    def upload(self, raw):
        record = self.identity(raw)
        self.call(method="POST", data=record)
        for index, offset in enumerate(range(0, len(raw), 512 * 1024)):
            self.call(
                f"/{record['sha256']}/chunks/{index}",
                "POST",
                {
                    "content_base64": base64.b64encode(
                        raw[offset : offset + 512 * 1024]
                    ).decode()
                },
            )
        return record["sha256"]

    def test_unknown_ack_resumes_same_immutable_identity_and_missing_positions(self):
        self.call(method="POST", data=self.record)
        self.assertEqual(
            self.call(method="POST", data=self.record).value["status"], "STAGING"
        )
        key = self.upload(self.raw)
        self.upload(self.raw)  # exact re-submission is idempotent, no duplicate rows
        result = self.call("/" + key).value
        self.assertEqual(result["uploaded_positions"], [0, 1])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM preservation_chunks").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT payload FROM checkpoint").fetchone()[0],
            "production unchanged",
        )
        with self.assertRaises(WebInputError):
            self.call(method="POST", data=self.identity(b"different"))
        with self.assertRaises(WebInputError):
            self.call(method="POST", data={**self.record, "source_commit": "b" * 40})

    def test_bad_or_missing_chunks_never_verify_or_replace_original(self):
        self.call(method="POST", data=self.record)
        key = self.record["sha256"]
        with self.assertRaises(WebInputError):
            self.call("/" + key + "/verify", "POST")
        for suffix, body in (
            ("/chunks/2", {"content_base64": "eA=="}),
            ("/chunks/0", {"content_base64": "%%%"}),
            ("/chunks/0", {"content_base64": "eA=="}),
        ):
            with self.assertRaises((ValueError, WebInputError)):
                self.call("/" + key + suffix, "POST", body)
        self.assertEqual(self.call("/" + key).value["status"], "STAGING")

    def test_operator_auth_required_before_archive_api(self):
        for headers in ({}, {"X-Operator-Key": "wrong"}):
            result = asyncio.run(
                self.api.authorized(
                    SimpleNamespace(headers=headers), self.base, "GET", {}
                )
            )
            self.assertEqual(result.status, 403)
        result = asyncio.run(
            self.api.authorized(
                SimpleNamespace(headers={"X-Operator-Key": "fixture"}),
                self.base,
                "GET",
                {},
            )
        )
        self.assertEqual(result.status, 200)

    def test_seekable_reader_validates_each_chunk_and_complete_digest(self):
        key = self.upload(self.raw)
        reader = self.ns["PreservationReader"](
            self.record, lambda index: self.api.preservation_chunk(key, index)
        )
        reader.seek(-7, 2)
        self.assertEqual(reader.read(), self.raw[-7:])
        reader.seek(512 * 1024 - 3)
        self.assertEqual(reader.read(8), self.raw[512 * 1024 - 3 : 512 * 1024 + 5])
        with self.assertRaises(ValueError):
            reader.seek(-1)
        self.db.execute(
            "UPDATE preservation_chunks SET content=? WHERE position=0",
            (base64.b64encode(b"tampered").decode(),),
        )
        with self.assertRaises(ValueError):
            self.ns["PreservationReader"](
                self.record, lambda index: self.api.preservation_chunk(key, index)
            ).read(1)
        oversized = self.ns["PreservationReader"](
            {"bytes": 8 * 1024 * 1024},
            lambda _: self.fail("must refuse before reading chunks"),
        )
        with self.assertRaises(WebInputError):
            oversized.read()

    def test_bad_archive_retains_upload_without_partial_restore_or_verified_label(self):
        key = self.upload(self.raw)
        with self.assertRaises(zipfile.BadZipFile):
            self.call("/" + key + "/verify", "POST")
        self.assertEqual(self.call("/" + key).value["status"], "STAGING")
        self.assertEqual(
            self.db.execute("SELECT payload FROM checkpoint").fetchone()[0],
            "production unchanged",
        )

    def test_declared_restore_and_media_copies_refuse_before_extraction(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "preservation.json",
                json.dumps(
                    {
                        "media": [
                            {
                                "storage_key": "storage/originals/large",
                                "bytes": 89 * 1024 * 1024,
                            }
                        ]
                    }
                ),
            )
        key = self.upload(buffer.getvalue())
        with self.assertRaises(WebInputError) as error:
            self.call("/" + key + "/verify", "POST")
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(self.call("/" + key).value["status"], "STAGING")
        self.assertEqual(
            self.db.execute("SELECT payload FROM checkpoint").fetchone()[0],
            "production unchanged",
        )

    @unittest.skipUnless(
        sys.platform == "win32", "native isolated preservation fixture"
    )
    def test_real_archive_reopens_database_media_and_quarantined_evidence(self):
        from tests.backend.test_migration_preservation import MigrationPreservationTests

        fixture = MigrationPreservationTests("runTest")
        try:
            fixture.setUp()
            raw = fixture.archive().read_bytes()
            key = self.upload(raw)
            proof = self.call("/" + key + "/verify", "POST").value
            self.assertEqual(proof["status"], "VERIFIED")
            self.assertEqual(proof["proof"]["integrity_and_foreign_keys"], "PASS")
            self.assertFalse(proof["proof"]["permits_migration_or_cleanup"])
            self.assertFalse(proof["proof"]["runtime_evidence_executed"])
            self.assertEqual(self.call("/" + key + "/verify", "POST").value, proof)
            self.assertEqual(
                self.db.execute("SELECT payload FROM checkpoint").fetchone()[0],
                "production unchanged",
            )
        finally:
            fixture.doCleanups()
