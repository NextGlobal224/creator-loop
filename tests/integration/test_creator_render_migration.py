import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.backend.creator.drafts import create_draft
from creator_loop.backend.runtime.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    _connect_write,
    initialize,
    migration_path,
    validate,
)


class CreatorRenderMigrationTests(unittest.TestCase):
    def test_every_supported_schema_upgrade_preserves_data_and_backup(self):
        for version in range(len(MIGRATIONS)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "dữ liệu cũ" / "creator_loop.sqlite3"
                path.parent.mkdir()
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA foreign_keys=ON")
                    for migration in MIGRATIONS[:version]:
                        sql = migration_path(migration).read_text(encoding="utf-8")
                        db.executescript(sql)
                        db.execute(
                            "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                            (
                                migration,
                                hashlib.sha256(sql.encode()).hexdigest(),
                                "now",
                                "0.1.0",
                            ),
                        )
                    db.execute(f"PRAGMA user_version={version}")
                    if version:
                        db.execute(
                            "INSERT INTO sources VALUES('s','FACEBOOK',NULL,'prior',NULL,NULL,NULL,'UNKNOWN','now')"
                        )
                    db.commit()
                initialize(path)
                with closing(_connect_write(path)) as db:
                    validate(db)
                    if version:
                        self.assertEqual(
                            db.execute(
                                "SELECT external_id FROM sources WHERE source_id='s'"
                            ).fetchone(),
                            ("prior",),
                        )
                self.assertEqual(
                    len(list(path.parent.glob(f"*.pre-v{SCHEMA_VERSION}-*.sqlite3"))),
                    int(version > 0),
                )

    def test_render_seal_lineage_and_incomplete_output_are_guarded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "creator_loop.sqlite3"
            initialize(path)
            with closing(_connect_write(path)) as db:
                db.execute(
                    "INSERT INTO projects VALUES('p','Topic','ACTIVE','now',NULL)"
                )
                db.commit()
                draft = create_draft(
                    db,
                    project_id="p",
                    body_text="Sự thật cần căn cứ.",
                    format="VIDEO",
                    actor="machine",
                )
                db.execute("INSERT INTO assets VALUES('a','TEXT','script','now',NULL)")
                db.execute(
                    "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES('f','a','ORIGINAL','storage/originals/script.txt',?,10,'text/plain','now')",
                    ("a" * 64,),
                )
                db.execute(
                    "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,created_at) VALUES('run','a','f','DRAFT_VIDEO_RENDER','QUEUED','ffmpeg','test','now')"
                )
                db.execute(
                    "INSERT INTO draft_render_requests VALUES('run',?,'{}','fp',NULL)",
                    (draft.draft_version_id,),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO draft_render_inputs VALUES('run',0,'f',?,'SCRIPT')",
                        ("b" * 64,),
                    )
                db.execute(
                    "INSERT INTO draft_render_inputs VALUES('run',0,'f',?,'SCRIPT')",
                    ("a" * 64,),
                )
                db.execute(
                    "UPDATE draft_render_requests SET sealed_at='now' WHERE render_run_id='run'"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO draft_render_inputs VALUES('run',1,'f',?,'SCRIPT')",
                        ("a" * 64,),
                    )
                output = (
                    "run",
                    "storage/derived/output.mp4",
                    "c" * 64,
                    1000,
                    "video/mp4",
                    3000,
                    360,
                    640,
                    "ffmpeg",
                    "test",
                    "now",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO draft_render_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        output,
                    )
                db.execute(
                    "UPDATE processing_runs SET status='SUCCEEDED',finished_at='now' WHERE run_id='run'"
                )
                db.execute(
                    "INSERT INTO draft_render_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    output,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE draft_render_outputs SET sha256=?", ("d" * 64,))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT OR REPLACE INTO draft_render_requests VALUES('run',?,'{}','changed','now')",
                        (draft.draft_version_id,),
                    )
                self.assertEqual(
                    db.execute(
                        "SELECT parent_file_id FROM asset_files WHERE file_id='f'"
                    ).fetchone(),
                    (None,),
                )
                db.commit()
                validate(db)


if __name__ == "__main__":
    unittest.main()
