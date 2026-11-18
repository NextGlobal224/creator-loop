"""Bounded local decoder contract; subprocess is fake and never uses network."""

import hashlib
import io
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.knowledge.offline_audio_decoder import decode_offline_audio


class OfflineAudioDecoderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executable = Path(self.tmp.name) / "ffmpeg-fixture"
        self.executable.write_bytes(b"fixture executable, never run")
        self.digest = hashlib.sha256(self.executable.read_bytes()).hexdigest()

    def test_decoded_frames_are_split_without_loss(self):
        payload = bytes(range(100))

        def fake_run(args, **kwargs):
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
            self.assertEqual(kwargs["timeout"], 90)
            self.assertTrue(kwargs["check"])
            Path(args[-1]).write_bytes(payload)

        with (
            patch(
                "creator_loop.backend.knowledge.offline_audio_decoder.subprocess.run",
                side_effect=fake_run,
            ),
            patch("creator_loop.backend.knowledge.offline_audio_decoder.CHUNK_PCM", 32),
        ):
            chunks = decode_offline_audio(
                b"synthetic source", self.executable, self.digest
            )
        restored = b""
        for chunk in chunks:
            with wave.open(io.BytesIO(chunk), "rb") as reader:
                self.assertEqual(
                    (
                        reader.getnchannels(),
                        reader.getframerate(),
                        reader.getsampwidth(),
                    ),
                    (1, 8000, 2),
                )
                restored += reader.readframes(reader.getnframes())
        self.assertEqual(restored, payload)
        self.assertEqual(len(chunks), 4)

    def test_invalid_digest_timeout_and_oversize_fail_closed(self):
        with patch(
            "creator_loop.backend.knowledge.offline_audio_decoder.subprocess.run"
        ) as run:
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                decode_offline_audio(b"source", self.executable, "0" * 64)
            run.assert_not_called()
        with patch(
            "creator_loop.backend.knowledge.offline_audio_decoder.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 90),
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                decode_offline_audio(b"source", self.executable, self.digest)

        def oversized(args, **_kwargs):
            Path(args[-1]).write_bytes(b"x" * 102)

        with (
            patch(
                "creator_loop.backend.knowledge.offline_audio_decoder.subprocess.run",
                side_effect=oversized,
            ),
            patch("creator_loop.backend.knowledge.offline_audio_decoder.MAX_PCM", 100),
        ):
            with self.assertRaisesRegex(ValueError, "duration"):
                decode_offline_audio(b"source", self.executable, self.digest)
