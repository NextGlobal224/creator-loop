"""Durable journal replacement at the Windows path limit, with safe failure."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.update_preparation import _journal


class JournalPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve(strict=True)
        name = "corrupt-restore-" + "a" * 32 + ".json"
        padding = 250 - len(str(base)) - len(name) - 2
        self.assertGreater(padding, 0)
        self.folder = base / ("d" * padding)
        self.folder.mkdir()
        self.path = self.folder / name
        self.before = b'{"phase":"CORRUPT_COPY_GUARD_PENDING"}'
        self.path.write_bytes(self.before)
        self.sentinel = self.folder / "unrelated.keep"
        self.sentinel.write_bytes(b"Keep unrelated evidence")

    @unittest.skipUnless(sys.platform == "win32", "actual Windows path limit")
    def test_temporary_name_over_260_publishes_complete_deep_journal(self):
        self.assertEqual(len(str(self.path)), 250)
        self.assertGreater(len(str(self.path)) + 38, 260)
        record = {"phase": "CORRUPT_COPY_GUARDED", "label": "Đã giữ guard"}
        _journal(self.path, record)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), record)
        self.assertEqual(self.sentinel.read_bytes(), b"Keep unrelated evidence")
        self.assertEqual(
            {p.name for p in self.folder.iterdir()},
            {self.path.name, self.sentinel.name},
        )

    def test_failed_replacement_retains_original_and_cleans_only_owned_temporary(self):
        with patch.object(Path, "replace", side_effect=OSError("publication refused")):
            with self.assertRaises(OSError):
                _journal(self.path, {"phase": "CORRUPT_COPY_GUARDED"})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.sentinel.read_bytes(), b"Keep unrelated evidence")
        self.assertEqual(
            {p.name for p in self.folder.iterdir()},
            {self.path.name, self.sentinel.name},
        )
