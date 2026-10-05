"""Real CREATE_NEW and handle rollback beyond the legacy Windows path limit."""

import sys
import tempfile
import unittest
from pathlib import Path

from creator_loop.windows_owned_file import OwnedWindowsFile


@unittest.skipUnless(sys.platform == "win32", "native Windows owned-file handles")
class OwnedFilePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve(strict=True)
        padding = 245 - len(str(base)) - 1
        self.assertGreater(padding, 0)
        self.folder = base / ("d" * padding)
        self.folder.mkdir()
        self.path = self.folder / ("guard-archive-" + "a" * 32 + ".pending")
        self.assertGreater(len(str(self.path)), 260)
        # Independent observer spelling; do not reuse the implementation helper.
        self.observer = Path("\\\\?\\" + str(self.path))

    def test_long_create_is_exclusive_and_preserves_complete_bytes(self):
        owned = OwnedWindowsFile.create_new(self.path)
        try:
            owned.stream.write(b"complete private archive")
            owned.stream.seek(0)
            self.assertEqual(owned.stream.read(), b"complete private archive")
        finally:
            owned.close()
        with self.assertRaises(FileExistsError):
            OwnedWindowsFile.create_new(self.path)
        self.assertEqual(self.observer.read_bytes(), b"complete private archive")
        self.observer.unlink()

    def test_long_discard_deletes_only_the_still_owned_handle(self):
        sentinel = Path("\\\\?\\" + str(self.folder / "unrelated.keep"))
        self.addCleanup(sentinel.unlink, missing_ok=True)
        sentinel.write_bytes(b"Keep unrelated evidence")
        owned = OwnedWindowsFile.create_new(self.path)
        try:
            owned.stream.write(b"partial private archive")
            owned.discard()
        finally:
            owned.close()
        self.assertFalse(self.observer.exists())
        self.assertEqual(sentinel.read_bytes(), b"Keep unrelated evidence")
