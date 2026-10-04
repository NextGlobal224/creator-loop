"""Malformed native child output cannot allocate unbounded GUI buffers."""

import unittest

from creator_loop.playback_protocol import (
    HEADER,
    MAX_METADATA,
    MAX_VIDEO,
    PlaybackPacketReader,
    encode_packet,
)


class PlaybackProtocolTests(unittest.TestCase):
    def test_fragmented_real_binary_pixels_and_pcm_survive_exactly(self) -> None:
        video = {"format": 1, "kind": "video", "width": 2, "height": 1, "time_us": 13}
        audio = {
            "format": 1,
            "kind": "audio",
            "rate": 48000,
            "channels": 2,
            "sample": "Int16",
            "time_us": 19,
        }
        pixels, pcm = bytes((0, 13, 10, 255, 255, 0, 0, 255)), b"\x00\r\n\xff" * 12
        wire = encode_packet(video, pixels) + encode_packet(audio, pcm)
        reader, received = PlaybackPacketReader(), []
        for index in range(0, len(wire), 3):
            received.extend(reader.feed(wire[index : index + 3]))
        reader.finish()
        self.assertEqual(
            [(p.metadata, p.payload) for p in received], [(video, pixels), (audio, pcm)]
        )

    def test_header_budgets_fail_before_reading_announced_payload(self) -> None:
        for header in (
            HEADER.pack(b"BAD!", 1, 0),
            HEADER.pack(b"CLP1", MAX_METADATA + 1, 0),
            HEADER.pack(b"CLP1", 1, MAX_VIDEO + 1),
        ):
            reader = PlaybackPacketReader()
            with self.assertRaises(ValueError):
                reader.feed(header)
            self.assertFalse(reader.buffer)
            with self.assertRaises(ValueError):
                reader.feed(b"")

    def test_inconsistent_pixels_pcm_and_status_refuse(self) -> None:
        bodies = [
            (
                {
                    "format": 1,
                    "kind": "video",
                    "width": True,
                    "height": 1,
                    "time_us": 0,
                },
                b"1234",
            ),
            (
                {"format": 1, "kind": "video", "width": 2, "height": 1, "time_us": 0},
                b"1234",
            ),
            (
                {
                    "format": 1,
                    "kind": "audio",
                    "rate": 48000,
                    "channels": 2,
                    "sample": "Int16",
                    "time_us": 0,
                },
                b"123",
            ),
            (
                {
                    "format": 1,
                    "kind": "status",
                    "state": "ENDED",
                    "position_ms": -1,
                    "duration_ms": 1,
                },
                b"",
            ),
            (
                {
                    "format": 1,
                    "kind": "status",
                    "state": "PLAYING",
                    "position_ms": 0,
                    "duration_ms": 1,
                },
                b"secret",
            ),
        ]
        for body, payload in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                encode_packet(body, payload)

    def test_partial_header_or_body_cannot_be_successful_eof(self) -> None:
        wire = encode_packet(
            {"format": 1, "kind": "video", "width": 1, "height": 1, "time_us": 0},
            b"1234",
        )
        for size in (1, HEADER.size, len(wire) - 1):
            reader = PlaybackPacketReader()
            self.assertEqual(reader.feed(wire[:size]), [])
            with self.assertRaises(ValueError):
                reader.finish()
