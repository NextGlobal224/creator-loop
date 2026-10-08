"""Reconcile only a bound known audio executor whose native owner is dead."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from creator_loop.app_lock import AppDataLock
from creator_loop.database import initialize
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root
from creator_loop.processing_recovery import recover_processing_startup
from creator_loop.processing_witness import record_processing_executor
from creator_loop.windows_paths import file_io_path


@unittest.skipUnless(sys.platform == "win32", "Windows native owner witness")
class ProcessingAudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO assets VALUES('video','VIDEO','Fixture','now',NULL)"
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES('original','video','ORIGINAL','storage/originals/video.mp4',?,1,'video/mp4','now')",
                ("0" * 64,),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at) VALUES('pcm','video','DERIVED_AUDIO','storage/derived/audio.wav',?,1,'audio/wav','original','now')",
                ("0" * 64,),
            )
            db.commit()
        self.raw = self.root / "storage/derived/retained-raw.json"
        self.raw.write_bytes(b"original immutable RAW")

    def _row(self, run, task="VIDEO_AUDIO_DECODE", status="RUNNING"):
        if task == "VIDEO_AUDIO_DECODE":
            file_id, tool, version, model, model_version = (
                "original",
                "PySide6 Qt audio decoder",
                "6.10.2",
                None,
                None,
            )
        else:
            file_id, tool, version, model, model_version = (
                "pcm",
                "whisper.cpp",
                "1.8.7",
                "Whisper base multilingual",
                "5359861c739e955e79d9a303bcbc70fb988958b1",
            )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,started_at,error_message,created_at) VALUES(?,'video',?,?,?,?,?,?,?,'now','existing context','now')",
                (run, file_id, task, status, tool, version, model, model_version),
            )
            db.commit()

    def _crashed_executor(self, task="VIDEO_AUDIO_DECODE"):
        run = uuid4().hex
        self._row(run, task)
        script = self.root / "executor.py"
        file_id = "original" if task == "VIDEO_AUDIO_DECODE" else "pcm"
        component = (
            "qt-pcm/6.10.2"
            if task == "VIDEO_AUDIO_DECODE"
            else "whisper.cpp/1.8.7/base-multilingual"
        )
        script.write_text(
            """import os,sys
from pathlib import Path
from creator_loop.app_lock import AppDataLock
from creator_loop.processing_witness import record_processing_executor
root=Path(sys.argv[1])
with AppDataLock(root):
    record_processing_executor(root,sys.argv[2],'video',sys.argv[3],sys.argv[4],sys.argv[5])
    os._exit(17)
""",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        with OwnedWindowsProcess(
            Path(sys._base_executable),
            [str(script), str(self.root), run, file_id, task, component],
            self.root / "executor-logs",
            component_version="test/processing-executor",
            environment=environment,
        ) as parent:
            self.assertEqual(
                parent.kernel.WaitForSingleObject(parent.process, 10000), 0
            )
            deadline = time.perf_counter() + 5
            while not parent.tree_finished() and time.perf_counter() < deadline:
                time.sleep(0.01)
            self.assertTrue(parent.tree_finished())
            self.assertEqual(parent.wait(1).exit_code, 17)
        return run

    def _recover(self):
        with AppDataLock(self.root) as lock:
            return recover_processing_startup(self.root, lock)

    def _status(self, run):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT status,finished_at,error_code,error_message FROM processing_runs WHERE run_id=?",
                (run,),
            ).fetchone()

    def test_deep_witness_real_dead_owner_recovery_keeps_raw(self):
        base = self.root
        prefix = "Deep processing witness tiếng Việt "
        deep = base / (prefix + "x" * (209 - len(str(base)) - 1 - len(prefix)))
        self.assertEqual(len(str(deep)), 209)
        self.assertTrue(deep.is_relative_to(base))
        ensure_data_root(deep)
        self.addCleanup(shutil.rmtree, file_io_path(deep))
        old_path, old_raw = self.path, self.raw
        self.root = deep
        self.path = deep / "creator_loop.sqlite3"
        shutil.copyfile(old_path, self.path)
        self.raw = deep / "storage/derived/retained-raw.json"
        self.raw.write_bytes(old_raw.read_bytes())
        run = self._crashed_executor()
        witness = deep / "runtime" / ("processing-" + run) / "task.json"
        self.assertGreater(len(str(witness)), 260)
        report = self._recover()
        self.assertEqual(report.interrupted_run_ids, (run,))
        self.assertEqual(self._status(run)[0], "FAILED")
        self.assertEqual(self._status(run)[2], "INTERRUPTED_AT_STARTUP")
        self.assertEqual(self.raw.read_bytes(), b"original immutable RAW")
        self.assertEqual(self._recover().interrupted_run_ids, ())

    def test_real_pcm_owner_crash_reconciles_run_without_deleting_raw(self):
        run = self._crashed_executor()
        witness = self.root / "runtime" / ("processing-" + run)
        before = {path.name: path.read_bytes() for path in witness.iterdir()}
        self.assertEqual(self._recover().interrupted_run_ids, (run,))
        row = self._status(run)
        self.assertEqual(row[0], "FAILED")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2], "INTERRUPTED_AT_STARTUP")
        self.assertIn("existing context", row[3])
        self.assertEqual(self.raw.read_bytes(), b"original immutable RAW")
        self.assertEqual(
            {path.name: path.read_bytes() for path in witness.iterdir()}, before
        )
        self.assertEqual(self._recover().interrupted_run_ids, ())

    def test_real_transcription_owner_crash_reconciles_only_bound_task(self):
        run = self._crashed_executor("AUDIO_TRANSCRIPTION")
        unknown = uuid4().hex
        self._row(unknown, "AUDIO_TRANSCRIPTION")
        result = self._recover()
        self.assertEqual(result.interrupted_run_ids, (run,))
        self.assertEqual(result.unverified_running_ids, (unknown,))
        self.assertEqual(self._status(unknown)[0], "RUNNING")

    def test_live_owner_witness_is_preserved(self):
        run = uuid4().hex
        self._row(run)
        record_processing_executor(
            self.root, run, "video", "original", "VIDEO_AUDIO_DECODE", "qt-pcm/6.10.2"
        )
        result = self._recover()
        self.assertEqual(result.interrupted_run_ids, ())
        self.assertEqual(result.unverified_running_ids, (run,))

    def test_mismatched_witness_never_certifies_dead_task(self):
        run = self._crashed_executor()
        path = self.root / "runtime" / ("processing-" + run) / "task.json"
        body = json.loads(path.read_bytes())
        body["input_file_id"] = "different-input"
        path.write_text(json.dumps(body), encoding="utf-8")
        result = self._recover()
        self.assertEqual(result.interrupted_run_ids, ())
        self.assertEqual(result.unverified_running_ids, (run,))

    def test_duplicate_metadata_and_extra_files_preserve_unknown_runs(self):
        run = self._crashed_executor()
        directory = self.root / "runtime" / ("processing-" + run)
        (directory / "task.json").write_bytes(b'{"format":1,"format":1}')
        result = self._recover()
        self.assertEqual(result.unverified_running_ids, (run,))
        (directory / "extra.raw").write_bytes(b"keep")
        self.assertEqual(self._recover().unverified_running_ids, (run,))
        self.assertEqual((directory / "extra.raw").read_bytes(), b"keep")
