"""Bounded FFmpeg fixture adapter; not a Worker or production decoder.

The supplied executable must be explicitly identified by digest. This adapter
is for isolated local/CI work only, never for a browser or a Cloudflare job.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tempfile
import wave
from pathlib import Path

MAX_SOURCE = 8 * 1024 * 1024
MAX_DURATION_MS = 900_000
RATE = 8_000
FRAME_BYTES = 2
MAX_PCM = MAX_DURATION_MS * RATE * FRAME_BYTES // 1000
CHUNK_PCM = 119 * RATE * FRAME_BYTES


def decode_offline_audio(
    source: bytes, executable: Path, sha256: str
) -> tuple[bytes, ...]:
    """Decode bounded input into contiguous mono PCM WAV slices, without network.

    FFmpeg is not trusted for source provenance. Callers must retain the input,
    pin its own decoder identity, check output durations, and separately prove
    the decoder's appropriateness for a deployment and its cost boundary.
    """
    if not isinstance(source, bytes) or not 0 < len(source) <= MAX_SOURCE:
        raise ValueError("Audio source exceeds offline decoder limit")
    if (
        not isinstance(executable, Path)
        or not executable.is_absolute()
        or not executable.is_file()
    ):
        raise ValueError("Explicit decoder executable required")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Decoder digest required")
    with executable.open("rb") as decoder:
        actual_sha = hashlib.file_digest(decoder, "sha256").hexdigest()
    if actual_sha != sha256:
        raise ValueError("Decoder digest mismatch")
    with tempfile.TemporaryDirectory(prefix="creator-audio-fixture-") as directory:
        root = Path(directory)
        input_path, output_path = root / "source", root / "decoded.pcm"
        input_path.write_bytes(source)
        try:
            subprocess.run(
                [
                    str(executable),
                    "-nostdin",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(input_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-threads",
                    "1",
                    "-t",
                    "901",
                    "-ac",
                    "1",
                    "-ar",
                    str(RATE),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    "-fs",
                    str(MAX_PCM + RATE * FRAME_BYTES),
                    str(output_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Offline audio decoder failed or timed out") from exc
        size = output_path.stat().st_size
        if size == 0 or size > MAX_PCM or size % FRAME_BYTES:
            raise ValueError("Decoded audio duration or samples exceed limit")
        pcm = output_path.read_bytes()
    chunks = []
    for offset in range(0, len(pcm), CHUNK_PCM):
        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(FRAME_BYTES)
            writer.setframerate(RATE)
            writer.writeframes(pcm[offset : offset + CHUNK_PCM])
        chunks.append(out.getvalue())
    return tuple(chunks)
