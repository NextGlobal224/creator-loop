"""Offline container identification, not evidence of browser decoding or ASR."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.knowledge.audio_playback import playback_audio_type


def box(kind, body):
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def mp3():
    # Two structurally valid MPEG-1 Layer III frames, 128 kbps / 44.1 kHz.
    frame = bytes.fromhex("fffb9000") + bytes(413)
    return frame * 2


def m4a(handler=b"soun"):
    track = box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + handler)))
    return box(b"ftyp", b"M4A " + bytes(4)) + box(b"moov", track) + box(b"mdat", b"x")


class AudioPlaybackTest(unittest.TestCase):
    def test_audio_containers_and_unknown(self):
        for content, expected in (
            (mp3(), "audio/mpeg"),
            (b"ID3\x04\0\0\0\0\0\0" + mp3(), "audio/mpeg"),
            (m4a(), "audio/mp4"),
            (m4a(b"vide"), None),
            (b"fake.mp3", None),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(playback_audio_type(content), expected)

    def test_rejects_truncated_or_forged_data(self):
        for content in (
            mp3()[:417],
            b"ID3\x04\0\0\xff\0\0\0" + mp3(),
            m4a()[:-1],
            box(b"ftyp", b"M4A " + bytes(4)) + box(b"mdat", b"x"),
            box(b"ftyp", b"M4A " + bytes(4))
            + box(b"moov", box(b"trak", b""))
            + box(b"mdat", b"x"),
            box(b"free", b"") * 4097,
        ):
            with self.subTest(size=len(content)):
                self.assertIsNone(playback_audio_type(content))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable"
    )
    def test_real_ten_minute_encoded_containers_when_encoder_available(self):
        # Synthetic silence; this is not a speech recognition quality test.
        with tempfile.TemporaryDirectory() as directory:
            for extension, codec, expected in (
                ("mp3", "libmp3lame", "audio/mpeg"),
                ("m4a", "aac", "audio/mp4"),
            ):
                path = Path(directory) / ("fixture." + extension)
                subprocess.run(
                    [
                        shutil.which("ffmpeg"),
                        "-nostdin",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=16000:cl=mono",
                        "-t",
                        "600",
                        "-threads",
                        "1",
                        "-c:a",
                        codec,
                        "-b:a",
                        "32k",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=90,
                )
                result = subprocess.run(
                    [
                        shutil.which("ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                duration = float(json.loads(result.stdout)["format"]["duration"])
                self.assertTrue(599 <= duration <= 602)
                self.assertLess(path.stat().st_size, 8 * 1024 * 1024)
                self.assertEqual(playback_audio_type(path.read_bytes()), expected)


if __name__ == "__main__":
    unittest.main()
