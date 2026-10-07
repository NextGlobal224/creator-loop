"""Child-side command publication races, independent of GUI/audio device timing."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.playback_child import _PlaybackControlReader, _read_control


class PlaybackControlReaderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "control.json"
        self.command = {"format": 1, "seq": 1, "action": "play", "position_ms": 0}
        self.path.write_text(json.dumps(self.command), encoding="utf-8")

    @unittest.skipUnless(sys.platform == "win32", "native Windows sharing")
    def test_native_delete_share_conflict_recovers_after_handle_closes(self):
        from creator_loop.runtime_files import RuntimeHandle

        reader = _PlaybackControlReader()
        original = self.path.read_bytes()
        with RuntimeHandle(self.path):
            with self.assertRaises(PermissionError) as caught:
                _read_control(self.path)
            self.assertEqual(caught.exception.errno, 13)
            self.assertIsNone(reader.poll(self.path))
        self.assertEqual(reader.poll(self.path), self.command)
        self.assertIsNone(reader.deadline)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(sys.platform == "win32", "native Windows sharing")
    def test_native_permanent_conflict_has_fixed_eight_second_deadline(self):
        from creator_loop.runtime_files import RuntimeHandle

        reader = _PlaybackControlReader()
        with RuntimeHandle(self.path):
            for now in (100.0, 107.99):
                with patch(
                    "creator_loop.playback_child.time.monotonic", return_value=now
                ):
                    self.assertIsNone(reader.poll(self.path))
                    self.assertEqual(reader.deadline, 108.0)
            with patch(
                "creator_loop.playback_child.time.monotonic", return_value=108.0
            ):
                with self.assertRaisesRegex(TimeoutError, "8 seconds"):
                    reader.poll(self.path)

    def test_absent_command_is_not_failure_and_valid_publish_is_read(self):
        reader = _PlaybackControlReader()
        self.path.unlink()
        self.assertIsNone(reader.poll(self.path))
        self.assertIsNone(reader.deadline)
        self.path.write_text(json.dumps(self.command), encoding="utf-8")
        self.assertEqual(reader.poll(self.path), self.command)

    def test_absence_after_conflict_does_not_erase_deadline(self):
        reader = _PlaybackControlReader()
        reader.deadline = 108.0
        self.path.unlink()
        with patch("creator_loop.playback_child.time.monotonic", return_value=108.0):
            with self.assertRaises(TimeoutError):
                reader.poll(self.path)

    def test_malformed_or_oversized_command_still_fails(self):
        reader = _PlaybackControlReader()
        for raw in ("{", '{"format":2}', " " * 4097):
            with self.subTest(raw=raw[:20]):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    reader.poll(self.path)

    def test_unrelated_permission_failure_is_not_retried(self):
        failure = PermissionError(13, "Privilege error")
        failure.winerror = 1314
        reader = _PlaybackControlReader()
        with patch("creator_loop.playback_child._read_control", side_effect=failure):
            with self.assertRaises(PermissionError) as caught:
                reader.poll(self.path)
        self.assertIs(caught.exception, failure)
        self.assertIsNone(reader.deadline)
