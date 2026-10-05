"""Bounded PCM crosses the same owned decoder boundary as video/audio evidence."""

import hashlib
import io
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

if sys.platform == "win32":
    from creator_loop.isolated_decode import decode_isolated
    from creator_loop.paths import ensure_data_root
    from creator_loop.publication_media import _open_read_lock
    from PySide6.QtCore import QCoreApplication


@unittest.skipUnless(sys.platform == "win32", "Windows owned Qt decoder")
class IsolatedPCMTests(unittest.TestCase):
    def setUp(self):
        self.app = QCoreApplication.instance() or QCoreApplication([])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        ensure_data_root(self.root)
        self.fixture = Path(__file__).resolve().parent / "fixtures/video-with-tone.mp4"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    def test_pcm_result_is_verified_and_borrowed_original_position_restored(self):
        before = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        with _open_read_lock(self.fixture) as source:
            source.seek(17)
            body, frame = decode_isolated(
                self.fixture,
                self.root,
                mode="pcm",
                start_ms=0,
                source=source,
            )
            self.assertEqual(source.tell(), 17)
        self.assertIsNone(frame)
        with wave.open(io.BytesIO(body["pcm_wav"]), "rb") as wav:
            self.assertEqual(
                (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                (1, 2, 16000),
            )
            self.assertEqual(wav.getnframes(), body["frames"])
            self.assertTrue(any(wav.readframes(wav.getnframes())))
        self.assertEqual(
            hashlib.sha256(body["pcm_wav"]).hexdigest(), body["pcm_sha256"]
        )
        self.assertEqual(hashlib.sha256(self.fixture.read_bytes()).hexdigest(), before)
        self.assertFalse(
            any(
                path.name.startswith("decode-")
                for path in (self.root / "runtime").iterdir()
            )
        )
        self.assertFalse((self.root / "creator_loop.sqlite3").exists())

    def test_small_pcm_budget_refuses_without_returning_truncated_output(self):
        with self.assertRaisesRegex(ValueError, "budget"):
            decode_isolated(
                self.fixture, self.root, mode="pcm", start_ms=0, max_pcm_frames=1
            )

    def test_range_selection_cannot_relabel_pcm_timeline(self):
        with self.assertRaisesRegex(ValueError, "zero-origin"):
            decode_isolated(self.fixture, self.root, mode="pcm", start_ms=1)
        self.assertFalse(any((self.root / "runtime").iterdir()))

    def test_no_audio_refuses_without_creating_success(self):
        fixture = self.fixture.with_name("video-red-blue.mp4")
        with self.assertRaises(ValueError):
            decode_isolated(fixture, self.root, mode="pcm", start_ms=0)

    def test_short_native_deadline_refuses_and_releases_original(self):
        before = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        with self.assertRaises(TimeoutError):
            decode_isolated(
                self.fixture, self.root, mode="pcm", start_ms=0, timeout_seconds=0.01
            )
        self.assertEqual(hashlib.sha256(self.fixture.read_bytes()).hexdigest(), before)
