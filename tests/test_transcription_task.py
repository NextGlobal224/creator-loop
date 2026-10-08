"""Actual owned I/O/Qt/fake CLI, GUI pulses and durable task cancellation."""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    import test_whisper_worker as native_fixture
    from creator_loop.app_lock import AppDataLock
    from creator_loop.component_selection import save_component_selection
    from creator_loop.database import initialize
    from creator_loop.media_intake import intake_video_original
    from creator_loop.owned_process import OwnedWindowsProcess
    from creator_loop.processing_recovery import recover_processing_startup
    from creator_loop.storage_paths import resolve_storage_path
    from creator_loop.transcription_command import TranscriptionCommand
    from creator_loop.transcription_task import (
        _read_task_cancellation,
        run_transcription_task,
    )
    from creator_loop.windows_owned_file import OwnedWindowsFile
    from creator_loop.windows_paths import file_io_path
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "actual Windows owned executor + Qt")
class TranscriptionTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = native_fixture.FAKE_CLI.replace(
            'if(Path.GetFileName(input)=="hang.wav")',
            'if(Path.GetFileName(model)=="hang-model.bin")',
        )
        with patch.object(native_fixture, "FAKE_CLI", script):
            native_fixture.WhisperWorkerTests.setUpClass.__func__(cls)

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        fixture = Path(__file__).parent / "fixtures/video-with-tone.mp4"
        self.original = intake_video_original(fixture, root=self.root)
        self.path = resolve_storage_path(
            self.root, "ORIGINAL", self.original.storage_key
        )
        self.before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.command = TranscriptionCommand(self.root)
        self.addCleanup(self.command.shutdown)
        self.results, self.failures, self.phases, self.pulses = [], [], [], []
        self.command.finished.connect(
            lambda code, text: self.results.append((code, text))
        )
        self.command.failed.connect(self.failures.append)
        self.command.phase_changed.connect(self.phases.append)
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: self.pulses.append(time.perf_counter()))
        timer.start()
        self.timer = timer
        self.addCleanup(timer.stop)

    def query(self, sql):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql).fetchall()

    def select(self, *, hang=False):
        specs = self.specs
        if hang:
            model = self.root / "hang-model.bin"
            model.write_bytes(self.model.read_bytes())
            specs = (*specs[:-1], replace(specs[-1], path=model))
        with AppDataLock(self.root) as lock:
            save_component_selection(
                self.root, specs, lock=lock, worker_memory_limit=768 * 1024**2
            )

    def until(self, predicate, timeout=20):
        deadline = time.perf_counter() + timeout
        while not predicate() and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), str(self.results) + str(self.failures))

    def report(self):
        self.assertFalse(self.failures, self.failures)
        code, text = self.results[-1]
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[-1]["request_id"], self.command.request_id)
        self.assertEqual(rows[-1]["event"], "result")
        return code, rows[-1]

    def engine_started(self):
        return bool(
            list((self.root / "runtime").glob("whisper-*/child-ownership.json"))
        )

    def _deep_root(self):
        base = self.root
        prefix = "Deep transcription tiếng Việt "
        deep = base / (prefix + "x" * (209 - len(str(base)) - 1 - len(prefix)))
        self.assertEqual(len(str(deep)), 209)
        deep.mkdir()
        self.addCleanup(shutil.rmtree, file_io_path(deep))
        self.addCleanup(self.command.shutdown)
        self.root = deep
        self.db_path = deep / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.original = intake_video_original(
            Path(__file__).parent / "fixtures/video-with-tone.mp4", root=deep
        )
        self.path = resolve_storage_path(deep, "ORIGINAL", self.original.storage_key)
        before = hashlib.sha256(file_io_path(self.path).read_bytes()).hexdigest()
        self.command.root = deep
        return before

    def test_deep_request_progress_and_native_task_keep_original_and_lineage(self):
        before = self._deep_root()
        self.select()
        self.command.start_task(self.original.file_id)
        request = self.command.workspace / "request.json"
        self.assertGreater(len(str(request)), 260)
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual(
            (code, body["status"]),
            (0, "SUCCEEDED"),
            (
                body,
                self.query(
                    "SELECT task_type,status,error_code,error_message FROM processing_runs"
                ),
            ),
        )
        self.assertIn("decode", self.phases)
        self.assertIn("transcribe", self.phases)
        self.assertGreater(len(self.pulses), 10)
        self.assertEqual(
            self.query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "SUCCEEDED")],
        )
        self.assertEqual(
            self.query(
                "SELECT parent_file_id FROM asset_files WHERE file_id='"
                + body["raw_file_id"]
                + "'"
            ),
            [(body["audio_file_id"],)],
        )
        self.assertEqual(self.query("SELECT COUNT(*) FROM evidence_versions"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM review_events"), [(0,)])
        self.assertEqual(
            hashlib.sha256(file_io_path(self.path).read_bytes()).hexdigest(), before
        )

    def test_deep_task_cancel_preserves_original_pcm_and_prior_raw(self):
        before = self._deep_root()
        prior = self.root / "storage/derived/retained-raw.json"
        file_io_path(prior.parent).mkdir(exist_ok=True)
        file_io_path(prior).write_bytes(b"unchanged previous RAW")
        self.select(hang=True)
        self.command.start_task(self.original.file_id)
        self.until(
            lambda: bool(
                list(
                    file_io_path(self.root / "runtime").glob(
                        "whisper-*/child-ownership.json"
                    )
                )
            )
        )
        self.assertEqual(
            self.query(
                "SELECT status FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("RUNNING",)],
        )
        self.command.cancel()
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["status"]), (2, "CANCELLED"))
        self.assertEqual(
            self.query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "CANCELLED")],
        )
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM asset_files WHERE role='RAW'"), [(0,)]
        )
        self.assertEqual(file_io_path(prior).read_bytes(), b"unchanged previous RAW")
        self.assertEqual(
            hashlib.sha256(file_io_path(self.path).read_bytes()).hexdigest(), before
        )

    def test_actual_pipeline_registers_lineage_without_text_in_task_log(self):
        self.select()
        self.command.start_task(self.original.file_id)
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["status"]), (0, "SUCCEEDED"))
        self.assertIn("decode", self.phases)
        self.assertIn("transcribe", self.phases)
        self.assertGreater(len(self.pulses), 10)
        self.assertEqual(
            self.query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "SUCCEEDED")],
        )
        self.assertEqual(self.query("SELECT COUNT(*) FROM evidence_versions"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM review_events"), [(0,)])
        self.assertEqual(
            self.query(
                "SELECT parent_file_id FROM asset_files WHERE file_id='"
                + body["raw_file_id"]
                + "'"
            ),
            [(body["audio_file_id"],)],
        )
        self.assertNotIn("fixture RAW", self.results[-1][1])
        self.assertEqual(
            hashlib.sha256(self.path.read_bytes()).hexdigest(), self.before
        )

    def test_held_library_lock_refuses_without_db_writes(self):
        self.select()
        before = self.db_path.read_bytes()
        with AppDataLock(self.root):
            self.command.start_task(self.original.file_id)
            self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["error"]), (3, "DATA_ROOT_BUSY"))
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM processing_runs"), [(0,)])

    def test_missing_selection_refuses_without_creating_task(self):
        self.command.start_task(self.original.file_id)
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["status"]), (4, "FAILED"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM processing_runs"), [(0,)])

    def test_cancel_before_worker_decode_records_cancelled_queued_run(self):
        self.select()
        self.command.start_task(self.original.file_id)
        self.command.cancel()
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["status"]), (2, "CANCELLED"))
        self.assertEqual(
            self.query(
                "SELECT status,started_at,finished_at IS NOT NULL FROM processing_runs"
            ),
            [("CANCELLED", None, 1)],
        )
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM asset_files WHERE role!='ORIGINAL'"),
            [(0,)],
        )

    def test_running_cancel_keeps_decode_success_and_unrelated_process_alive(self):
        self.select(hang=True)
        with OwnedWindowsProcess(
            Path(sys.executable),
            ["-c", "import time;time.sleep(30)"],
            self.root / "logs/sentinel",
            component_version="unrelated-fixture",
        ) as sentinel:
            self.command.start_task(self.original.file_id)
            self.until(self.engine_started)
            started = time.perf_counter()
            self.command.cancel()
            self.assertLess(time.perf_counter() - started, 0.2)
            self.until(lambda: not self.command.busy)
            self.assertEqual(
                sentinel.kernel.WaitForSingleObject(sentinel.process, 0), 258
            )
        code, body = self.report()
        self.assertEqual((code, body["status"]), (2, "CANCELLED"))
        self.assertEqual(
            self.query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "CANCELLED")],
        )
        self.assertTrue(list((self.root / "runtime").glob("whisper-*")))
        self.assertEqual(
            hashlib.sha256(self.path.read_bytes()).hexdigest(), self.before
        )

    def test_outer_deadline_retires_owned_tree_then_recovery_marks_interruption(self):
        self.select(hang=True)
        self.command.start_task(self.original.file_id)
        self.until(self.engine_started)
        self.command.deadline = time.perf_counter() + 0.15
        self.until(lambda: not self.command.busy)
        self.assertTrue(self.failures)
        self.assertGreater(len(self.pulses), 10)
        with AppDataLock(self.root) as lock:
            recovery = recover_processing_startup(self.root, lock)
        self.assertEqual(len(recovery.interrupted_run_ids), 1)
        self.assertEqual(
            self.query(
                "SELECT status,error_code FROM processing_runs WHERE task_type='AUDIO_TRANSCRIPTION'"
            ),
            [("FAILED", "INTERRUPTED_AT_STARTUP")],
        )
        self.assertEqual(
            hashlib.sha256(self.path.read_bytes()).hexdigest(), self.before
        )

    def test_cancel_publication_exclusive_writer_is_retried_before_complete_read(self):
        self.select(hang=True)
        self.command.start_task(self.original.file_id)
        self.until(self.engine_started)
        self.assertIsNotNone(self.command.workspace)
        control = self.command.workspace / "cancel.json"
        owned = OwnedWindowsFile.create_new(control)
        try:
            payload = json.dumps(
                {"format": 1, "request_id": self.command.request_id, "cancel": True}
            ).encode()
            owned.stream.write(payload[:5])
            owned.stream.flush()
            os.fsync(owned.stream.fileno())
            # Publication is visible by pathname, but the exclusive writer
            # prevents reading until fsync/close. Do not turn this into FAILED.
            deadline = time.perf_counter() + 0.3
            while time.perf_counter() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(self.command.busy, str(self.results) + str(self.failures))
            owned.stream.write(payload[5:])
            owned.stream.flush()
            os.fsync(owned.stream.fileno())
        finally:
            owned.close()
        self.until(lambda: not self.command.busy)
        code, body = self.report()
        self.assertEqual((code, body["status"]), (2, "CANCELLED"), body)
        self.assertEqual(
            self.query(
                "SELECT task_type,status FROM processing_runs ORDER BY created_at"
            ),
            [("VIDEO_AUDIO_DECODE", "SUCCEEDED"), ("AUDIO_TRANSCRIPTION", "CANCELLED")],
        )

    def test_closed_malformed_cancel_record_is_not_retried_or_accepted(self):
        path = self.root / "runtime/cancel-test.json"
        identity = "a" * 32
        for body in (
            {"format": True, "request_id": identity, "cancel": True},
            {"format": 1, "request_id": "b" * 32, "cancel": True},
            {"format": 1, "request_id": identity, "cancel": 1},
        ):
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.subTest(body=body), self.assertRaises(ValueError):
                _read_task_cancellation(path, identity)
        path.write_text(
            '{"format":1,"format":1,"request_id":"' + identity + '","cancel":true}',
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            _read_task_cancellation(path, identity)

    def test_unbound_private_entry_refuses_without_db_initialization(self):
        before = self.db_path.read_bytes()
        request = (
            self.root
            / "runtime/transcribe-00000000000000000000000000000000/request.json"
        )
        request.parent.mkdir()
        request.write_text('{"format":1}', encoding="utf-8")
        self.assertEqual(run_transcription_task(request), 4)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM processing_runs"), [(0,)])
