"""Backup retains known dead audio evidence; refuses active/foreign/tampered data."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.backup_runtime import hold_terminal_audio_runtime
from creator_loop.database import initialize
from creator_loop.paths import ensure_data_root
from creator_loop.update_backup import create_update_backup

T = "2026-10-08T00:00:00Z"
MODEL = "5359861c739e955e79d9a303bcbc70fb988958b1"


@unittest.skipUnless(sys.platform == "win32", "Native retained runtime leases")
class RetainedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "QA retained"
        ensure_data_root(self.root)
        initialize(self.root / "creator_loop.sqlite3")
        self.db = sqlite3.connect(self.root / "creator_loop.sqlite3")
        self.addCleanup(self.db.close)
        self.asset, self.video, self.pcm = "a" * 32, "b" * 32, "c" * 32
        self.decode, self.asr, self.request = "d" * 32, "e" * 32, "f" * 32
        self.db.execute(
            "INSERT INTO assets VALUES(?,?,?,?,?)", (self.asset, "VIDEO", "QA", T, None)
        )
        import hashlib

        for file_id, role, key, mime, parent in (
            (self.video, "ORIGINAL", "storage/originals/test.mp4", "video/mp4", None),
            (
                self.pcm,
                "DERIVED_AUDIO",
                "storage/derived/test.wav",
                "audio/wav",
                self.video,
            ),
        ):
            path = self.root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            self.db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    self.asset,
                    role,
                    key,
                    hashlib.sha256(b"fixture").hexdigest(),
                    7,
                    mime,
                    parent,
                    T,
                ),
            )
        for run, file_id, task, tool, version, model, revision in (
            (
                self.decode,
                self.video,
                "VIDEO_AUDIO_DECODE",
                "PySide6 Qt audio decoder",
                "6.10.2",
                None,
                None,
            ),
            (
                self.asr,
                self.pcm,
                "AUDIO_TRANSCRIPTION",
                "whisper.cpp",
                "1.8.7",
                "Whisper base multilingual",
                MODEL,
            ),
        ):
            self.db.execute(
                "INSERT INTO processing_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    self.asset,
                    file_id,
                    task,
                    "SUCCEEDED",
                    tool,
                    version,
                    model,
                    revision,
                    T,
                    T,
                    None,
                    None,
                    T,
                ),
            )
        self.db.commit()
        self.gui = self.identity(99101, "11")
        self.task = {
            **self.identity(99102, "12"),
            "record_format": 1,
            "parent_pid": 99101,
            "component_version": "video-transcription/1",
            "run_id": "1" * 32,
            "job_id": "2" * 32,
        }
        self.engine = {
            **self.identity(99103, "13"),
            "record_format": 1,
            "parent_pid": 99102,
            "component_version": "whisper.cpp/1.8.7/base-multilingual",
            "run_id": "3" * 32,
            "job_id": "4" * 32,
        }
        self.create_records()

    def identity(self, pid, creation):
        return {
            "pid": pid,
            "creation_identity": creation,
            "creation_identity_format": "WIN32_FILETIME",
            "executable": str(Path(sys.executable).resolve()),
        }

    def write(self, directory, name, body):
        (directory / name).write_text(json.dumps(body), encoding="utf-8")

    def marker(self, directory, parent, component, kind):
        self.write(
            directory,
            "ownership.json",
            {
                "format": 1,
                "data_root": str(self.root),
                "workspace": directory.name,
                "component_version": component,
                "kind": kind,
                "parent": parent,
            },
        )

    def create_records(self):
        for run, file_id, task, comp in (
            (self.decode, self.video, "VIDEO_AUDIO_DECODE", "qt-pcm/6.10.2"),
            (
                self.asr,
                self.pcm,
                "AUDIO_TRANSCRIPTION",
                "whisper.cpp/1.8.7/base-multilingual",
            ),
        ):
            directory = self.root / "runtime" / ("processing-" + run)
            directory.mkdir()
            self.marker(directory, self.task, comp, "PROCESSING_EXECUTOR_WITNESS")
            self.write(
                directory,
                "task.json",
                {
                    "format": 1,
                    "run_id": run,
                    "asset_id": self.asset,
                    "input_file_id": file_id,
                    "task_type": task,
                    "component_version": comp,
                },
            )
        directory = self.root / "runtime" / ("transcribe-" + self.request)
        directory.mkdir()
        self.marker(
            directory, self.gui, "video-transcription/1", "VIDEO_TRANSCRIPTION_TASK"
        )
        self.write(directory, "child-ownership.json", self.task)
        self.write(
            directory,
            "request.json",
            {
                "format": 1,
                "request_id": self.request,
                "root": str(self.root),
                "file_id": self.video,
                "language": "vi",
                "worker_memory_mib": 768,
            },
        )
        directory = self.root / "runtime" / "whisper-abcdefgh"
        directory.mkdir()
        self.marker(
            directory,
            self.task,
            self.engine["component_version"],
            "WHISPER_TRANSCRIPTION_WORKSPACE",
        )
        self.write(directory, "child-ownership.json", self.engine)
        self.write(directory, "raw.json", {"segments": []})

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in (self.root / "runtime").rglob("*")
            if p.is_file() and p.name != "app-data.lock"
        }

    def guarded(self):
        return patch(
            "creator_loop.backup_runtime._identity_dead",
            side_effect=lambda identity: identity["pid"] != self.gui["pid"],
        )

    def test_backup_preserves_terminal_records_and_rows_with_live_gui_parent(self):
        before = self.snapshot()
        with self.guarded():
            target = create_update_backup(self.root)
        self.assertTrue((target / "backup-manifest.json").exists())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM processing_runs").fetchone()[0], 2
        )

    def test_unknown_directory_refuses_and_keeps_prior_evidence(self):
        (self.root / "runtime/foreign").mkdir()
        before = self.snapshot()
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_live_task_or_engine_refuses(self):
        for pid in (self.task["pid"], self.engine["pid"]):
            with (
                self.subTest(pid=pid),
                patch(
                    "creator_loop.backup_runtime._identity_dead",
                    side_effect=lambda value: (
                        value["pid"] not in (self.gui["pid"], pid)
                    ),
                ),
                self.assertRaises(RuntimeError),
            ):
                create_update_backup(self.root)

    def test_running_or_missing_run_refuses(self):
        for status in ("RUNNING", "QUEUED"):
            self.db.execute(
                "UPDATE processing_runs SET status=? WHERE run_id=?", (status, self.asr)
            )
            self.db.commit()
            with self.guarded(), self.assertRaises(RuntimeError):
                create_update_backup(self.root)

    def test_tampered_root_parent_child_and_extra_file_refuse(self):
        path = self.root / "runtime/whisper-abcdefgh/ownership.json"
        old = path.read_text()
        for key, value in (
            ("data_root", str(self.root.parent)),
            ("component_version", "foreign"),
            ("kind", "other"),
        ):
            body = json.loads(old)
            body[key] = value
            path.write_text(json.dumps(body))
            with self.subTest(key=key), self.guarded(), self.assertRaises(RuntimeError):
                create_update_backup(self.root)
        path.write_text(old)
        (path.parent / "unknown.txt").write_text("retain")
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_request_input_and_cancel_mismatch_refuse(self):
        directory = self.root / "runtime" / ("transcribe-" + self.request)
        self.write(
            directory,
            "cancel.json",
            {"format": 1, "request_id": "0" * 32, "cancel": True},
        )
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_boolean_witness_format_is_not_integer(self):
        directory = self.root / "runtime" / ("processing-" + self.asr)
        path = directory / "task.json"
        body = json.loads(path.read_text())
        body["format"] = True
        path.write_text(json.dumps(body))
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_runtime_held_against_mutation_through_snapshot(self):
        path = self.root / "runtime" / ("transcribe-" + self.request) / "request.json"
        with self.guarded(), hold_terminal_audio_runtime(self.root, self.db):
            with self.assertRaises(PermissionError):
                path.write_text("overwrite")
            with self.assertRaises(PermissionError):
                path.rename(path.with_name("moved.json"))

    def test_row_factory_supported(self):
        self.db.row_factory = sqlite3.Row
        with self.guarded(), hold_terminal_audio_runtime(self.root, self.db):
            pass

    def test_request_other_original_cannot_borrow_terminal_executor(self):
        self.db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) SELECT ?,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at FROM asset_files WHERE file_id=?",
            ("9" * 32, self.video),
        )
        self.db.commit()
        directory = self.root / "runtime" / ("transcribe-" + self.request)
        request = json.loads((directory / "request.json").read_text())
        request["file_id"] = "9" * 32
        self.write(directory, "request.json", request)
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_integer_cancel_flag_is_not_boolean_true(self):
        directory = self.root / "runtime" / ("transcribe-" + self.request)
        self.write(
            directory,
            "cancel.json",
            {"format": 1, "request_id": self.request, "cancel": 1},
        )
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_missing_terminal_run_cannot_grant_backup(self):
        self.db.execute("DELETE FROM processing_runs WHERE run_id=?", (self.asr,))
        self.db.commit()
        with self.guarded(), self.assertRaises(RuntimeError):
            create_update_backup(self.root)

    def test_empty_runtime_namespace_held_against_publication(self):
        import shutil

        for directory in list((self.root / "runtime").iterdir()):
            if directory.name != "app-data.lock":
                shutil.rmtree(directory)
        with hold_terminal_audio_runtime(self.root, self.db) as recheck:
            (self.root / "runtime" / ("transcribe-" + "8" * 32)).mkdir()
            with self.assertRaises(RuntimeError):
                recheck()

    def test_new_workspace_during_snapshot_refuses_before_publication(self):
        from creator_loop.update_backup import _inventory

        before = self.snapshot()

        def introduce_namespace(db, root):
            (self.root / "runtime" / ("transcribe-" + "7" * 32)).mkdir()
            return _inventory(db, root)

        with (
            self.guarded(),
            patch(
                "creator_loop.update_backup._inventory", side_effect=introduce_namespace
            ),
            self.assertRaises(RuntimeError),
        ):
            create_update_backup(self.root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list((self.root / "backups").iterdir()), [])
