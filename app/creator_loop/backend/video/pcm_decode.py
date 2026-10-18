"""Child-only streaming PCM conversion; caller owns Job/input/output lifetime.

Run in an owned decoder process with a native memory cap and deadline, never
on the GUI thread. Supply a held read-only input device and a NEW owned output
stream. This foundation does not register files, runs, Evidence or recovery.
"""

from __future__ import annotations

import math
import time
import wave
from dataclasses import dataclass
from typing import BinaryIO, Callable

from PySide6.QtCore import QCoreApplication, QIODevice, QUrl
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

MAX_PCM_FRAMES = (64 * 1024**2 - 44) // 2


@dataclass(frozen=True)
class DecodedPCM:
    frames: int
    duration_ms: int
    buffers: int


def decode_pcm_local(
    source: QIODevice,
    output: BinaryIO,
    *,
    max_frames: int = MAX_PCM_FRAMES,
    timeout_seconds: float = 120,
    cancelled: Callable[[], bool] = lambda: False,
) -> DecodedPCM:
    """Convert the complete zero-origin audio track to PCM16/16kHz/mono.

    Refuse gaps/overlap beyond one sample of timestamp rounding. No silent
    timeline stitching, truncation or hardware playback. Retain partial output
    on failure; only a successful caller may register it as derived media.
    Native cleanup/I/O is also bounded by the caller's process deadline.
    """
    if (
        type(max_frames) is not int
        or not 0 < max_frames <= MAX_PCM_FRAMES
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Invalid PCM conversion limits")
    if QCoreApplication.instance() is None or not source.isOpen():
        raise RuntimeError("PCM conversion requires Qt and a held input device")
    if output.tell() != 0 or output.seek(0, 2) != 0:
        raise ValueError("PCM output must be a new empty owned stream")
    if cancelled():
        raise InterruptedError("PCM conversion cancelled before startup")
    decoder = QAudioDecoder()
    desired = QAudioFormat()
    desired.setSampleRate(16000)
    desired.setChannelCount(1)
    desired.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    decoder.setAudioFormat(desired)
    decoder.setSourceDevice(source)
    done = []
    errors = []
    decoder.finished.connect(lambda: done.append(True))
    decoder.error.connect(lambda error: errors.append(str(error)))
    frames = count = 0
    deadline = time.perf_counter() + timeout_seconds

    def check() -> None:
        if cancelled():
            raise InterruptedError("PCM conversion cancelled")
        if time.perf_counter() >= deadline:
            raise TimeoutError("PCM conversion exceeded decoder deadline")
        if errors:
            raise ValueError("PCM decoder failed: " + decoder.errorString())

    try:
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            decoder.start()
            while not done or decoder.bufferAvailable():
                check()
                QCoreApplication.processEvents()
                while decoder.bufferAvailable():
                    check()
                    buffer = decoder.read()
                    actual = buffer.format()
                    if (
                        not buffer.isValid()
                        or actual.sampleRate() != 16000
                        or actual.channelCount() != 1
                        or actual.sampleFormat() != QAudioFormat.SampleFormat.Int16
                        or not 0 < buffer.byteCount() <= 1024**2
                        or buffer.byteCount() != buffer.frameCount() * 2
                    ):
                        raise ValueError("Unexpected PCM buffer format/size")
                    if (
                        buffer.startTime() < 0
                        or abs(buffer.startTime() * 16000 - frames * 1000000) >= 1000000
                    ):
                        raise ValueError(
                            "PCM timeline has a gap/overlap/nonzero origin"
                        )
                    if frames + buffer.frameCount() > max_frames:
                        raise ValueError(
                            "PCM output exceeds task budget; no truncation"
                        )
                    chunk = bytes(buffer.constData())
                    if len(chunk) != buffer.byteCount():
                        raise ValueError("Truncated PCM buffer")
                    wav.writeframesraw(chunk)
                    frames += buffer.frameCount()
                    count += 1
                time.sleep(0.001)
            check()
            if not frames:
                raise ValueError("No decoded audio samples")
        return DecodedPCM(frames, (frames * 1000 + 15999) // 16000, count)
    finally:
        decoder.stop()
        decoder.setSource(QUrl())
