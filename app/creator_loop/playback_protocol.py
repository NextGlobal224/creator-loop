"""Bounded private playback records; raw RGBA/PCM never enter user logs."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

HEADER = struct.Struct("!4sII")
MAX_METADATA = 4096
MAX_PIXELS = 8_294_400
MAX_VIDEO = MAX_PIXELS * 4
MAX_AUDIO = 1024 * 1024
MAX_BUFFER = HEADER.size + MAX_METADATA + MAX_VIDEO + 4 * 1024 * 1024


@dataclass(frozen=True)
class PlaybackPacket:
    metadata: dict[str, Any]
    payload: bytes


def _integer(body: dict[str, Any], key: str, low: int, high: int) -> int:
    value = body.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid playback {key}")
    return value


def _validate(body: dict[str, Any], length: int) -> None:
    if type(body.get("format")) is not int or body["format"] != 1:
        raise ValueError("Unsupported playback format")
    kind = body.get("kind")
    if kind == "video":
        width = _integer(body, "width", 1, MAX_PIXELS)
        height = _integer(body, "height", 1, MAX_PIXELS)
        if width * height > MAX_PIXELS or length != width * height * 4:
            raise ValueError("Playback frame exceeds pixel/byte budget")
        _integer(body, "time_us", 0, 2**63 - 1)
    elif kind == "audio":
        _integer(body, "rate", 1, 192000)
        channels = _integer(body, "channels", 1, 32)
        sample = body.get("sample")
        if sample not in ("UInt8", "Int16", "Int32", "Float"):
            raise ValueError("Invalid playback sample format")
        width = {"UInt8": 1, "Int16": 2, "Int32": 4, "Float": 4}[sample]
        if not 0 < length <= MAX_AUDIO or length % (channels * width):
            raise ValueError("Invalid playback PCM length")
        _integer(body, "time_us", 0, 2**63 - 1)
    elif kind == "status":
        if length or body.get("state") not in (
            "LOADING",
            "PLAYING",
            "PAUSED",
            "ENDED",
            "ERROR",
            "HEARTBEAT",
        ):
            raise ValueError("Invalid playback status")
        _integer(body, "position_ms", 0, 2**63 - 1)
        _integer(body, "duration_ms", 0, 2**63 - 1)
        message = body.get("message", "")
        if not isinstance(message, str) or len(message) > 512:
            raise ValueError("Invalid playback error message")
    else:
        raise ValueError("Unknown playback record")


def encode_packet(body: dict[str, Any], payload: bytes = b"") -> bytes:
    _validate(body, len(payload))
    metadata = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(metadata) <= MAX_METADATA:
        raise ValueError("Playback metadata exceeds budget")
    return HEADER.pack(b"CLP1", len(metadata), len(payload)) + metadata + payload


class PlaybackPacketReader:
    """Reject announced oversize before buffering its body; support split reads."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.failed = False

    def feed(self, data: bytes) -> list[PlaybackPacket]:
        if self.failed:
            raise ValueError("Playback protocol is already failed")
        try:
            if len(data) > 4 * 1024 * 1024 or len(self.buffer) + len(data) > MAX_BUFFER:
                raise ValueError("Playback receive buffer exceeds budget")
            self.buffer.extend(data)
            packets = []
            while len(self.buffer) >= HEADER.size:
                magic, meta_size, payload_size = HEADER.unpack_from(self.buffer)
                if (
                    magic != b"CLP1"
                    or not 0 < meta_size <= MAX_METADATA
                    or payload_size > MAX_VIDEO
                ):
                    raise ValueError("Invalid playback record header")
                meta_end = HEADER.size + meta_size
                if len(self.buffer) < meta_end:
                    break
                body = json.loads(bytes(self.buffer[HEADER.size : meta_end]))
                if not isinstance(body, dict):
                    raise ValueError("Invalid playback metadata")
                _validate(body, payload_size)
                end = meta_end + payload_size
                if len(self.buffer) < end:
                    break
                packets.append(PlaybackPacket(body, bytes(self.buffer[meta_end:end])))
                del self.buffer[:end]
            return packets
        except (ValueError, UnicodeError, RecursionError):
            self.failed = True
            self.buffer.clear()
            raise

    def finish(self) -> None:
        if self.buffer or self.failed:
            raise ValueError("Playback pipe ended with an incomplete/invalid record")
