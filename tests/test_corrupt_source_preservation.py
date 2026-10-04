"""Raw damage retention keeps live bytes on success, error, cancel and crash."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_database import hold_corrupt_database
from creator_loop.corrupt_source_preservation import (
    _create_file,
    _publish_new_manifest,
    preserve_corrupt_source,
)
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_files import RuntimeHandle


class CorruptSourcePreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "dữ liệu có space"
        ensure_data_root(self.root)
        self.source = self.root / "creator_loop.sqlite3"
        self.source.write_bytes(b"PRIVATE DAMAGED SOURCE\x00\xff" * 4096)
        for suffix in ("-wal", "-shm", "-journal"):
            self.source.with_name(self.source.name + suffix).write_bytes(
                b"PRIVATE RAW SIDECAR " + suffix.encode()
            )
        self.before = self.snapshot()
        self.damage = self.inspect()
        self.token = self.damage["damage_identity"]

    def inspect(self):
        with hold_corrupt_database(self.root) as damage:
            return damage

    def snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in self.root.glob("creator_loop.sqlite3*")
        }

    def folders(self):
        return sorted((self.root / "backups").glob("raw-source-*"))

    def assert_source_and_lock_preserved(self):
        self.assertEqual(self.snapshot(), self.before)
        with AppDataLock(self.root):
            pass

    def test_exact_binary_sidecars_manifest_and_prior_backups_kept_without_restore(
        self,
    ):
        previous = self.root / "backups" / ("1" * 32)
        previous.mkdir()
        sentinel = previous / "backup-manifest.json"
        sentinel.write_bytes(b"KEEP PRIOR BACKUP")
        for _ in range(2):
            manifest = preserve_corrupt_source(self.root, self.token)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(record["damage"], self.damage)
            self.assertEqual(record["damage_identity"], self.token)
            self.assertEqual(
                record["data_root_identity"], self.damage["data_root_identity"]
            )
            self.assertTrue(record["raw_bytes_verified"])
            for key in (
                "consistent_backup",
                "restore_authorized",
                "media_included",
                "wal_recoverability_assessed",
            ):
                self.assertIs(record[key], False)
            self.assertNotIn("PRIVATE", manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                {path.name for path in manifest.parent.iterdir()},
                set(self.before) | {manifest.name},
            )
            for name, body in self.before.items():
                self.assertEqual((manifest.parent / name).read_bytes(), body)
                self.assertEqual(
                    hashlib.sha256(body).hexdigest(),
                    next(
                        entry["sha256"]
                        for entry in record["damage"]["raw_files"]
                        if entry["name"] == name
                    ),
                )
        self.assertEqual(len(self.folders()), 2)
        self.assertEqual(sentinel.read_bytes(), b"KEEP PRIOR BACKUP")
        self.assert_source_and_lock_preserved()

    def test_missing_sidecar_not_created_and_inventory_explicit(self):
        self.source.with_name(self.source.name + "-shm").unlink()
        damage = self.inspect()
        manifest = preserve_corrupt_source(self.root, damage["damage_identity"])
        self.assertFalse((manifest.parent / (self.source.name + "-shm")).exists())
        self.assertEqual(
            next(
                entry for entry in damage["raw_files"] if entry["name"].endswith("-shm")
            ),
            {"name": self.source.name + "-shm", "present": False},
        )

    def test_stale_source_or_inventory_review_refuses_before_any_copy(self):
        wal = self.source.with_name(self.source.name + "-wal")
        wal.write_bytes(b"changed WAL since review")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            preserve_corrupt_source(self.root, self.token)
        self.assertEqual(self.folders(), [])
        self.assertEqual(wal.read_bytes(), b"changed WAL since review")

    def test_identical_raw_bytes_in_other_root_do_not_reuse_review(self):
        other = self.root.parent / "other root"
        ensure_data_root(other)
        for name, body in self.before.items():
            (other / name).write_bytes(body)
        with self.assertRaisesRegex(ValueError, "changed since review"):
            preserve_corrupt_source(other, self.token)
        self.assertEqual(list((other / "backups").iterdir()), [])
        self.assert_source_and_lock_preserved()

    def test_invalid_review_budget_and_immediate_cancel_do_not_create_archive(self):
        for token, options, error in (
            ("invalid", {}, ValueError),
            (self.token, {"timeout_seconds": float("inf")}, ValueError),
            (self.token, {"timeout_seconds": 0}, ValueError),
            (self.token, {"cancelled": lambda: True}, InterruptedError),
        ):
            with self.subTest(options=options), self.assertRaises(error):
                preserve_corrupt_source(self.root, token, **options)
        self.assertEqual(self.folders(), [])
        self.assert_source_and_lock_preserved()

    def test_elapsed_work_budget_refuses_before_copy_without_timer_resolution_assumption(
        self,
    ):
        with (
            patch(
                "creator_loop.corrupt_source_preservation.time.monotonic",
                side_effect=range(100),
            ),
            self.assertRaises(TimeoutError),
        ):
            preserve_corrupt_source(self.root, self.token, timeout_seconds=0.1)
        self.assertEqual(self.folders(), [])
        self.assert_source_and_lock_preserved()

    def test_busy_or_unknown_runtime_refuses_without_copy(self):
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            preserve_corrupt_source(self.root, self.token)
        unresolved = self.root / "runtime" / "unknown-owned-worker.json"
        unresolved.write_bytes(b"DO NOT GUESS PID OWNERSHIP")
        with self.assertRaises(RuntimeError):
            preserve_corrupt_source(self.root, self.token)
        self.assertEqual(self.folders(), [])
        self.assertEqual(unresolved.read_bytes(), b"DO NOT GUESS PID OWNERSHIP")
        self.assert_source_and_lock_preserved()

    def test_insufficient_capacity_refuses_before_folder_and_keeps_source(self):
        with (
            patch(
                "creator_loop.corrupt_source_preservation.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            self.assertRaises(OSError),
        ):
            preserve_corrupt_source(self.root, self.token)
        self.assertEqual(self.folders(), [])
        self.assert_source_and_lock_preserved()

    def test_file_flush_failure_keeps_partial_evidence_without_manifest(self):
        with (
            patch(
                "creator_loop.corrupt_source_preservation.os.fsync",
                side_effect=OSError("fixture disk failure"),
            ),
            self.assertRaises(OSError),
        ):
            preserve_corrupt_source(self.root, self.token)
        (folder,) = self.folders()
        self.assertFalse((folder / "raw-source-manifest.json").exists())
        self.assertEqual(
            (folder / self.source.name).read_bytes(), self.before[self.source.name]
        )
        self.assert_source_and_lock_preserved()

    def test_short_write_refuses_and_keeps_partial_bytes(self):
        def short_file(handles, path):
            real = _create_file(handles, path)
            return SimpleNamespace(write=lambda chunk: real.write(chunk[:1]))

        with (
            patch(
                "creator_loop.corrupt_source_preservation._create_file",
                side_effect=short_file,
            ),
            self.assertRaises(OSError),
        ):
            preserve_corrupt_source(self.root, self.token)
        (folder,) = self.folders()
        self.assertEqual(
            (folder / self.source.name).read_bytes(), self.before[self.source.name][:1]
        )
        self.assertFalse((folder / "raw-source-manifest.json").exists())
        self.assert_source_and_lock_preserved()

    def test_cancel_after_copy_keeps_unpublished_bytes_and_releases_all_handles(self):
        def cancelled():
            return bool(self.folders() and list(self.folders()[0].iterdir()))

        with self.assertRaises(InterruptedError):
            preserve_corrupt_source(self.root, self.token, cancelled=cancelled)
        (folder,) = self.folders()
        self.assertFalse((folder / "raw-source-manifest.json").exists())
        self.assertTrue(list(folder.iterdir()))
        self.assert_source_and_lock_preserved()
        self.source.write_bytes(self.before[self.source.name])

    def test_unknown_destination_entry_prevents_publication_without_cleanup(self):
        def create(handles, path):
            result = _create_file(handles, path)
            (path.parent / "unknown.txt").write_bytes(b"KEEP UNKNOWN")
            return result

        with (
            patch(
                "creator_loop.corrupt_source_preservation._create_file",
                side_effect=create,
            ),
            self.assertRaises(RuntimeError),
        ):
            preserve_corrupt_source(self.root, self.token)
        (folder,) = self.folders()
        self.assertEqual((folder / "unknown.txt").read_bytes(), b"KEEP UNKNOWN")
        self.assertFalse((folder / "raw-source-manifest.json").exists())
        self.assert_source_and_lock_preserved()

    def test_manifest_collision_never_overwrites_unknown_destination(self):
        def collide(temporary, destination, stream):
            destination.write_bytes(b"KEEP UNKNOWN MANIFEST")
            _publish_new_manifest(temporary, destination, stream)

        with (
            patch(
                "creator_loop.corrupt_source_preservation._publish_new_manifest",
                side_effect=collide,
            ),
            self.assertRaises(OSError),
        ):
            preserve_corrupt_source(self.root, self.token)
        (folder,) = self.folders()
        self.assertEqual(
            (folder / "raw-source-manifest.json").read_bytes(), b"KEEP UNKNOWN MANIFEST"
        )
        self.assertTrue((folder / "raw-source-manifest.json.pending").exists())
        self.assert_source_and_lock_preserved()

    @unittest.skipUnless(sys.platform == "win32", "native sharing proof")
    def test_publication_directory_allows_children_denies_rename_and_discard(self):
        folder = self.root / "backups"
        with RuntimeHandle(folder, directory=True, allow_child_writes=True) as held:
            child = folder / "new-child"
            child.write_bytes(b"new owned file")
            with self.assertRaises(OSError):
                folder.rename(folder.with_name("MOVED"))
            with self.assertRaises(RuntimeError):
                held.discard()
        self.assertEqual(child.read_bytes(), b"new owned file")
        with self.assertRaises(ValueError):
            RuntimeHandle(child, allow_child_writes=True)

    @unittest.skipUnless(sys.platform == "win32", "native sharing proof")
    def test_windows_retained_source_and_destination_deny_mutation(self):
        attempted = []

        def cancelled():
            for folder in self.folders():
                target = folder / self.source.name
                if target.exists() and not attempted:
                    for path in (self.source, target):
                        with self.assertRaises(OSError):
                            path.write_bytes(b"MUTATE")
                        with self.assertRaises(OSError):
                            path.unlink()
                    with self.assertRaises(OSError):
                        folder.rename(folder.with_name("REPLACED"))
                    attempted.append(True)
            return False

        manifest = preserve_corrupt_source(self.root, self.token, cancelled=cancelled)
        self.assertEqual(attempted, [True])
        self.assertEqual(
            (manifest.parent / self.source.name).read_bytes(),
            self.before[self.source.name],
        )
        self.assert_source_and_lock_preserved()

    @unittest.skipIf(sys.platform == "win32", "native lease prevents late source write")
    def test_portable_late_source_change_refuses_publication(self):
        changed = []

        def cancelled():
            if (
                self.folders()
                and len(list(self.folders()[0].iterdir())) == 4
                and not changed
            ):
                self.source.write_bytes(b"external late source change")
                changed.append(True)
            return False

        with self.assertRaises(RuntimeError):
            preserve_corrupt_source(self.root, self.token, cancelled=cancelled)
        self.assertEqual(self.source.read_bytes(), b"external late source change")
        (folder,) = self.folders()
        self.assertFalse((folder / "raw-source-manifest.json").exists())

    def test_actual_process_crash_before_and_after_manifest_keeps_raw_bytes(self):
        code = """
import os, sys
from pathlib import Path
import creator_loop.corrupt_source_preservation as module
mode, root, token = sys.argv[1:]
if mode == 'before':
    module.os.fsync = lambda fd: os._exit(31)
else:
    original = module._publish_new_manifest
    def published(*args):
        original(*args)
        os._exit(32)
    module._publish_new_manifest = published
module.preserve_corrupt_source(Path(root), token)
"""
        for mode, exit_code in (("before", 31), ("after", 32)):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, "-c", code, mode, str(self.root), self.token],
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                folder = next(
                    path
                    for path in self.folders()
                    if path not in getattr(self, "seen", [])
                )
                self.seen = self.folders()
                manifest = folder / "raw-source-manifest.json"
                self.assertEqual(manifest.exists(), mode == "after")
                if mode == "after":
                    self.assertTrue(
                        json.loads(manifest.read_text(encoding="utf-8"))[
                            "raw_bytes_verified"
                        ]
                    )
                    for name, body in self.before.items():
                        self.assertEqual((folder / name).read_bytes(), body)
                else:
                    self.assertTrue(list(folder.iterdir()))
                self.assert_source_and_lock_preserved()

    def test_cli_success_and_refusal_never_open_initialize_or_restore_database(self):
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--preserve-corrupt-source",
            self.token,
        ]
        mixed = subprocess.run(
            command + ["--smoke"], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(mixed.returncode, 2, mixed.stderr)
        self.assertEqual(self.folders(), [])
        saved = subprocess.run(command, env=env, capture_output=True, timeout=20)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        record = json.loads(saved.stdout)
        self.assertTrue(record["raw_source_preserved"])
        self.assertFalse(record["consistent_backup"])
        self.assertFalse(record["restored"])
        self.assertTrue((self.root / record["raw_source_manifest"]).is_file())
        self.assert_source_and_lock_preserved()
        self.source.unlink()  # own synthetic source; missing-source route must not create DB
        missing = subprocess.run(command, env=env, capture_output=True, timeout=15)
        self.assertEqual(missing.returncode, 4, missing.stderr)
        self.assertFalse(self.source.exists())
        self.assertNotIn(b"PRIVATE", missing.stderr + saved.stdout)
