"""Historical registered bytes/SQL fixtures; no engine execution or AI quality."""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import initialize
from creator_loop.evidence import EvidenceRepository
from creator_loop.library import Asset, LibraryRepository
from creator_loop.paths import ensure_data_root
from creator_loop.storage_paths import new_storage_destination
from creator_loop.transcript_evidence import (
    create_transcript_evidence,
    load_transcript_suggestions,
)


class TranscriptEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        ensure_data_root(self.root)
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.asset_id = uuid4().hex
        self.original_id, self.pcm_id, self.raw_id, self.provenance_id = (
            uuid4().hex for _ in range(4)
        )
        self.decode_id, self.run_id = (uuid4().hex for _ in range(2))
        self.paths = {}
        self.raw = {
            "model": {"type": "base", "multilingual": True},
            "params": {"language": "vi", "translate": False},
            "result": {"language": "vi"},
            "transcription": [
                {"offsets": {"from": 0, "to": 90}, "text": " Lời máy chưa duyệt "}
            ],
        }
        self.provenance = {
            "format": 1,
            "kind": "WHISPER_TRANSCRIPTION_PROVENANCE",
            "run_id": self.run_id,
            "input_file_id": self.pcm_id,
            "original_file_id": self.original_id,
            "raw_file_id": self.raw_id,
            "settings": {"language": "vi"},
        }
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            repo = LibraryRepository(db)
            repo.create_asset(
                Asset(self.asset_id, "VIDEO", "Historical fixture", "2026-10-05", None)
            )
            self._file(
                db,
                self.original_id,
                "ORIGINAL",
                "video/mp4",
                None,
                None,
                (Path(__file__).parent / "fixtures/video-with-tone.mp4").read_bytes(),
            )
            self._run(db, self.decode_id, self.original_id, "VIDEO_AUDIO_DECODE")
            # Physical PCM fixture: 1600 frames/16 kHz, complete zero-origin100ms.
            import io
            import wave

            wav = io.BytesIO()
            with wave.open(wav, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * 1600)
            pcm = wav.getvalue()
            self._file(
                db,
                self.pcm_id,
                "DERIVED_AUDIO",
                "audio/wav",
                self.original_id,
                self.decode_id,
                pcm,
            )
            db.execute(
                "UPDATE asset_files SET duration_ms=951 WHERE file_id=?",
                (self.original_id,),
            )
            db.execute(
                "UPDATE asset_files SET duration_ms=100 WHERE file_id=?", (self.pcm_id,)
            )
            self._run(db, self.run_id, self.pcm_id, "AUDIO_TRANSCRIPTION")
            raw = json.dumps(self.raw, ensure_ascii=False).encode("utf-8")
            self.raw_digest = hashlib.sha256(raw).hexdigest()
            self.provenance.update(
                raw_sha256=self.raw_digest,
                input_sha256=hashlib.sha256(pcm).hexdigest(),
                input_bytes=len(pcm),
            )
            self._file(
                db,
                self.raw_id,
                "OTHER",
                "application/json",
                self.pcm_id,
                self.run_id,
                raw,
            )
            self._file(
                db,
                self.provenance_id,
                "OTHER",
                "application/json",
                self.raw_id,
                self.run_id,
                json.dumps(self.provenance).encode(),
            )
            db.commit()

    def _run(self, db, identity, input_id, task):
        LibraryRepository(db).create_processing_run(
            run_id=identity,
            asset_id=self.asset_id,
            input_file_id=input_id,
            task_type=task,
            status="SUCCEEDED",
            tool_name="whisper.cpp" if task == "AUDIO_TRANSCRIPTION" else "Qt",
            tool_version="1.8.7" if task == "AUDIO_TRANSCRIPTION" else "6.10.2",
            model_name="Whisper base multilingual"
            if task == "AUDIO_TRANSCRIPTION"
            else None,
            model_version="5359861c739e955e79d9a303bcbc70fb988958b1"
            if task == "AUDIO_TRANSCRIPTION"
            else None,
            started_at="2026-10-05",
            finished_at="2026-10-05",
            error_code=None,
            error_message=None,
            created_at="2026-10-05",
        )

    def _file(self, db, identity, role, mime, parent, run, body):
        key, path = new_storage_destination(self.root, role, identity)
        path.write_bytes(body)
        self.paths[identity] = path
        LibraryRepository(db).create_asset_file(
            file_id=identity,
            asset_id=self.asset_id,
            role=role,
            storage_key=key,
            sha256=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            mime_type=mime,
            parent_file_id=parent,
            processing_run_id=run,
            created_at="2026-10-05",
        )

    def _replace_registered(self, identity, body):
        self.paths[identity].write_bytes(body)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE asset_files SET sha256=?,byte_size=? WHERE file_id=?",
                (hashlib.sha256(body).hexdigest(), len(body), identity),
            )
            db.commit()

    def _create(self, lock, index=0, digest=None):
        return create_transcript_evidence(
            self.raw_id, index, digest or self.raw_digest, self.root, lock
        )

    def _assert_no_evidence(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone(), (0,)
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone(), (0,)
            )

    def test_read_without_selected_components_and_create_pending_model_original_anchor(
        self,
    ):
        before = {identity: path.read_bytes() for identity, path in self.paths.items()}
        with AppDataLock(self.root) as lock:
            suggestions = load_transcript_suggestions(self.raw_id, self.root, lock)
            self._assert_no_evidence()
            version = self._create(lock)
            another = self._create(lock)
        self.assertEqual(
            suggestions.segments[0].text, self.raw["transcription"][0]["text"]
        )
        self.assertEqual(version.content, suggestions.segments[0].text)
        self.assertEqual(
            (
                version.anchor_file_id,
                version.producer_type,
                version.processing_run_id,
                version.created_by,
            ),
            (self.original_id, "MODEL", self.run_id, None),
        )
        self.assertEqual(
            json.loads(version.locator_data),
            {"start_ms": 0, "end_ms": 90, "track": "audio"},
        )
        self.assertNotEqual(version.evidence_id, another.evidence_id)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone(), (0,)
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone(), (2,)
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            before,
            {identity: path.read_bytes() for identity, path in self.paths.items()},
        )

    def test_stale_digest_or_absent_or_boolean_index_refused_atomically(self):
        with AppDataLock(self.root) as lock:
            for index, digest in ((0, "0" * 64), (1, None), (-1, None), (True, None)):
                with (
                    self.subTest(index=index, digest=digest),
                    self.assertRaises(ValueError),
                ):
                    self._create(lock, index, digest)
        self._assert_no_evidence()

    def test_changed_registered_bytes_refused_without_engine_or_database_writes(self):
        for identity in self.paths:
            original = self.paths[identity].read_bytes()
            self.paths[identity].write_bytes(original + b"x")
            with (
                self.subTest(identity=identity),
                AppDataLock(self.root) as lock,
                self.assertRaises(ValueError),
            ):
                self._create(lock)
            self.paths[identity].write_bytes(original)
        self._assert_no_evidence()

    def test_failed_processing_state_or_wrong_parent_refused(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE processing_runs SET status='FAILED' WHERE run_id=?",
                (self.run_id,),
            )
            db.commit()
        with AppDataLock(self.root) as lock, self.assertRaises(ValueError):
            self._create(lock)
        self._assert_no_evidence()

    def test_bound_provenance_mismatch_duplicate_fields_and_boolean_format_refused(
        self,
    ):
        variants = [
            {**self.provenance, "original_file_id": "0" * 32},
            {**self.provenance, "format": True},
        ]
        bodies = [json.dumps(value).encode() for value in variants]
        bodies.append(json.dumps(self.provenance).encode()[:-1] + b',"format":1}')
        with AppDataLock(self.root) as lock:
            for body in bodies:
                self._replace_registered(self.provenance_id, body)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    self._create(lock)
        self._assert_no_evidence()

    def test_invalid_range_is_never_clipped_and_empty_text_is_not_evidence(self):
        with AppDataLock(self.root) as lock:
            for end, text in ((101, "machine"), (90, "  ")):
                self.raw["transcription"][0].update(
                    offsets={"from": 0, "to": end}, text=text
                )
                body = json.dumps(self.raw).encode()
                self.raw_digest = hashlib.sha256(body).hexdigest()
                self.provenance["raw_sha256"] = self.raw_digest
                self._replace_registered(self.raw_id, body)
                self._replace_registered(
                    self.provenance_id, json.dumps(self.provenance).encode()
                )
                with self.subTest(end=end, text=text), self.assertRaises(ValueError):
                    self._create(lock)
        self._assert_no_evidence()

    def test_unheld_or_wrong_root_lock_refused(self):
        with self.assertRaises(RuntimeError):
            self._create(AppDataLock(self.root))
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other).resolve()
            ensure_data_root(other_root)
            with AppDataLock(other_root) as lock, self.assertRaises(RuntimeError):
                self._create(lock)
        self._assert_no_evidence()

    def test_missing_database_is_not_created_by_refused_selection(self):
        self.db_path.unlink()
        with AppDataLock(self.root) as lock, self.assertRaises(ValueError):
            self._create(lock)
        self.assertFalse(self.db_path.exists())

    def test_database_disappearing_after_guard_is_not_recreated_by_writer(self):
        from creator_loop import transcript_evidence as service

        original_guard = service._root

        def disappear(root, coordination):
            canonical = original_guard(root, coordination)
            self.db_path.unlink()
            return canonical

        with (
            AppDataLock(self.root) as lock,
            patch.object(service, "_root", side_effect=disappear),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self._create(lock)
        self.assertFalse(self.db_path.exists())

    def test_version_insert_failure_rolls_back_evidence_identity(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TRIGGER fixture_refuse_version BEFORE INSERT ON evidence_versions BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
            )
            db.commit()
        with AppDataLock(self.root) as lock, self.assertRaises(sqlite3.IntegrityError):
            self._create(lock)
        self._assert_no_evidence()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM evidences").fetchone(), (0,)
            )

    @unittest.skipUnless(sys.platform == "win32", "native Windows retained byte leases")
    def test_all_registered_bytes_remain_locked_through_evidence_commit(self):
        original = EvidenceRepository._create_with_version
        attempts = []

        def insert(repository, evidence, version):
            for identity, path in self.paths.items():
                with self.subTest(identity=identity):
                    with self.assertRaises(OSError):
                        path.open("r+b")
                    with self.assertRaises(OSError):
                        path.rename(path.with_name(path.name + "-replacement"))
                    with self.assertRaises(OSError):
                        path.unlink()
                    attempts.append(identity)
            return original(repository, evidence, version)

        with (
            AppDataLock(self.root) as lock,
            patch.object(EvidenceRepository, "_create_with_version", insert),
        ):
            version = self._create(lock)
        self.assertEqual(set(attempts), set(self.paths))
        self.assertEqual(version.producer_type, "MODEL")


if __name__ == "__main__":
    unittest.main()
