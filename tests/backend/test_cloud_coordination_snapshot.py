"""Offline SQLite fixture only; not Cloudflare backup or cutover proof."""

import base64
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from creator_loop.backend.runtime.cloud_coordination_snapshot import (
    capture,
    capture_parts,
    capture_pinned,
    restore_empty,
    restore_parts,
    validate,
    verify_restored,
)
from creator_loop.backend.runtime.database import initialize


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    for statement in (
        "CREATE TABLE checkpoint(id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT, sha256 TEXT)",
        "CREATE TABLE objects(sha256 TEXT PRIMARY KEY,size INTEGER)",
        "CREATE TABLE object_chunks(sha256 TEXT,position INTEGER,content TEXT,PRIMARY KEY(sha256,position))",
        "CREATE TABLE receipts(id TEXT PRIMARY KEY,fingerprint TEXT,response TEXT)",
        "CREATE TABLE backups(id TEXT PRIMARY KEY,created_at REAL,record TEXT)",
        "CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,record TEXT)",
        "CREATE TABLE control(id TEXT PRIMARY KEY,record TEXT)",
        "CREATE TABLE preservation_archives(sha256 TEXT PRIMARY KEY,record TEXT)",
        "CREATE TABLE preservation_chunks(sha256 TEXT,position INTEGER,content TEXT,PRIMARY KEY(sha256,position))",
    ):
        db.execute(statement)
    return db


def row_reader(db):
    return lambda statement, *args: [dict(row) for row in db.execute(statement, args)]


class CoordinationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.source, self.target = database(), database()
        self.addCleanup(self.source.close)
        self.addCleanup(self.target.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        domain_path = Path(temporary.name) / "domain.sqlite3"
        initialize(domain_path)
        domain = sqlite3.connect(domain_path)
        media = b"fixture media"
        media_hash = hashlib.sha256(media).hexdigest()
        domain.execute(
            "INSERT INTO assets(asset_id,media_type,display_name,created_at) VALUES('a','TEXT','fixture','now')"
        )
        domain.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES('f','a','ORIGINAL',?,?,?,'text/plain','now')",
            ("storage/originals/" + media_hash, media_hash, len(media)),
        )
        domain.commit()
        raw = domain.serialize()
        domain.close()
        self.source.execute(
            "INSERT INTO checkpoint VALUES(1,7,?,?)",
            (
                base64.b64encode(zlib.compress(raw)).decode(),
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        self.source.execute("INSERT INTO objects VALUES(?,?)", (media_hash, len(media)))
        self.source.execute(
            "INSERT INTO object_chunks VALUES(?,0,?)",
            (media_hash, base64.b64encode(media).decode()),
        )
        self.source.execute(
            "INSERT INTO jobs VALUES('a','CANCELLED',?)",
            (json.dumps({"id": "a", "status": "CANCELLED"}),),
        )
        self.source.execute(
            "INSERT INTO jobs VALUES('b','QUEUED',?)",
            (json.dumps({"id": "b", "status": "QUEUED"}),),
        )
        self.source.execute(
            "INSERT INTO control VALUES('grant',?)",
            (json.dumps({"reservation": "reserved"}),),
        )
        self.source.execute(
            "INSERT INTO receipts VALUES('r','hash',?)",
            (json.dumps({"state": "unknown"}),),
        )
        backup = {
            "format": "CREATOR_WEB_BACKUP_V1",
            "version": "B-web/0.2.0-beta.1",
            "checkpoint": row_reader(self.source)("SELECT * FROM checkpoint"),
            "objects": row_reader(self.source)("SELECT * FROM objects"),
        }
        self.source.execute(
            "INSERT INTO backups VALUES('historic',1.0,?)", (json.dumps(backup),)
        )
        self.source.commit()

    def snap(self):
        self.source.execute("BEGIN")
        try:
            return capture(row_reader(self.source))
        finally:
            self.source.rollback()

    def restore(self, snapshot):
        with self.target:
            restore_empty(
                snapshot,
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
            )

    def test_atomic_snapshot_is_pinned_and_restores_only_snapshot(self):
        snap = self.snap()
        self.source.execute(
            "INSERT INTO control VALUES('late-source-a','late response')"
        )
        self.restore(snap)
        self.assertEqual(
            row_reader(self.target)("SELECT * FROM control"),
            [{"id": "grant", "record": json.dumps({"reservation": "reserved"})}],
        )
        self.assertEqual(snap["late_writes_after_snapshot"], "NOT_CAPTURED")
        self.assertEqual(snap["alarm"], "NOT_CAPTURED")
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2
        )
        self.assertEqual(
            self.source.execute("SELECT COUNT(*) FROM control").fetchone()[0], 2
        )

    def test_rejects_corruption_and_nonempty_target(self):
        snap = self.snap()
        damaged = copy.deepcopy(snap)
        damaged["tables"]["control"]["rows"][0]["record"] = "changed"
        with self.assertRaises(ValueError):
            self.restore(damaged)
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )
        self.target.execute("INSERT INTO control VALUES('newer','do not replace')")
        self.target.commit()
        with self.assertRaises(ValueError):
            self.restore(snap)
        self.assertEqual(
            self.target.execute(
                "SELECT record FROM control WHERE id='newer'"
            ).fetchone()[0],
            "do not replace",
        )

    def test_rehashed_manifest_does_not_hide_corrupt_media(self):
        snap = self.snap()
        table = snap["tables"]["object_chunks"]
        table["rows"][0]["content"] = base64.b64encode(b"corrupt media").decode()
        table["sha256"] = hashlib.sha256(
            json.dumps(
                table["rows"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "media digest"):
            self.restore(snap)
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )

    def test_verified_archive_requires_all_matching_chunks(self):
        content = b"canonical fixture"
        key = hashlib.sha256(content).hexdigest()
        record = {
            "sha256": key,
            "bytes": len(content),
            "chunk_hashes": [key],
            "status": "VERIFIED",
            "source_commit": "a" * 40,
        }
        self.source.execute(
            "INSERT INTO preservation_archives VALUES(?,?)", (key, json.dumps(record))
        )
        self.source.execute(
            "INSERT INTO preservation_chunks VALUES(?,0,?)",
            (key, base64.b64encode(content).decode()),
        )
        self.source.commit()
        snap = self.snap()
        validate(snap)
        table = snap["tables"]["preservation_chunks"]
        table["rows"] = []
        table["count"] = 0
        table["sha256"] = hashlib.sha256(b"[]").hexdigest()
        with self.assertRaisesRegex(ValueError, "archive incomplete"):
            self.restore(snap)

    def test_missing_media_referenced_by_checkpoint_is_refused(self):
        snap = self.snap()
        for name in ("objects", "object_chunks"):
            table = snap["tables"][name]
            table.update(rows=[], count=0, sha256=hashlib.sha256(b"[]").hexdigest())
        with self.assertRaisesRegex(ValueError, "checkpoint references missing media"):
            self.restore(snap)
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )

    def test_read_transaction_excludes_receipt_committed_mid_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "fixture.sqlite3")
            reader = sqlite3.connect(path)
            writer = sqlite3.connect(path)
            try:
                self.source.backup(reader)
                reader.execute("PRAGMA journal_mode=WAL")
                reader.row_factory = sqlite3.Row
                reader.execute("BEGIN")
                inserted = False

                def rows(sql, *args):
                    nonlocal inserted
                    result = [dict(row) for row in reader.execute(sql, args)]
                    if not inserted and sql.startswith(
                        "SELECT id,revision,payload,sha256 FROM checkpoint"
                    ):
                        writer.execute(
                            "INSERT INTO control VALUES('late-source-concurrent','late')"
                        )
                        writer.commit()
                        inserted = True
                    return result

                snap = capture(rows)
                reader.rollback()
                self.assertTrue(inserted)
                self.assertEqual(
                    [row["id"] for row in snap["tables"]["control"]["rows"]], ["grant"]
                )
                self.assertEqual(
                    reader.execute("SELECT COUNT(*) FROM control").fetchone()[0], 2
                )
                self.assertEqual(snap["late_writes_after_snapshot"], "NOT_CAPTURED")
            finally:
                reader.close()
                writer.close()

    def test_size_and_scope_refuse_instead_of_truncate(self):
        with self.assertRaises(ValueError):
            capture(row_reader(self.source), limit=10)
        snap = self.snap()
        snap["alarm"] = "CAPTURED"
        with self.assertRaises(ValueError):
            validate(snap)

    def test_insert_failure_rolls_back_all_tables(self):
        snap = self.snap()
        snap["tables"]["jobs"]["rows"].append(
            copy.deepcopy(snap["tables"]["jobs"]["rows"][0])
        )
        table = snap["tables"]["jobs"]
        table["count"] = len(table["rows"])
        import hashlib

        table["sha256"] = hashlib.sha256(
            json.dumps(
                table["rows"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.restore(snap)
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
        )

    def test_part_stream_round_trip_and_corruption_rolls_back(self):
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        bad = parts.copy()
        bad[-1] = bad[-1] + b"x"
        with self.assertRaisesRegex(ValueError, "part mismatch"):
            with self.target:
                restore_parts(
                    manifest,
                    lambda i: bad[i],
                    row_reader(self.target),
                    lambda sql, *args: self.target.execute(sql, args),
                    verify=lambda: verify_restored(row_reader(self.target)),
                )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )
        with self.target:
            restore_parts(
                manifest,
                lambda i: parts[i],
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
                verify=lambda: verify_restored(row_reader(self.target)),
            )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2
        )
        self.assertEqual(
            json.loads(
                self.target.execute(
                    "SELECT record FROM control WHERE id='grant'"
                ).fetchone()[0]
            ),
            {"reservation": "reserved"},
        )
        self.assertEqual(manifest["alarm"], "NOT_CAPTURED")

    def test_stream_roundtrips_large_escape_expansion_without_unbounded_line(self):
        original = "\0" * 800000
        self.source.execute(
            "INSERT INTO control VALUES('control-characters',?)", (original,)
        )
        self.source.commit()
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        self.assertEqual(manifest["format"], "B_COORDINATION_STREAM_V2")
        self.assertLess(max(map(len, b"".join(parts).splitlines())), 4 * 1024 * 1024)
        with self.target:
            restore_parts(
                manifest,
                lambda i: parts[i],
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
                verify=lambda: None,
            )
        self.assertEqual(
            self.target.execute(
                "SELECT record FROM control WHERE id='control-characters'"
            ).fetchone()[0],
            original,
        )

    def test_stream_v2_preserves_large_valid_control_json_with_semantic_check(self):
        original = json.dumps({"text": "\n" * 400000})
        self.source.execute("INSERT INTO control VALUES('large-json',?)", (original,))
        self.source.commit()
        parts = []
        with self.source:
            manifest = capture_parts(
                row_reader(self.source), lambda i, part: parts.append(part)
            )
        with self.target:
            restore_parts(
                manifest,
                lambda i: parts[i],
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
                verify=lambda: verify_restored(row_reader(self.target)),
            )
        self.assertEqual(
            self.target.execute(
                "SELECT record FROM control WHERE id='large-json'"
            ).fetchone()[0],
            original,
        )

    def test_stream_v1_fixture_restores_with_semantic_check(self):
        from creator_loop.backend.runtime.cloud_coordination_snapshot import TABLES

        lines = []
        counts = {}
        for table, columns in TABLES.items():
            keys = (
                columns[:2]
                if table in ("object_chunks", "preservation_chunks")
                else columns[:1]
            )
            records = row_reader(self.source)(
                "SELECT "
                + ",".join(columns)
                + " FROM "
                + table
                + " ORDER BY "
                + ",".join(keys)
            )
            counts[table] = len(records)
            lines.extend(
                json.dumps(
                    {"table": table, "row": row},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                + b"\n"
                for row in records
            )
        payload = b"".join(lines)
        parts = [payload[i : i + 262144] for i in range(0, len(payload), 262144)]
        manifest = {
            "format": "B_COORDINATION_STREAM_V1",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "parts": [
                {
                    "position": i,
                    "bytes": len(part),
                    "sha256": hashlib.sha256(part).hexdigest(),
                }
                for i, part in enumerate(parts)
            ],
            "counts": counts,
            "alarm": "NOT_CAPTURED",
            "late_writes_after_snapshot": "NOT_CAPTURED",
            "sessions": "EXCLUDED",
            "backup_history": "INCLUDED",
        }
        with self.target:
            restore_parts(
                manifest,
                lambda i: parts[i],
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
                verify=lambda: verify_restored(row_reader(self.target)),
            )
        self.assertEqual(
            self.target.execute("SELECT count(*) FROM jobs").fetchone()[0], 2
        )

    def test_stream_v2_invalid_encoded_fields_roll_back(self):
        self.source.execute(
            "INSERT INTO control VALUES('encoded',?)", (json.dumps({"x": '"'}),)
        )
        self.source.commit()
        captured = []
        with self.source:
            manifest = capture_parts(
                row_reader(self.source), lambda i, part: captured.append(part)
            )
        records = [json.loads(line) for line in b"".join(captured).splitlines()]
        for mutation in ("duplicate", "unknown", "base64", "utf8", "v1"):
            modified = copy.deepcopy(records)
            row = next(
                r
                for r in modified
                if r["table"] == "control" and r["row"]["id"] == "encoded"
            )
            if mutation == "duplicate":
                row["base64_fields"].append(row["base64_fields"][0])
            elif mutation == "unknown":
                row["base64_fields"].append("not_a_column")
            elif mutation == "base64":
                row["row"]["record"] = "*"
            elif mutation == "utf8":
                row["row"]["record"] = "/w=="
            payload = b"".join(
                json.dumps(
                    r, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                + b"\n"
                for r in modified
            )
            parts = [payload[i : i + 262144] for i in range(0, len(payload), 262144)]
            candidate = copy.deepcopy(manifest)
            candidate.update(
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                parts=[
                    {
                        "position": i,
                        "bytes": len(part),
                        "sha256": hashlib.sha256(part).hexdigest(),
                    }
                    for i, part in enumerate(parts)
                ],
            )
            if mutation == "v1":
                candidate["format"] = "B_COORDINATION_STREAM_V1"
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "invalid encoded stream"),
            ):
                with self.target:
                    restore_parts(
                        candidate,
                        lambda i: parts[i],
                        row_reader(self.target),
                        lambda sql, *args: self.target.execute(sql, args),
                        verify=lambda: None,
                    )
            self.assertEqual(
                self.target.execute("SELECT count(*) FROM checkpoint").fetchone()[0], 0
            )

    def test_pinned_parts_use_separate_tables_and_failure_rolls_back(self):
        self.source.execute(
            "CREATE TABLE snapshot_parts(snapshot_id TEXT,position INTEGER,content TEXT,PRIMARY KEY(snapshot_id,position))"
        )
        self.source.execute(
            "CREATE TABLE snapshot_manifests(id TEXT PRIMARY KEY,record TEXT)"
        )
        self.source.execute("INSERT INTO control VALUES('large',?)", ("x" * 600000,))
        self.source.commit()
        key = "a" * 32
        with self.assertRaisesRegex(ValueError, "exceeds bound"):
            with self.source:
                self.source.execute("BEGIN IMMEDIATE")
                capture_pinned(
                    row_reader(self.source),
                    lambda sql, *args: self.source.execute(sql, args),
                    key,
                    max_bytes=300000,
                )
        self.assertEqual(
            self.source.execute("SELECT COUNT(*) FROM snapshot_parts").fetchone()[0], 0
        )
        self.assertEqual(
            self.source.execute("SELECT COUNT(*) FROM snapshot_manifests").fetchone()[
                0
            ],
            0,
        )
        with self.source:
            self.source.execute("BEGIN IMMEDIATE")
            manifest = capture_pinned(
                row_reader(self.source),
                lambda sql, *args: self.source.execute(sql, args),
                key,
            )
        self.assertEqual(manifest["counts"]["control"], 2)
        self.assertGreater(
            self.source.execute("SELECT COUNT(*) FROM snapshot_parts").fetchone()[0], 1
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            with self.source:
                self.source.execute("BEGIN IMMEDIATE")
                capture_pinned(
                    row_reader(self.source),
                    lambda sql, *args: self.source.execute(sql, args),
                    key,
                )

    def test_semantic_verification_failure_rolls_back_stream_restore(self):
        self.source.execute(
            "UPDATE jobs SET record=? WHERE id='a'",
            (json.dumps({"id": "a", "status": "RUNNING"}),),
        )
        self.source.commit()
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        with self.assertRaisesRegex(ValueError, "job identity or status mismatch"):
            with self.target:
                restore_parts(
                    manifest,
                    lambda i: parts[i],
                    row_reader(self.target),
                    lambda sql, *args: self.target.execute(sql, args),
                    verify=lambda: verify_restored(row_reader(self.target)),
                )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )

    def test_stream_semantic_verifier_rejects_rehashed_corrupt_media(self):
        self.source.execute(
            "UPDATE object_chunks SET content=?",
            (base64.b64encode(b"bad media").decode(),),
        )
        self.source.commit()
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        with self.assertRaisesRegex(ValueError, "media digest"):
            with self.target:
                restore_parts(
                    manifest,
                    lambda i: parts[i],
                    row_reader(self.target),
                    lambda sql, *args: self.target.execute(sql, args),
                    verify=lambda: verify_restored(row_reader(self.target)),
                )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )

    def test_stream_verifier_rejects_corrupt_historical_backup(self):
        row = json.loads(
            self.source.execute(
                "SELECT record FROM backups WHERE id='historic'"
            ).fetchone()[0]
        )
        row["checkpoint"][0]["sha256"] = "0" * 64
        self.source.execute(
            "UPDATE backups SET record=? WHERE id='historic'", (json.dumps(row),)
        )
        self.source.commit()
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        with self.assertRaisesRegex(ValueError, "checkpoint hash"):
            with self.target:
                restore_parts(
                    manifest,
                    lambda i: parts[i],
                    row_reader(self.target),
                    lambda sql, *args: self.target.execute(sql, args),
                    verify=lambda: verify_restored(row_reader(self.target)),
                )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 0
        )

    def test_historical_media_verifier_reads_chunks_bounded_and_rejects_missing(self):
        content = b"x" * 120000
        key = hashlib.sha256(content).hexdigest()
        self.source.execute("INSERT INTO objects VALUES(?,?)", (key, len(content)))
        for index, offset in enumerate(range(0, len(content), 48000)):
            self.source.execute(
                "INSERT INTO object_chunks VALUES(?,?,?)",
                (
                    key,
                    index,
                    base64.b64encode(content[offset : offset + 48000]).decode(),
                ),
            )
        backup = json.loads(
            self.source.execute(
                "SELECT record FROM backups WHERE id='historic'"
            ).fetchone()[0]
        )
        backup["objects"].append({"sha256": key, "size": len(content)})
        self.source.execute(
            "UPDATE backups SET record=? WHERE id='historic'", (json.dumps(backup),)
        )

        def bounded(sql, *args):
            if "FROM object_chunks" in sql and "LIMIT 1" not in sql:
                raise AssertionError("unbounded media read")
            return row_reader(self.source)(sql, *args)

        verify_restored(bounded)
        backup["objects"].append({"sha256": "f" * 64, "size": 1})
        self.source.execute(
            "UPDATE backups SET record=? WHERE id='historic'", (json.dumps(backup),)
        )
        with self.assertRaisesRegex(
            ValueError, "historical backup references missing media"
        ):
            verify_restored(bounded)

    def test_null_primary_key_cannot_hide_later_jobs_during_verify(self):
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        lines = []
        for raw in b"".join(parts).splitlines():
            record = json.loads(raw)
            if record["table"] == "jobs" and record["row"]["id"] == "a":
                record["row"]["id"] = None
            lines.append(
                json.dumps(
                    record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                + b"\n"
            )
        payload = b"".join(lines)
        parts = [payload[i : i + 262144] for i in range(0, len(payload), 262144)]
        manifest["parts"] = [
            {
                "position": i,
                "bytes": len(part),
                "sha256": hashlib.sha256(part).hexdigest(),
            }
            for i, part in enumerate(parts)
        ]
        manifest["bytes"] = len(payload)
        manifest["sha256"] = hashlib.sha256(payload).hexdigest()
        with self.assertRaisesRegex(ValueError, "invalid snapshot primary key"):
            with self.target:
                restore_parts(
                    manifest,
                    lambda i: parts[i],
                    row_reader(self.target),
                    lambda sql, *args: self.target.execute(sql, args),
                    verify=lambda: verify_restored(row_reader(self.target)),
                )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
        )

    def test_stream_handles_synthetic_payload_over_old_32_mib_bound(self):
        raw = b"a" * 524288
        count = 56
        sha = hashlib.sha256()
        for _ in range(count):
            sha.update(raw)
        archive_hash = sha.hexdigest()
        chunk_hash = hashlib.sha256(raw).hexdigest()
        archive = {
            "sha256": archive_hash,
            "bytes": len(raw) * count,
            "chunk_hashes": [chunk_hash] * count,
            "status": "VERIFIED",
            "source_commit": "a" * 40,
        }
        self.source.execute(
            "INSERT INTO preservation_archives VALUES(?,?)",
            (archive_hash, json.dumps(archive)),
        )
        text = base64.b64encode(raw).decode()
        self.source.executemany(
            "INSERT INTO preservation_chunks VALUES(?,?,?)",
            ((archive_hash, i, text) for i in range(count)),
        )
        self.source.commit()
        # Synthetic archive; no real user data or Cloudflare calls.
        parts = []
        self.source.execute("BEGIN")
        try:
            manifest = capture_parts(
                row_reader(self.source), lambda index, data: parts.append(data)
            )
        finally:
            self.source.rollback()
        self.assertGreater(manifest["bytes"], 32 * 1024 * 1024)
        self.assertEqual(sum(map(len, parts)), manifest["bytes"])
        self.assertTrue(all(len(data) <= 256 * 1024 for data in parts))
        with self.target:
            restore_parts(
                manifest,
                lambda i: parts[i],
                row_reader(self.target),
                lambda sql, *args: self.target.execute(sql, args),
                verify=lambda: verify_restored(row_reader(self.target)),
            )
        self.assertEqual(
            self.target.execute("SELECT COUNT(*) FROM preservation_chunks").fetchone()[
                0
            ],
            count,
        )


if __name__ == "__main__":
    unittest.main()
