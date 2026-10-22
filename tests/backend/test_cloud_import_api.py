"""Actual import handler with SQLite coordination stand-in, not cloud QA."""

import ast
import asyncio
import base64
import copy
import hashlib
import hmac
import json
import re
import sqlite3
import tempfile
import time
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from creator_loop.backend.creator.web_domain import WebDomain, WebInputError
from creator_loop.backend.runtime import database
from creator_loop.backend.runtime.cloud_import import B_TABLES
from creator_loop.backend.runtime.database import (
    _connect_write,
    _migrate_locked,
    initialize,
    validate,
)

from tests.backend import test_web_domain


class CloudImportAPITests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_web_domain.WebDomainTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        target = self.root / "initial.sqlite3"
        initialize(target)
        with closing(_connect_write(target)) as db:
            raw = db.serialize()
        self.coord = sqlite3.connect(":memory:")
        self.addCleanup(self.coord.close)
        self.coord.row_factory = sqlite3.Row
        self.coord.executescript(
            "CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,revision INTEGER,payload TEXT,sha256 TEXT);"
            "CREATE TABLE receipts(id TEXT PRIMARY KEY,fingerprint TEXT,response TEXT);"
            "CREATE TABLE backups(id TEXT PRIMARY KEY,record TEXT);"
            "CREATE TABLE objects(sha256 TEXT PRIMARY KEY,size INTEGER);"
            "CREATE TABLE object_chunks(sha256 TEXT,position INTEGER,content TEXT,PRIMARY KEY(sha256,position));"
            "CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,record TEXT);"
            "CREATE TABLE control(id TEXT PRIMARY KEY,record TEXT);"
        )

        def digest(value):
            return hashlib.sha256(value).hexdigest()

        self.coord.execute(
            "INSERT INTO checkpoint VALUES(1,5,?,?)",
            (base64.b64encode(zlib.compress(raw)).decode(), digest(raw)),
        )
        self.coord.commit()
        source = (
            Path(__file__).resolve().parents[2] / "cloudflare/creator-web/src/entry.py"
        )
        tree = ast.parse(source.read_text("utf-8"))
        methods = {
            "import_preserved_b",
            "authorized",
            "put_object",
            "object_bytes",
            "commit",
            "discard_projection",
        }
        nodes = [
            method
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Database"
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and method.name in methods
        ]
        nodes.extend(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "unpack_database"
        )
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name)
                and t.id in {"VERSION", "MAX_MEDIA_TOTAL", "MAX_MEDIA_OBJECTS"}
                for t in node.targets
            ):
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]), str(source), "exec"
                    ),
                    constants,
                )
        namespace = {
            "base64": base64,
            "sqlite3": sqlite3,
            "hashlib": hashlib,
            "json": json,
            "re": re,
            "time": time,
            "hmac": hmac,
            "zlib": zlib,
            "closing": closing,
            "WebDomain": WebDomain,
            "WebInputError": WebInputError,
            "_connect_write": _connect_write,
            "validate": validate,
            "_migrate_locked": _migrate_locked,
            "Path": lambda value: self.root if value == "/tmp" else Path(value),
            "digest": digest,
            "reply": lambda value, status=200, **_: SimpleNamespace(
                value=value, status=status
            ),
            **constants,
        }
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"),
            namespace,
        )
        self.api = type(
            "ImportDatabaseFixture", (), {n: namespace[n] for n in methods}
        )()
        self.api.rows = lambda query, *args: [
            dict(r) for r in self.coord.execute(query, args)
        ]
        self.api.sql = SimpleNamespace(
            exec=lambda query, *args: self.coord.execute(query, args)
        )
        self.api.env = SimpleNamespace(OPERATOR_KEY="fixture")

        def transaction(callback):
            with self.coord:
                return callback()

        self.api.ctx = SimpleNamespace(
            storage=SimpleNamespace(transactionSync=transaction)
        )

        def load():
            row = self.api.rows("SELECT * FROM checkpoint")[0]
            root = self.root / ("creator-web-" + uuid4().hex)
            root.mkdir()
            (root / "creator_loop.sqlite3").write_bytes(
                zlib.decompress(base64.b64decode(row["payload"]))
            )
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                for key, sha in db.execute(
                    "SELECT storage_key,sha256 FROM asset_files WHERE role='ORIGINAL'"
                ):
                    p = root / key
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(self.api.object_bytes(sha))
            return root, row["revision"]

        self.api.load = load
        tables = {}
        with closing(_connect_write(self.fixture.domain.path)) as db:
            for table in B_TABLES:
                cursor = db.execute('SELECT * FROM "' + table + '"')
                names = [c[0] for c in cursor.description]
                rows = [dict(zip(names, r)) for r in cursor]
                if rows:
                    tables[table] = rows
        objects = {}
        for row in tables["asset_files"]:
            raw = (self.fixture.root / row["storage_key"]).read_bytes()
            objects[row["sha256"]] = {
                "sha256": row["sha256"],
                "size": len(raw),
                "content_base64": base64.b64encode(raw).decode(),
            }
        self.backup_id = "b" * 32
        self.coord.execute(
            "INSERT INTO backups VALUES(?,?)",
            (
                self.backup_id,
                json.dumps(
                    {
                        "checkpoint": self.api.rows("SELECT * FROM checkpoint"),
                        "objects": [],
                    }
                ),
            ),
        )
        self.coord.commit()
        self.data = {
            "format": "B_CLOUD_IMPORT_V1",
            "expected_revision": 5,
            "expected_checkpoint_sha256": self.api.rows("SELECT * FROM checkpoint")[0][
                "sha256"
            ],
            "backup_id": self.backup_id,
            "source_archive_sha256": "a" * 64,
            "source_database_sha256": "c" * 64,
            "source_schema_version": 9,
            "source_cloud_receipt_sha256": "d" * 64,
            "source_rows_sha256": digest(
                json.dumps(
                    tables, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ),
            "tables": tables,
            "objects": list(objects.values()),
        }

    def call(self, data=None, *, key="1" * 32, path="/api/import", operator=True):
        headers = {"Idempotency-Key": key}
        if operator:
            headers["X-Operator-Key"] = "fixture"
        return asyncio.run(
            self.api.authorized(
                SimpleNamespace(headers=headers),
                path,
                "GET" if data is None else "POST",
                data or {},
            )
        )

    def test_import_reopen_receipt_and_same_request_do_not_duplicate_rows(self):
        result = self.call(copy.deepcopy(self.data))
        self.assertEqual(result.value["revision"], 6)
        self.assertFalse(result.value["existing_rows_replaced"])
        self.assertFalse(result.value["publication_approval_created"])
        self.assertFalse(result.value["historical_render_media_operational"])
        self.assertEqual(result.value["historical_render_archive_mapping"], [])
        reopened = self.call(path="/api/imports/" + "1" * 32)
        self.assertEqual(reopened.value, result.value)
        repeated = self.call(copy.deepcopy(self.data))
        self.assertEqual(repeated.value, result.value)
        self.assertEqual(
            self.api.rows("SELECT revision FROM checkpoint")[0]["revision"], 6
        )
        self.assertEqual(len(self.api.rows("SELECT * FROM receipts")), 1)
        root, _ = self.api.load()
        try:
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                validate(db)
                self.assertEqual(
                    db.execute("SELECT count(*) FROM project_references").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM sources").fetchone()[0], 1
                )
                row = db.execute(
                    "SELECT storage_key,sha256 FROM asset_files"
                ).fetchone()
                self.assertEqual(row[0], "storage/originals/" + row[1])
                self.assertEqual(
                    (root / row[0]).read_bytes(), self.fixture.source.encode()
                )
        finally:
            self.api.discard_projection(root)

    def test_operator_revision_backup_jobs_and_unknown_receipt_are_explicit(self):
        self.assertEqual(
            self.call(copy.deepcopy(self.data), operator=False).status, 403
        )
        self.assertEqual(self.call(path="/api/imports/" + "2" * 32).status, 404)
        for damage in ("revision", "backup", "job"):
            data = copy.deepcopy(self.data)
            if damage == "revision":
                data["expected_revision"] = 4
            elif damage == "backup":
                data["backup_id"] = "e" * 32
            else:
                self.coord.execute(
                    "INSERT INTO jobs(id,status) VALUES('running','RUNNING')"
                )
                self.coord.commit()
            with self.assertRaises(WebInputError):
                self.call(data)
            self.assertEqual(
                self.api.rows("SELECT revision FROM checkpoint")[0]["revision"], 5
            )
            self.assertEqual(self.api.rows("SELECT * FROM objects"), [])
        self.assertEqual(self.api.rows("SELECT * FROM receipts"), [])

    def test_bad_media_or_late_domain_failure_roll_back_coordination_state(self):
        for damage in ("digest", "missing", "domain"):
            data = copy.deepcopy(self.data)
            if damage == "digest":
                data["objects"][0]["content_base64"] = base64.b64encode(
                    b"corrupt"
                ).decode()
            elif damage == "missing":
                data["objects"] = []
            else:
                data["tables"]["assets"][0]["media_type"] = "INVALID"
                data["source_rows_sha256"] = hashlib.sha256(
                    json.dumps(
                        data["tables"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            with self.assertRaises((WebInputError, ValueError)):
                self.call(data)
            self.assertEqual(self.api.rows("SELECT * FROM objects"), [])
            self.assertEqual(self.api.rows("SELECT * FROM object_chunks"), [])
            self.assertEqual(self.api.rows("SELECT * FROM receipts"), [])
            self.assertEqual(
                self.api.rows("SELECT revision FROM checkpoint")[0]["revision"], 5
            )

    def test_ten_mib_backup_fits_body_and_restores_without_capacity_bypass(self):
        chunks = [b"a" * (8 * 1024 * 1024), b"b" * (2 * 1024 * 1024)]
        with self.coord:
            for raw in chunks:
                self.api.put_object(hashlib.sha256(raw).hexdigest(), raw)
        with self.assertRaises(WebInputError):
            with self.coord:
                self.api.put_object(hashlib.sha256(b"extra").hexdigest(), b"extra")
        backup = self.call(path="/api/backup").value
        restore = {
            "confirm_data_after_backup_may_be_lost": True,
            "expected_revision": 5,
            "backup": backup,
        }
        self.assertLess(len(json.dumps(restore).encode()), 16 * 1024 * 1024)
        result = self.call(restore, path="/api/restore")
        self.assertEqual(result.value["revision"], 6)
        for raw in chunks:
            self.assertEqual(
                self.api.object_bytes(hashlib.sha256(raw).hexdigest()), raw
            )
        self.assertEqual(len(self.api.rows("SELECT * FROM objects")), 2)

    def test_schema10_restore_requires_explicit_upgrade_and_preserves_rollback(self):
        legacy = self.root / "legacy.sqlite3"
        with (
            patch.object(database, "MIGRATIONS", database.MIGRATIONS[:10]),
            patch.object(database, "SCHEMA_VERSION", 10),
            patch.object(
                database,
                "validate",
                side_effect=lambda db: validate(db, expected_version=10),
            ),
        ):
            initialize(legacy)
            legacy_root = legacy.parent / "legacy-data"
            legacy_root.mkdir()
            legacy.replace(legacy_root / "creator_loop.sqlite3")
            project = WebDomain(legacy_root).project("Nguồn trước nâng schema")[
                "project_id"
            ]
        with closing(_connect_write(legacy_root / "creator_loop.sqlite3")) as db:
            raw = db.serialize()
        backup = {
            "format": "CREATOR_WEB_BACKUP_V1",
            "version": self.call(path="/api/backup").value["version"],
            "checkpoint": [
                {
                    "payload": base64.b64encode(zlib.compress(raw)).decode(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            ],
            "objects": [],
        }
        data = {
            "backup": backup,
            "expected_revision": 5,
            "confirm_data_after_backup_may_be_lost": True,
        }
        before = self.api.rows("SELECT * FROM checkpoint")
        with self.assertRaisesRegex(WebInputError, "schema10"):
            self.call(data, path="/api/restore")
        self.assertEqual(self.api.rows("SELECT * FROM checkpoint"), before)
        self.assertEqual(self.api.rows("SELECT * FROM control"), [])
        data["confirm_schema10_additive_upgrade"] = True
        result = self.call(data, path="/api/restore")
        self.assertTrue(result.value["restored"])
        root, revision = self.api.load()
        self.assertEqual(revision, 6)
        with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
            validate(db)
            self.assertEqual(
                db.execute("SELECT project_id FROM projects").fetchone()[0], project
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM project_material_use_versions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
        self.assertEqual(
            json.loads(self.api.rows("SELECT record FROM control")[0]["record"]), before
        )

    def test_migration_preimage_is_recoverable_without_deleting_full_backup_slots(self):
        legacy = self.root / "legacy-pin.sqlite3"
        with (
            patch.object(database, "MIGRATIONS", database.MIGRATIONS[:10]),
            patch.object(database, "SCHEMA_VERSION", 10),
            patch.object(
                database,
                "validate",
                side_effect=lambda db: validate(db, expected_version=10),
            ),
        ):
            initialize(legacy)
        with closing(_connect_write(legacy)) as db:
            raw = db.serialize()
        with self.coord:
            self.coord.execute(
                "UPDATE checkpoint SET payload=?,sha256=?",
                (
                    base64.b64encode(zlib.compress(raw)).decode(),
                    hashlib.sha256(raw).hexdigest(),
                ),
            )
            for key in ("c" * 32, "d" * 32):
                self.coord.execute("INSERT INTO backups VALUES(?,?)", (key, "{}"))
            self.api.put_object(hashlib.sha256(b"media").hexdigest(), b"media")
        before = self.call(path="/api/backup").value
        old_slots = self.api.rows("SELECT * FROM backups")
        data = {
            "expected_revision": 5,
            "expected_checkpoint_sha256": before["checkpoint"][0]["sha256"],
            "backup_mode": "PIN_CURRENT_PREIMAGE",
            "target_schema": 11,
            "confirm_additive_source_use_migration": True,
        }
        self.assertEqual(
            self.call(data, path="/api/schema-migrate", operator=False).status, 403
        )
        with patch.dict(
            self.api.authorized.__globals__,
            {
                "_migrate_locked": lambda *args: (_ for _ in ()).throw(
                    RuntimeError("failure after pin")
                )
            },
        ):
            with self.assertRaises(RuntimeError):
                self.call(data, path="/api/schema-migrate")
        self.assertEqual(self.api.rows("SELECT * FROM control"), [])
        self.assertEqual(self.call(path="/api/backup").value, before)
        result = self.call(data, path="/api/schema-migrate").value
        self.assertEqual(self.call(data, path="/api/schema-migrate").value, result)
        self.assertEqual(self.api.rows("SELECT * FROM backups"), old_slots)
        self.assertEqual(len(self.api.rows("SELECT * FROM control")), 1)
        pin_path = result["preimage_backup_path"]
        self.assertEqual(self.call(path=pin_path, operator=False).status, 403)
        pinned = self.call(path=pin_path).value
        self.assertEqual(pinned, before)
        restore = {
            "backup": pinned,
            "expected_revision": 6,
            "confirm_data_after_backup_may_be_lost": True,
            "confirm_schema10_additive_upgrade": True,
        }
        self.assertTrue(
            self.call(restore, key="2" * 32, path="/api/restore").value["restored"]
        )
        after = self.call(path="/api/backup").value
        self.assertEqual(after["objects"], before["objects"])
        self.assertEqual(self.api.rows("SELECT * FROM backups"), old_slots)

    def test_shared_source_active_in_other_project_blocks_correction_and_choice(self):
        with self.coord:
            self.coord.execute(
                "INSERT INTO jobs VALUES(?,?,?)",
                (
                    "job",
                    "RUNNING",
                    json.dumps(
                        {
                            "request": {"project_id": "other"},
                            "sources": [{"file_id": "shared"}],
                        }
                    ),
                ),
            )
        before = self.api.rows("SELECT * FROM checkpoint")
        for path in ("/api/source-ranges", "/api/source-use"):
            with self.assertRaisesRegex(WebInputError, "nguồn dùng chung"):
                self.call({"project_id": "new", "file_id": "shared"}, path=path)
        self.assertEqual(self.api.rows("SELECT * FROM checkpoint"), before)
        self.assertEqual(self.api.rows("SELECT * FROM receipts"), [])

    def test_object_count_limit_keeps_existing_bytes(self):
        with self.coord:
            for index in range(512):
                raw = b"fixture" + index.to_bytes(2, "big")
                self.api.put_object(hashlib.sha256(raw).hexdigest(), raw)
        with self.assertRaisesRegex(WebInputError, "512"):
            with self.coord:
                self.api.put_object(hashlib.sha256(b"extra").hexdigest(), b"extra")
        self.assertEqual(len(self.api.rows("SELECT * FROM objects")), 512)
        first = b"fixture\0\0"
        self.assertEqual(
            self.api.object_bytes(hashlib.sha256(first).hexdigest()), first
        )
