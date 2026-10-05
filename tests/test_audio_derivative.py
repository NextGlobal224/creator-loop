"""Durable PCM/task lineage and failure behavior, using real owned Qt decode."""

import hashlib
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

if sys.platform == "win32":
    from creator_loop.audio_derivative import create_video_audio_derivative
    from creator_loop.media_intake import intake_video_original
    from creator_loop.storage_paths import resolve_storage_path
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "Windows real owned Qt PCM")
class AudioDerivativeTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.fixture = Path(__file__).resolve().parent / "fixtures/video-with-tone.mp4"
        self.input = intake_video_original(self.fixture, root=self.root)
        self.original = resolve_storage_path(
            self.root, "ORIGINAL", self.input.storage_key
        )
        self.before = hashlib.sha256(self.original.read_bytes()).hexdigest()

    def _query(self, sql):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql).fetchall()

    def test_success_registers_pcm_with_separate_run_and_no_evidence_review(self):
        with AppDataLock(self.root) as lock:
            result = create_video_audio_derivative(self.input.file_id, self.root, lock)
        row = self._query(
            "SELECT role,mime_type,parent_file_id,processing_run_id,sha256 FROM asset_files WHERE role='DERIVED_AUDIO'"
        )[0]
        self.assertEqual(
            row,
            (
                "DERIVED_AUDIO",
                "audio/wav",
                self.input.file_id,
                result.run_id,
                result.sha256,
            ),
        )
        run = self._query(
            "SELECT task_type,status,input_file_id,model_name,started_at,finished_at,error_code FROM processing_runs"
        )[0]
        self.assertEqual(
            run[:4], ("VIDEO_AUDIO_DECODE", "SUCCEEDED", self.input.file_id, None)
        )
        self.assertIsNotNone(run[4])
        self.assertIsNotNone(run[5])
        self.assertIsNone(run[6])
        stored = resolve_storage_path(self.root, "DERIVED_AUDIO", result.storage_key)
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), result.sha256)
        self.assertEqual(
            hashlib.sha256(self.original.read_bytes()).hexdigest(), self.before
        )
        self.assertEqual(self._query("SELECT count(*) FROM evidence_versions"), [(0,)])
        self.assertEqual(self._query("SELECT count(*) FROM review_events"), [(0,)])

    def test_repeated_tasks_create_new_files_runs_without_overwrite(self):
        with AppDataLock(self.root) as lock:
            first = create_video_audio_derivative(self.input.file_id, self.root, lock)
            before = resolve_storage_path(
                self.root, "DERIVED_AUDIO", first.storage_key
            ).read_bytes()
            second = create_video_audio_derivative(self.input.file_id, self.root, lock)
        self.assertNotEqual(first.file_id, second.file_id)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(
            resolve_storage_path(
                self.root, "DERIVED_AUDIO", first.storage_key
            ).read_bytes(),
            before,
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM processing_runs WHERE status='SUCCEEDED'"
            ),
            [(2,)],
        )

    def test_cancel_queued_task_records_terminal_without_worker_or_derived(self):
        with AppDataLock(self.root) as lock, self.assertRaises(InterruptedError):
            create_video_audio_derivative(
                self.input.file_id, self.root, lock, cancelled=lambda: True
            )
        row = self._query(
            "SELECT status,started_at,finished_at,error_code FROM processing_runs"
        )[0]
        self.assertEqual(row[0], "CANCELLED")
        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertEqual(row[3], "CANCELLED")
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='DERIVED_AUDIO'"),
            [(0,)],
        )

    def test_cancel_running_task_closes_native_tree_without_registering_pcm(self):
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 3

        with AppDataLock(self.root) as lock, self.assertRaises(InterruptedError):
            create_video_audio_derivative(
                self.input.file_id, self.root, lock, cancelled=cancel
            )
        row = self._query("SELECT status,started_at,finished_at FROM processing_runs")[
            0
        ]
        self.assertEqual(row[0], "CANCELLED")
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='DERIVED_AUDIO'"),
            [(0,)],
        )

    def test_write_failure_records_failed_and_preserves_original(self):
        with (
            AppDataLock(self.root) as lock,
            patch(
                "creator_loop.audio_derivative.OwnedWindowsFile.create_new",
                side_effect=OSError("simulated disk full"),
            ),
            self.assertRaises(OSError),
        ):
            create_video_audio_derivative(self.input.file_id, self.root, lock)
        row = self._query("SELECT status,finished_at,error_code FROM processing_runs")[
            0
        ]
        self.assertEqual(row[0], "FAILED")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2], "PCM_FAILED")
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='DERIVED_AUDIO'"),
            [(0,)],
        )
        self.assertEqual(
            hashlib.sha256(self.original.read_bytes()).hexdigest(), self.before
        )

    def test_missing_audio_is_task_failure_with_other_originals_retained(self):
        silent = intake_video_original(
            self.fixture.with_name("video-red-blue.mp4"), root=self.root
        )
        with AppDataLock(self.root) as lock, self.assertRaises(ValueError):
            create_video_audio_derivative(silent.file_id, self.root, lock)
        self.assertEqual(
            self._query("SELECT status FROM processing_runs"), [("FAILED",)]
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM asset_files WHERE role='ORIGINAL'"),
            [(2,)],
        )

    def test_released_coordination_refuses_without_creating_task(self):
        with AppDataLock(self.root) as lock:
            pass
        with self.assertRaises(RuntimeError):
            create_video_audio_derivative(self.input.file_id, self.root, lock)
        self.assertEqual(self._query("SELECT count(*) FROM processing_runs"), [(0,)])
