"""Native fake Whisper plus real registered PCM; immutable RAW and task states."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock
from creator_loop.database import initialize
from creator_loop.whisper_adapter import WhisperSettings

if sys.platform == "win32":
    import test_whisper_worker as native_fixture
    from creator_loop.audio_derivative import create_video_audio_derivative
    from creator_loop.media_intake import intake_video_original
    from creator_loop.storage_paths import resolve_storage_path
    from creator_loop.transcription_service import transcribe_audio_derivative
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "Windows owned native fake + Qt PCM")
class TranscriptionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the compiler/profile fixture, without inheriting its test cases.
        native_fixture.WhisperWorkerTests.setUpClass.__func__(cls)

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        fixture = Path(__file__).resolve().parent / "fixtures/video-with-tone.mp4"
        self.original = intake_video_original(fixture, root=self.root)
        with AppDataLock(self.root) as lock:
            self.audio = create_video_audio_derivative(
                self.original.file_id, self.root, lock
            )

    def _query(self, sql):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql).fetchall()

    def _run(self, lock, **kwargs):
        return transcribe_audio_derivative(
            self.audio.file_id,
            self.root,
            lock,
            self.specs,
            settings=WhisperSettings(self.budget),
            **kwargs,
        )

    def test_success_registers_exact_raw_provenance_and_no_review(self):
        with AppDataLock(self.root) as lock:
            result = self._run(lock)
        rows = self._query(
            "SELECT file_id,role,mime_type,parent_file_id,processing_run_id,storage_key,sha256 FROM asset_files WHERE role='OTHER'"
        )
        self.assertEqual(len(rows), 2)
        raw = next(row for row in rows if row[0] == result.raw_file_id)
        provenance = next(row for row in rows if row[0] == result.provenance_file_id)
        self.assertEqual(
            raw[1:5], ("OTHER", "application/json", self.audio.file_id, result.run_id)
        )
        raw_bytes = resolve_storage_path(self.root, "OTHER", raw[5]).read_bytes()
        self.assertEqual(raw_bytes, (result.workspace / "raw.json").read_bytes())
        self.assertEqual(hashlib.sha256(raw_bytes).hexdigest(), raw[6])
        proof = json.loads(
            resolve_storage_path(self.root, "OTHER", provenance[5]).read_bytes()
        )
        self.assertEqual(proof["raw_sha256"], result.raw_sha256)
        self.assertEqual(proof["run_id"], result.run_id)
        self.assertEqual(len(proof["components"]), 6)
        self.assertIs(proof["quality_accepted"], False)
        self.assertEqual(provenance[3], result.raw_file_id)
        self.assertEqual(
            self._query(
                "SELECT status,tool_name,model_name,input_file_id FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [
                (
                    "SUCCEEDED",
                    "whisper.cpp",
                    "Whisper base multilingual",
                    self.audio.file_id,
                )
            ],
        )
        self.assertEqual(self._query("SELECT count(*) FROM evidence_versions"), [(0,)])
        self.assertEqual(self._query("SELECT count(*) FROM review_events"), [(0,)])

    def test_second_run_preserves_first_raw_and_versions(self):
        with AppDataLock(self.root) as lock:
            first = self._run(lock)
            before = (first.workspace / "raw.json").read_bytes()
            second = self._run(lock)
        self.assertNotEqual(first.raw_file_id, second.raw_file_id)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual((first.workspace / "raw.json").read_bytes(), before)
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='OTHER'"), [(4,)]
        )

    def test_queued_cancel_records_cancelled_not_success(self):
        with AppDataLock(self.root) as lock, self.assertRaises(InterruptedError):
            self._run(lock, cancelled=lambda: True)
        self.assertEqual(
            self._query(
                "SELECT status,error_code FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("CANCELLED", "CANCELLED")],
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='OTHER'"), [(0,)]
        )

    def test_changed_pcm_refuses_and_records_failed(self):
        path = resolve_storage_path(self.root, "DERIVED_AUDIO", self.audio.storage_key)
        with path.open("ab") as output:
            output.write(b"changed")
        with AppDataLock(self.root) as lock, self.assertRaises(ValueError):
            self._run(lock)
        self.assertEqual(
            self._query(
                "SELECT status FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("FAILED",)],
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='OTHER'"), [(0,)]
        )

    def test_disk_write_failure_retains_runtime_raw_and_separate_decode_success(self):
        with (
            AppDataLock(self.root) as lock,
            patch(
                "creator_loop.transcription_service.OwnedWindowsFile.create_new",
                side_effect=OSError("simulated disk full"),
            ),
            self.assertRaises(OSError),
        ):
            self._run(lock)
        raw = list((self.root / "runtime").glob("whisper-*/raw.json"))
        self.assertEqual(len(raw), 1)
        self.assertTrue(raw[0].read_bytes())
        self.assertEqual(
            self._query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "FAILED")],
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='OTHER'"), [(0,)]
        )

    def test_registration_failure_retains_raw_copies_without_dangling_rows(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TRIGGER fixture_refuse_raw BEFORE INSERT ON asset_files WHEN NEW.role='OTHER' BEGIN SELECT RAISE(ABORT,'fixture raw refusal'); END"
            )
            db.commit()
        with AppDataLock(self.root) as lock, self.assertRaises(sqlite3.IntegrityError):
            self._run(lock)
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='OTHER'"), [(0,)]
        )
        self.assertEqual(
            self._query(
                "SELECT status FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("FAILED",)],
        )
        self.assertEqual(
            len(list((self.root / "storage/derived").glob("*-raw.json"))), 1
        )
        self.assertEqual(
            len(list((self.root / "runtime").glob("whisper-*/raw.json"))), 1
        )
