import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from creator_loop.database import (
    _connect_write,
    backup,
    initialize,
    migration_path,
    open_readonly,
    validate,
)
from creator_loop.locator import validate_locator
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.paths import data_root

T = "2026-09-29T00:00:00Z"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = (
            Path(self.tmp.name) / "thÆ° má»¥c cÃ³ dáº¥u" / "creator_loop.sqlite3"
        )
        initialize(self.path)
        self.db = _connect_write(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)

    def seed(self):
        d = self.db
        d.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)", ("a1", "VIDEO", "test", T, None)
        )
        d.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "f1",
                "a1",
                "ORIGINAL",
                "storage/originals/a.mp4",
                "abc",
                1,
                "video/mp4",
                T,
            ),
        )
        d.execute(
            "INSERT INTO evidences VALUES (?,?,?,?,?)", ("e1", "a1", "SPEECH", T, None)
        )
        d.execute(
            "INSERT INTO evidence_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ev1",
                "e1",
                "a1",
                1,
                "f1",
                "hello",
                "TIME_RANGE",
                '{"start_ms":0,"end_ms":1000,"track":"audio"}',
                "HUMAN",
                None,
                "owner",
                T,
            ),
        )
        d.execute("INSERT INTO claims VALUES (?,?,?,?)", ("c1", "FACTUAL", T, None))
        d.execute(
            "INSERT INTO claim_versions VALUES (?,?,?,?,?,?)",
            ("cv1", "c1", 1, "hello", "owner", T),
        )
        d.execute(
            "INSERT INTO claim_evidence VALUES (?,?,?)", ("cv1", "ev1", "SUPPORTS")
        )
        d.execute("INSERT INTO claim_version_seals VALUES (?,?)", ("cv1", T))
        d.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?)", ("p1", "test", "ACTIVE", T, None)
        )
        d.execute("INSERT INTO drafts VALUES (?,?,?,?)", ("d1", "p1", "ACTIVE", T))
        d.execute(
            "INSERT INTO draft_versions VALUES (?,?,?,?,?,?,?)",
            ("dv1", "d1", 1, "caption", "POST", "owner", T),
        )
        d.commit()

    def test_initial_schema_and_backup(self):
        validate(self.db)

        target = Path(self.tmp.name) / "backup.sqlite3"
        backup(self.path, target)

        copy = sqlite3.connect(target)
        try:
            self.assertEqual(
                copy.execute("PRAGMA user_version").fetchone()[0],
                2,
            )
        finally:
            copy.close()

        self.assertEqual(
            self.db.execute(
                "SELECT length(checksum) FROM schema_migrations"
            ).fetchone()[0],
            64,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],
            2,
        )

    def make_legacy_database(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        sql = migration_path().read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(path)) as legacy:
            legacy.execute("PRAGMA foreign_keys=ON")
            legacy.executescript(sql)
            legacy.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                ("0001_initial", digest, T, "0.1.0"),
            )
            legacy.execute("PRAGMA user_version=1")
            legacy.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("a1", "TEXT", "legacy", T, None),
            )
            legacy.execute(
                """INSERT INTO asset_files
                   (file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("f1", "a1", "ORIGINAL", "legacy.txt", "digest", 1, "text/plain", T),
            )
            legacy.execute(
                "INSERT INTO evidences VALUES (?,?,?,?,?)",
                ("e1", "a1", "DIRECT_TEXT", T, None),
            )
            legacy.execute(
                "INSERT INTO evidence_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ev1",
                    "e1",
                    "a1",
                    1,
                    "f1",
                    "legacy text",
                    "WHOLE_ASSET",
                    "{}",
                    "HUMAN",
                    None,
                    "owner",
                    T,
                ),
            )
            legacy.execute(
                "INSERT INTO claims VALUES (?,?,?,?)",
                ("c1", "FACTUAL", T, None),
            )
            legacy.execute(
                "INSERT INTO claim_versions VALUES (?,?,?,?,?,?)",
                ("cv1", "c1", 1, "legacy claim", "owner", T),
            )
            legacy.execute(
                "INSERT INTO claim_evidence VALUES (?,?,?)",
                ("cv1", "ev1", "SUPPORTS"),
            )
            legacy.execute(
                "INSERT INTO claims VALUES (?,?,?,?)",
                ("c2", "EDITORIAL_HYPOTHESIS", T, None),
            )
            legacy.execute(
                "INSERT INTO claim_versions VALUES (?,?,?,?,?,?)",
                ("cv2", "c2", 1, "uncited legacy claim", "owner", T),
            )
            legacy.commit()
        return path

    def test_v1_upgrade_preserves_and_seals_existing_claims(self):
        legacy = self.make_legacy_database()
        initialize(legacy)
        with closing(_connect_write(legacy)) as upgraded:
            validate(upgraded)
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                upgraded.execute("SELECT count(*) FROM claim_version_seals").fetchone()[
                    0
                ],
                2,
            )
            self.assertEqual(
                upgraded.execute(
                    "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id='cv1'"
                ).fetchone()[0],
                "ev1",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                upgraded.execute(
                    "INSERT INTO claim_evidence VALUES (?,?,?)",
                    ("cv2", "ev1", "CONTEXT"),
                )
        snapshots = list(legacy.parent.glob("legacy.sqlite3.pre-v2-*.sqlite3"))
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as snapshot:
            snapshot.execute("PRAGMA foreign_keys=ON")
            validate(snapshot, expected_version=1)
            self.assertEqual(
                snapshot.execute("SELECT count(*) FROM claim_versions").fetchone()[0],
                2,
            )
        initialize(legacy)
        self.assertEqual(
            len(list(legacy.parent.glob("legacy.sqlite3.pre-v2-*.sqlite3"))), 1
        )

    def test_claim_citations_and_seals_are_immutable(self):
        self.seed()
        self.db.execute(
            "INSERT INTO claims VALUES (?,?,?,?)", ("c2", "FACTUAL", T, None)
        )
        self.db.execute(
            "INSERT INTO claim_versions VALUES (?,?,?,?,?,?)",
            ("cv2", "c2", 1, "new", "owner", T),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO claim_version_seals VALUES (?,?)", ("cv2", T))
        self.db.execute(
            "INSERT INTO claim_evidence VALUES (?,?,?)", ("cv2", "ev1", "SUPPORTS")
        )
        self.db.execute("INSERT INTO claim_version_seals VALUES (?,?)", ("cv2", T))
        for sql in (
            "INSERT INTO claim_evidence VALUES ('cv2','ev1','CONTEXT')",
            "UPDATE claim_evidence SET relation_type='CONTEXT' WHERE claim_version_id='cv2'",
            "DELETE FROM claim_evidence WHERE claim_version_id='cv2'",
            "UPDATE claim_version_seals SET sealed_at='later' WHERE claim_version_id='cv2'",
            "DELETE FROM claim_version_seals WHERE claim_version_id='cv2'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)

    def test_failed_v1_migration_rolls_back_and_keeps_backup(self):
        legacy = self.make_legacy_database()
        with closing(sqlite3.connect(legacy)) as old:
            old.execute("CREATE TABLE claim_version_seals (incompatible TEXT)")
            old.commit()
        with self.assertRaises(sqlite3.OperationalError):
            initialize(legacy)
        with closing(sqlite3.connect(legacy)) as old:
            old.execute("PRAGMA foreign_keys=ON")
            validate(old, expected_version=1)
            self.assertEqual(
                old.execute("SELECT count(*) FROM claim_evidence").fetchone()[0], 1
            )
        snapshots = list(legacy.parent.glob("legacy.sqlite3.pre-v2-*.sqlite3"))
        self.assertEqual(len(snapshots), 1)
        with closing(sqlite3.connect(snapshots[0])) as snapshot:
            snapshot.execute("PRAGMA foreign_keys=ON")
            validate(snapshot, expected_version=1)

    def test_invalid_v1_history_never_starts_migration(self):
        legacy = self.make_legacy_database()
        with closing(sqlite3.connect(legacy)) as old:
            old.execute(
                "UPDATE schema_migrations SET checksum='tampered' WHERE id='0001_initial'"
            )
            old.commit()
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            initialize(legacy)
        with closing(sqlite3.connect(legacy)) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            list(legacy.parent.glob("legacy.sqlite3.pre-v2-*.sqlite3")), []
        )

    def test_v2_history_checksum_is_checked(self):
        self.db.execute(
            "UPDATE schema_migrations SET checksum='tampered' "
            "WHERE id='0002_claim_citation_seal'"
        )
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            initialize(self.path)

    def test_project_reference_exactly_one(self):
        self.seed()
        for asset, claim in [(None, None), ("a1", "cv1")]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(
                    "INSERT INTO project_references VALUES (?,?,?,?,?,?)",
                    ("ref", "p1", asset, claim, None, "RESEARCH"),
                )
        self.db.execute(
            "INSERT INTO project_references VALUES (?,?,?,?,?,?)",
            ("ref1", "p1", "a1", None, None, "RESEARCH"),
        )

    def test_review_exactly_one_and_version_immutable(self):
        self.seed()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)",
                ("r1", None, None, None, "ACCEPT", "owner", None, T),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE evidence_versions SET content='changed' WHERE evidence_version_id='ev1'"
            )
        self.db.execute(
            "INSERT INTO evidence_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ev2",
                "e1",
                "a1",
                2,
                "f1",
                "corrected",
                "TIME_RANGE",
                '{"start_ms":0,"end_ms":1000,"track":"audio"}',
                "HUMAN",
                None,
                "owner",
                T,
            ),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT evidence_version_id FROM claim_evidence"
            ).fetchone()[0],
            "ev1",
        )

    def test_anchor_same_asset(self):
        self.seed()
        self.db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)", ("a2", "VIDEO", "other", T, None)
        )
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("f2", "a2", "ORIGINAL", "other.mp4", "def", 1, "video/mp4", T),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO evidence_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ev2",
                    "e1",
                    "a1",
                    2,
                    "f2",
                    "bad",
                    "WHOLE_ASSET",
                    "{}",
                    "HUMAN",
                    None,
                    "owner",
                    T,
                ),
            )

    def test_package_atomicity_fingerprint_and_approval(self):
        self.seed()
        with self.assertRaises(ValueError):
            PublicationRepository(self.path).create_package(
                package_id="empty",
                project_id="p1",
                draft_version_id="dv1",
                platform="FB",
                format="POST",
                created_at=T,
                items=[],
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM publication_packages").fetchone()[0],
            0,
        )
        fp1 = PublicationRepository(self.path).create_package(
            package_id="pkg1",
            project_id="p1",
            draft_version_id="dv1",
            platform="FB",
            format="POST",
            created_at=T,
            items=[PackageItem("CAPTION", 0, "caption")],
        )
        self.db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
            ("ap1", "pkg1", fp1, "APPROVED", "owner", T, None),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO publication_package_items VALUES (?,?,?,?,?,?,?)",
                ("late", "pkg1", "CTA", 0, "x", None, "x"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE publication_packages SET fingerprint='bad' WHERE package_id='pkg1'"
            )
        self.db.commit()
        fp2 = PublicationRepository(self.path).create_package(
            package_id="pkg2",
            project_id="p1",
            draft_version_id="dv1",
            platform="FB",
            format="POST",
            created_at=T,
            items=[PackageItem("CAPTION", 0, "caption!")],
        )
        self.assertNotEqual(fp1, fp2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
                ("ap2", "pkg2", fp1, "APPROVED", "owner", T, None),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?)",
                ("post", "pkg1", "YT", None, None, None, "PENDING", T),
            )

    def test_locator_contract_and_migration_checksum(self):
        self.assertEqual(
            validate_locator(
                "TIME_RANGE",
                '{"start_ms":10,"end_ms":23,"track":"audio"}',
                duration_ms=30,
            )["end_ms"],
            23,
        )
        for kind, raw in [
            ("TIME_RANGE", '{"start_ms":30,"end_ms":10,"track":"audio"}'),
            ("IMAGE_REGION", '{"x":0.9,"y":0.2,"width":0.5,"height":0.2}'),
            ("TEXT_RANGE", '{"start":1,"end":5,"text_digest":"sha256:x"}'),
        ]:
            with self.assertRaises(ValueError):
                validate_locator(kind, raw, text_length=3)
        self.db.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE id='0001_initial'"
        )
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            initialize(self.path)

    def test_audit_events_are_append_only(self):
        self.seed()
        self.db.execute(
            "INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?)",
            ("r1", "ev1", None, None, "ACCEPT", "owner", None, T),
        )
        self.db.commit()
        for sql in (
            "UPDATE review_events SET action='REJECT' WHERE review_event_id='r1'",
            "DELETE FROM review_events WHERE review_event_id='r1'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.db.commit()
        fp = PublicationRepository(self.path).create_package(
            package_id="pkg",
            project_id="p1",
            draft_version_id="dv1",
            platform="FB",
            format="POST",
            created_at=T,
            items=[PackageItem("CAPTION", 0, "x")],
        )
        self.db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
            ("ap", "pkg", fp, "APPROVED", "owner", T, None),
        )
        self.db.commit()
        for sql in (
            "UPDATE approvals SET decision='REVOKED' WHERE approval_id='ap'",
            "DELETE FROM approvals WHERE approval_id='ap'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)

    def test_cross_asset_parent_update_and_processing_input(self):
        self.seed()
        self.db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?)", ("a2", "VIDEO", "other", T, None)
        )
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("f2", "a2", "ORIGINAL", "other", "def", 1, "video/mp4", T),
        )
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("child", "a1", "DERIVED_AUDIO", "child", "ghi", 1, "audio/wav", "f1", T),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE asset_files SET parent_file_id='f2' WHERE file_id='child'"
            )
        self.db.execute(
            "INSERT INTO processing_runs(run_id,asset_id,task_type,status,tool_name,tool_version,created_at) VALUES (?,?,?,?,?,?,?)",
            ("run", "a1", "TRANSCRIBE", "QUEUED", "fake", "1", T),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE processing_runs SET input_file_id='f2' WHERE run_id='run'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE processing_runs SET asset_id='a2',input_file_id='f1' WHERE run_id='run'"
            )

    def test_direct_sql_package_cannot_be_sealed_or_approved_wrongly(self):
        self.seed()
        self.db.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?)",
            ("p2", "other", "ACTIVE", T, None),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO publication_packages VALUES (?,?,?,?,?,?,?,?,?)",
                ("bad", "p2", "dv1", "FB", "POST", "hash", 1, None, T),
            )
        self.db.execute(
            "INSERT INTO publication_packages VALUES (?,?,?,?,?,?,?,?,?)",
            ("staging", "p1", "dv1", "FB", "POST", "hash", 1, None, T),
        )
        self.db.commit()  # direct SQL may leave internal staging; it cannot be approved or published
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE publication_packages SET sealed_at=? WHERE package_id='staging'",
                (T,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
                ("ap", "staging", "hash", "APPROVED", "owner", T, None),
            )
        self.db.execute(
            "INSERT INTO publication_package_items VALUES (?,?,?,?,?,?,?)",
            ("i", "staging", "CAPTION", 0, "text", None, "digest"),
        )
        self.db.execute(
            "UPDATE publication_packages SET sealed_at=? WHERE package_id='staging'",
            (T,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO publication_package_items VALUES (?,?,?,?,?,?,?)",
                ("i2", "staging", "CTA", 0, "more", None, "digest"),
            )

    def test_readonly_public_path_and_post_update_guard(self):
        self.seed()
        with closing(open_readonly(self.path)) as ro:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute(
                    "INSERT INTO projects VALUES (?,?,?,?,?)",
                    ("bad", "bad", "ACTIVE", T, None),
                )
        fp = PublicationRepository(self.path).create_package(
            package_id="pkg",
            project_id="p1",
            draft_version_id="dv1",
            platform="FB",
            format="POST",
            created_at=T,
            items=[PackageItem("CAPTION", 0, "x")],
        )
        self.db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
            ("ap", "pkg", fp, "APPROVED", "owner", T, None),
        )
        self.db.execute(
            "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?)",
            ("post", "pkg", "FB", None, None, None, "PENDING", T),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE posts SET platform='YT' WHERE post_id='post'")

    def test_data_root_override(self):
        old = os.environ.get("CREATOR_LOOP_DATA_ROOT")
        try:
            os.environ["CREATOR_LOOP_DATA_ROOT"] = str(
                Path(self.tmp.name) / "dá»¯ liá»‡u"
            )
            self.assertEqual(
                data_root(), (Path(self.tmp.name) / "dá»¯ liá»‡u").resolve()
            )
        finally:
            if old is None:
                os.environ.pop("CREATOR_LOOP_DATA_ROOT", None)
            else:
                os.environ["CREATOR_LOOP_DATA_ROOT"] = old


if __name__ == "__main__":
    unittest.main()
