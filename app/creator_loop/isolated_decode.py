"""Bound native Qt decoding/cleanup by an owned Windows process deadline."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QThread, qVersion
from PySide6.QtGui import QImage, QImageReader

from creator_loop import __version__
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.pcm_decode import MAX_PCM_FRAMES
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker

MAX_FRAME_PIXELS = 8_294_400  # one 4K frame; no full-video copy/cache
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_DECODER_MEMORY = 512 * 1024 * 1024
_decoding = False


def decode_isolated(
    path: Path,
    root: Path,
    *,
    mode: str,
    start_ms: int,
    end_ms: int | None = None,
    timeout_seconds: float = 8,
    require_audio: bool = False,
    source: BinaryIO | None = None,
    max_pcm_frames: int = MAX_PCM_FRAMES,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[dict[str, Any], QImage | None]:
    """Keep the original handle locked until the owned tree has exited.

    Borrowed sources must be flushed and exclusively used during this call.
    The shared file position is restored after native child cleanup.
    """
    global _decoding
    if sys.platform != "win32":
        raise OSError("Isolated Qt decoder requires Windows")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise ValueError("Decoder deadline must be finite and between 0 and 120s")
    if (
        mode not in ("video", "audio", "pcm")
        or type(start_ms) is not int
        or start_ms < 0
    ):
        raise ValueError("Invalid decoder mode/position")
    if mode == "pcm" and (
        start_ms != 0
        or end_ms is not None
        or type(max_pcm_frames) is not int
        or not 0 < max_pcm_frames <= MAX_PCM_FRAMES
    ):
        raise ValueError("PCM requires a complete zero-origin track and bounded output")
    if mode == "audio" and (type(end_ms) is not int or end_ms <= start_ms):
        raise ValueError("Invalid decoder audio range")
    if _decoding:
        raise RuntimeError("Another media decoder is already running")
    if cancelled() or QThread.currentThread().isInterruptionRequested():
        raise InterruptedError("Media decoder cancelled before startup")
    canonical = Path(root).resolve(strict=True)
    for name in ("runtime", "logs"):
        folder = canonical / name
        if folder.is_symlink() or folder.is_junction():
            raise OSError("Decoder requires real runtime/log directories")
        folder.mkdir(exist_ok=True)
    _decoding = True
    owned_source = source is None
    held = None
    saved_position = None
    try:
        held = _open_read_lock(path) if source is None else source
        held.flush()
        saved_position = held.tell()
        held.seek(0)
        with tempfile.TemporaryDirectory(
            prefix="decode-", dir=canonical / "runtime"
        ) as work:
            directory = Path(work)
            component = f"qt-decoder/{__version__}/Qt-{qVersion()}"
            marker = create_workspace_marker(canonical, directory, component)
            request = directory / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "mode": mode,
                        "path": str(path),
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "require_audio": require_audio,
                        "timeout_seconds": timeout_seconds,
                        **({"max_pcm_frames": max_pcm_frames} if mode == "pcm" else {}),
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
            deadline = time.monotonic() + timeout_seconds
            shutdown_cancelled = [False]
            with OwnedWindowsProcess(
                Path(sys.executable),
                [*prefix, "--decode-media", str(request)],
                canonical / "logs" / f"decode-{uuid4().hex}",
                component_version=component,
                environment=environment,
                capture_output=False,
                stdin_source=held,
                memory_limit_bytes=MAX_DECODER_MEMORY,
                before_resume=lambda child: bind_workspace_child(
                    directory, marker, child
                ),
            ) as process:

                def cancel() -> None:
                    shutdown_cancelled[0] = True
                    process.close()

                app = QCoreApplication.instance()
                if app is not None:
                    app.aboutToQuit.connect(cancel)
                try:
                    while True:
                        if (
                            cancelled()
                            or QThread.currentThread().isInterruptionRequested()
                        ):
                            raise InterruptedError("Media decoder cancelled by user")
                        if shutdown_cancelled[0]:
                            raise InterruptedError(
                                "Media decoder cancelled on app shutdown"
                            )
                        outcome = process.poll()
                        if outcome is not None:
                            break
                        if time.monotonic() >= deadline:
                            process.stop(124)
                            raise TimeoutError(
                                "Media decoder exceeded its process deadline"
                            )
                        if app is not None:
                            QCoreApplication.processEvents()
                        time.sleep(0.01)
                finally:
                    if app is not None:
                        app.aboutToQuit.disconnect(cancel)
            if cancelled() or QThread.currentThread().isInterruptionRequested():
                raise InterruptedError("Media decoder cancelled by user")
            response = directory / "response.json"
            if not response.is_file() or response.stat().st_size > 4096:
                raise ValueError(
                    f"Media decoder exited without a result ({outcome.exit_code})"
                )
            body = json.loads(response.read_text(encoding="utf-8"))
            if not isinstance(body, dict) or body.get("format") != 1:
                raise ValueError("Invalid media decoder result")
            if outcome.exit_code != 0 or body.get("error") is not None:
                message = str(body.get("error", "Media decoder failed"))[:512]
                if body.get("timeout") is True:
                    raise TimeoutError(message)
                raise ValueError(message)
            if mode == "pcm":
                frames = body.get("frames")
                if (
                    type(frames) is not int
                    or not 0 < frames <= max_pcm_frames
                    or type(body.get("duration_ms")) is not int
                    or body["duration_ms"] != (frames * 1000 + 15999) // 16000
                ):
                    raise ValueError("Invalid PCM result frames/duration")
                pcm = directory / "audio.wav"
                if pcm.is_symlink() or pcm.resolve(strict=True) != pcm:
                    raise ValueError("PCM result must be a regular owned file")
                with _open_read_lock(pcm) as output:
                    info = os.fstat(output.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size != 44 + frames * 2
                    ):
                        raise ValueError("Invalid PCM result size/type")
                    raw = output.read(44 + max_pcm_frames * 2 + 1)
                header = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF",
                    36 + frames * 2,
                    b"WAVE",
                    b"fmt ",
                    16,
                    1,
                    1,
                    16000,
                    32000,
                    2,
                    16,
                    b"data",
                    frames * 2,
                )
                if (
                    len(raw) != info.st_size
                    or raw[:44] != header
                    or hashlib.sha256(raw).hexdigest() != body.get("pcm_sha256")
                ):
                    raise ValueError("Invalid PCM result header/digest")
                body["pcm_wav"] = raw
                return body, None
            if (
                type(body.get("duration_ms")) is not int
                or body["duration_ms"] <= start_ms
            ):
                raise ValueError("Invalid decoded duration")
            if mode == "audio":
                begin, finish = body.get("buffer_start_ms"), body.get("buffer_end_ms")
                if (
                    type(begin) is not int
                    or type(finish) is not int
                    or end_ms is None
                    or not 0 <= begin < finish
                    or begin >= end_ms
                    or finish <= start_ms
                    or body["duration_ms"] < end_ms
                ):
                    raise ValueError("Invalid decoded audio buffer range")
                return body, None
            frame_time = body.get("frame_time_ms")
            if (
                type(frame_time) is not int
                or not start_ms <= frame_time < body["duration_ms"]
            ):
                raise ValueError("Invalid decoded frame timestamp")
            frame = directory / "frame.png"
            if not frame.is_file() or not 0 < frame.stat().st_size <= MAX_FRAME_BYTES:
                raise ValueError("Decoded frame exceeds local byte budget")
            if hashlib.sha256(frame.read_bytes()).hexdigest() != body.get(
                "frame_sha256"
            ):
                raise ValueError("Decoded frame digest mismatch")
            reader = QImageReader(str(frame))
            try:
                reader.setDecideFormatFromContent(True)
                size = reader.size()
                if (
                    not size.isValid()
                    or size.width() * size.height() > MAX_FRAME_PIXELS
                ):
                    raise ValueError("Decoded frame exceeds local pixel budget")
                image = reader.read()
            finally:
                del reader  # release Qt file handle on success and failure
            if image.isNull():
                raise ValueError("Decoded frame cannot be opened")
            return body, image
    finally:
        try:
            if held is not None:
                if owned_source:
                    held.close()
                elif saved_position is not None:
                    held.seek(saved_position)
        finally:
            _decoding = False


def run_decode_worker(request: Path) -> int:
    """Private packaged/source entry point; never initializes a user DB."""
    from PySide6.QtCore import QFile, QIODevice
    from PySide6.QtGui import QGuiApplication

    if request.stat().st_size > 4096:
        return 2
    body = json.loads(request.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("format") != 1:
        return 2
    app = QGuiApplication.instance() or QGuiApplication([])
    device = QFile()
    result: dict[str, Any] = {"format": 1}
    code = 0
    try:
        if not device.open(
            0, QIODevice.OpenModeFlag.ReadOnly, QFile.FileHandleFlag.DontCloseHandle
        ):
            raise OSError("Cannot open inherited read-only media handle")
        device.seek(0)
        path = Path(body["path"])
        if body["mode"] == "video":
            from creator_loop.video_evidence import _decode_video_local

            decoded = _decode_video_local(
                path,
                start_ms=body["start_ms"],
                timeout_seconds=body["timeout_seconds"],
                device=device,
                require_audio=body["require_audio"],
            )
            frame = request.parent / "frame.png"
            # The .png suffix selects the codec without PySide6's inconsistent
            # explicit-format runtime/stub overload.
            if not decoded.image.save(str(frame)):
                raise OSError("Cannot write private decoded frame")
            result.update(
                duration_ms=decoded.duration_ms,
                frame_time_ms=decoded.frame_time_ms,
                frame_sha256=hashlib.sha256(frame.read_bytes()).hexdigest(),
            )
        elif body["mode"] == "audio":
            from creator_loop.audio_evidence import _decode_audio_local

            decoded_audio = _decode_audio_local(
                path,
                device,
                start_ms=body["start_ms"],
                end_ms=body["end_ms"],
                timeout_seconds=body["timeout_seconds"],
            )
            result.update(
                duration_ms=decoded_audio.duration_ms,
                buffer_start_ms=decoded_audio.buffer_start_ms,
                buffer_end_ms=decoded_audio.buffer_end_ms,
            )
        elif body["mode"] == "pcm":
            from creator_loop.pcm_decode import decode_pcm_local

            if body.get("start_ms") != 0 or body.get("end_ms") is not None:
                raise ValueError("PCM worker requires the complete zero-origin track")
            target = request.parent / "audio.wav"
            with target.open("xb") as output:
                pcm = decode_pcm_local(
                    device,
                    output,
                    max_frames=body["max_pcm_frames"],
                    timeout_seconds=body["timeout_seconds"],
                )
                output.flush()
                os.fsync(output.fileno())
            result.update(
                frames=pcm.frames,
                duration_ms=pcm.duration_ms,
                pcm_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            )
        else:
            raise ValueError("Invalid decoder mode")
    except (ValueError, OSError, TimeoutError) as error:
        result.update(error=str(error)[:512], timeout=isinstance(error, TimeoutError))
        code = 2
    finally:
        device.close()
    (request.parent / "response.json").write_text(json.dumps(result), encoding="utf-8")
    # Keep the Qt application alive through all local native cleanup above.
    _ = app
    return code
