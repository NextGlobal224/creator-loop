"""Native inode retention, non-overwrite and failure evidence on own fixtures."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_database import (
    _hold_corrupt_source_locked,
    hold_corrupt_database,
)
from creator_loop.corrupt_source_retention import hold_existing_corrupt_source
from creator_loop.paths import ensure_data_root
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_files import RuntimeHandle


class CorruptSourceRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Giữ nguồn có space"
        ensure_data_root(self.root)
        self.source = self.root / "creator_loop.sqlite3"
        self.source.write_bytes(b"KEEP DAMAGED SOURCE\x00\xff")
        self.destination = self.root / "backups" / ("corrupt-original-" + "1" * 32)
        self.destination.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(AppDataLock(self.root))
        if sys.platform == "win32":
            for directory in (self.root, self.root / "backups", self.destination):
                self.stack.enter_context(
                    RuntimeHandle(directory, directory=True, allow_child_writes=True)
                )

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_exact_inode_moves_without_writes_and_stays_exclusive_until_release(self):
        before = self.source.stat()
        with hold_existing_corrupt_source(self.source) as pin:
            self.assertEqual(pin.stream.read(), b"KEEP DAMAGED SOURCE\x00\xff")
            with self.assertRaises(io.UnsupportedOperation):
                pin.stream.write(b"NEVER WRITE")
            for action in (
                lambda: self.source.write_bytes(b"NEVER WRITE"),
                self.source.unlink,
            ):
                with self.assertRaises(OSError):
                    action()
            target = pin.retain(self.destination)
            self.assertFalse(self.source.exists())
            self.assertEqual(
                (target.stat().st_dev, target.stat().st_ino),
                (before.st_dev, before.st_ino),
            )
            with self.assertRaises(OSError):
                target.write_bytes(b"NEVER WRITE")
            with self.assertRaises(OSError):
                target.unlink()
            with self.assertRaises(RuntimeError):
                pin.retain(self.destination)
        self.assertEqual(target.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_all_four_original_files_can_be_held_then_retained_separately(self):
        paths = [
            self.root / ("creator_loop.sqlite3" + suffix)
            for suffix in ("", "-wal", "-shm", "-journal")
        ]
        for index, path in enumerate(paths):
            path.write_bytes(f"KEEP {index}".encode())
        with ExitStack() as handles:
            pins = [
                handles.enter_context(hold_existing_corrupt_source(path))
                for path in paths
            ]
            for pin in pins:
                pin.retain(self.destination)
        self.assertEqual(
            {p.name for p in self.destination.iterdir()}, {p.name for p in paths}
        )
        for index, path in enumerate(paths):
            self.assertFalse(path.exists())
            self.assertEqual(
                (self.destination / path.name).read_bytes(), f"KEEP {index}".encode()
            )

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_destination_collision_never_overwrites_either_file(self):
        target = self.destination / self.source.name
        target.write_bytes(b"KEEP PREEXISTING")
        with hold_existing_corrupt_source(self.source) as pin:
            with self.assertRaises(OSError):
                pin.retain(self.destination)
        self.assertEqual(self.source.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")
        self.assertEqual(target.read_bytes(), b"KEEP PREEXISTING")

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_readonly_lease_gap_cannot_be_skipped_or_steal_another_holder(self):
        with _open_read_lock(self.source):
            with self.assertRaises(OSError):
                with hold_existing_corrupt_source(self.source):
                    pass
        self.assertEqual(self.source.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_same_app_lock_survives_readonly_to_exclusive_source_lease_transition(self):
        with _hold_corrupt_source_locked(self.root) as damage:
            self.assertFalse(damage["restore_authorized"])
            with self.assertRaises(OSError):
                with hold_existing_corrupt_source(self.source):
                    pass
        with self.assertRaises(DataRootBusy):
            with hold_corrupt_database(self.root):
                pass
        with hold_existing_corrupt_source(self.source) as pin:
            self.assertEqual(pin.stream.read(), b"KEEP DAMAGED SOURCE\x00\xff")
        self.assertEqual(self.source.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_source_hardlink_and_wrong_filename_refuse_without_mutation(self):
        alias = self.root / "source alias"
        os.link(self.source, alias)
        with self.assertRaises(ValueError):
            with hold_existing_corrupt_source(self.source):
                pass
        unknown = self.root / "unowned.txt"
        unknown.write_bytes(b"KEEP UNKNOWN")
        with self.assertRaises(ValueError):
            with hold_existing_corrupt_source(unknown):
                pass
        self.assertEqual(unknown.read_bytes(), b"KEEP UNKNOWN")

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_fs_sync_error_after_rename_keeps_actual_destination_and_no_success_path(
        self,
    ):
        with hold_existing_corrupt_source(self.source) as pin:
            with patch(
                "creator_loop.corrupt_source_preservation.os.fsync",
                side_effect=OSError("fixture sync failure"),
            ):
                with self.assertRaises(OSError):
                    pin.retain(self.destination)
            self.assertIsNone(pin.retained_path)
            # Caller must inspect actual state after a rename/fsync error.
            self.assertFalse(self.source.exists())
            pin.retained_path = self.destination / self.source.name
        self.assertEqual(
            (self.destination / self.source.name).read_bytes(),
            b"KEEP DAMAGED SOURCE\x00\xff",
        )

    @unittest.skipUnless(sys.platform == "win32", "exclusive native Windows source")
    def test_caller_failure_releases_handle_without_deleting_original(self):
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with hold_existing_corrupt_source(self.source):
                raise RuntimeError("caller failed")
        self.assertEqual(self.source.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")

    @unittest.skipIf(sys.platform == "win32", "portable refusal")
    def test_portable_refuses_mutation_without_touching_source(self):
        with self.assertRaises(OSError):
            with hold_existing_corrupt_source(self.source):
                pass
        self.assertEqual(self.source.read_bytes(), b"KEEP DAMAGED SOURCE\x00\xff")
