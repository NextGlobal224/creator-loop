"""Private Qt playback host: inherited readonly original, bounded binary output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFile, QIODevice, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioOutput,
    QMediaPlayer,
    QVideoFrame,
    QVideoSink,
)

from creator_loop.playback_protocol import MAX_AUDIO, MAX_PIXELS, encode_packet


def _read_control(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(4097)
    if not 0 < len(data) <= 4096:
        raise ValueError("Playback request exceeds budget")
    body = json.loads(data)
    if (
        not isinstance(body, dict)
        or type(body.get("format")) is not int
        or body["format"] != 1
    ):
        raise ValueError("Invalid playback request")
    return body


def run_playback_worker(request: Path) -> int:
    if sys.platform != "win32":
        return 2
    import msvcrt

    msvcrt.setmode(1, os.O_BINARY)
    body = _read_control(request)
    start, end, duration = (
        body.get(name) for name in ("start_ms", "end_ms", "duration_ms")
    )
    if any(type(value) is not int for value in (start, end, duration)):
        return 2
    assert isinstance(start, int) and isinstance(end, int) and isinstance(duration, int)
    if (
        not 0 <= start < end <= duration
        or type(body.get("size")) is not int
        or body["size"] <= 0
        or not isinstance(body.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", body["sha256"])
        or type(body.get("audio")) is not bool
    ):
        return 2
    app = QGuiApplication([])
    device = QFile()
    if not device.open(
        0, QIODevice.OpenModeFlag.ReadOnly, QFile.FileHandleFlag.DontCloseHandle
    ):
        return 2
    player = QMediaPlayer()
    video, pcm, audio = QVideoSink(), QAudioBufferOutput(), QAudioOutput()
    player.setVideoSink(video)
    player.setAudioBufferOutput(pcm)
    if body["audio"]:
        audio.setVolume(1.0)
        player.setAudioOutput(audio)
    state: dict[str, Any] = {
        "started": False,
        "ended": False,
        "failed": False,
        "seq": 0,
        "progress": time.monotonic(),
        "position": -1,
        "range_done": False,
    }

    def send(metadata: dict[str, Any], payload: bytes = b"") -> None:
        # A stalled consumer can block only this owned child, never the GUI.
        wire = memoryview(encode_packet({"format": 1, **metadata}, payload))
        while wire:
            count = os.write(1, wire)
            if count <= 0:
                raise OSError("Playback pipe closed")
            wire = wire[count:]

    def status(name: str, message: str = "") -> None:
        send(
            {
                "kind": "status",
                "state": name,
                "position_ms": max(0, player.position()),
                "duration_ms": max(0, player.duration()),
                "message": message[:512],
            }
        )

    def fail(message: str) -> None:
        if not state["failed"]:
            state["failed"] = True
            status("ERROR", message)
            app.quit()

    def frame_ready(frame: QVideoFrame) -> None:
        if (
            not frame.isValid()
            or state["ended"]
            or state["range_done"]
            or not state["started"]
        ):
            return
        try:
            timestamp = max(0, frame.startTime())
            if not start * 1000 <= timestamp < end * 1000:
                return
            if frame.width() * frame.height() > MAX_PIXELS:
                raise ValueError("Playback frame exceeds 4K pixel budget")
            image = frame.toImage()
            if image.isNull():
                raise ValueError("Playback frame cannot be read")
            if image.width() > 960 or image.height() > 540:
                image = image.scaled(
                    960,
                    540,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            send(
                {
                    "kind": "video",
                    "width": image.width(),
                    "height": image.height(),
                    "time_us": timestamp,
                },
                bytes(image.constBits()),
            )
            state["progress"] = time.monotonic()
        except Exception as exc:
            fail(str(exc))

    def audio_ready(buffer: QAudioBuffer) -> None:
        if (
            not buffer.isValid()
            or state["ended"]
            or state["range_done"]
            or not state["started"]
        ):
            return
        try:
            if buffer.byteCount() > MAX_AUDIO:
                raise ValueError("Playback PCM exceeds local buffer budget")
            format_ = buffer.format()
            begin = max(0, buffer.startTime())
            rate = format_.sampleRate()
            first = max(0, ((start * 1000 - begin) * rate + 999999) // 1000000)
            last = min(buffer.frameCount(), ((end * 1000 - begin) * rate) // 1000000)
            if first >= last:
                return
            width = format_.bytesPerFrame()
            payload = bytes(buffer.constData())[first * width : last * width]
            send(
                {
                    "kind": "audio",
                    "rate": format_.sampleRate(),
                    "channels": format_.channelCount(),
                    "sample": format_.sampleFormat().name,
                    "time_us": begin + (first * 1000000 + rate - 1) // rate,
                },
                payload,
            )
            state["progress"] = time.monotonic()
        except Exception as exc:
            fail(str(exc))

    def media_status(value: QMediaPlayer.MediaStatus) -> None:
        if value == QMediaPlayer.MediaStatus.LoadedMedia and not state["started"]:
            if player.duration() != duration or end > player.duration():
                fail("Playback duration changed after source verification")
                return
            state["started"] = True
            player.setPosition(start)
            player.play()
            status("PLAYING")
        elif value == QMediaPlayer.MediaStatus.EndOfMedia:
            if end < duration:
                state["range_done"] = True
                status("PAUSED")
                return
            state["ended"] = True
            status("ENDED")
            app.quit()

    def tick() -> None:
        try:
            control = request.parent / "control.json"
            if control.exists():
                command = _read_control(control)
                seq = command.get("seq")
                if type(seq) is not int or seq < 1:
                    raise ValueError("Invalid playback command sequence")
                if seq > state["seq"]:
                    state["seq"] = seq
                    action = command.get("action")
                    if action == "stop":
                        app.quit()
                        return
                    if action == "pause":
                        player.pause()
                        status("PAUSED")
                    elif action == "play":
                        position = command.get("position_ms")
                        if type(position) is not int or not start <= position < end:
                            raise ValueError("Playback seek is outside verified range")
                        player.setPosition(position)
                        state["range_done"] = False
                        player.play()
                        status("PLAYING")
                    else:
                        raise ValueError("Unknown playback command")
                    state["progress"] = time.monotonic()
            position = player.position()
            if position != state["position"]:
                state["position"] = position
                state["progress"] = time.monotonic()
            if (
                state["started"]
                and end < duration
                and position >= end
                and player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            ):
                state["range_done"] = True
                player.pause()
                status("PAUSED")
            if (
                not state["started"]
                or player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            ) and time.monotonic() - state["progress"] > 8:
                fail("Playback made no progress within 8 seconds")
            else:
                status("HEARTBEAT")
        except Exception as exc:
            fail(str(exc))

    video.videoFrameChanged.connect(frame_ready)
    pcm.audioBufferReceived.connect(audio_ready)
    player.mediaStatusChanged.connect(media_status)
    player.errorOccurred.connect(lambda _code, message: fail(message))
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(20)
    timer.timeout.connect(tick)
    try:
        # Hash exactly the inherited locked handle, never reopen the path.
        status("LOADING")
        digest, size = hashlib.sha256(), 0
        device.seek(0)
        while not device.atEnd():
            chunk = device.read(1024 * 1024).data()
            if not chunk:
                raise OSError("Cannot read inherited playback source")
            digest.update(chunk)
            size += len(chunk)
        if size != body["size"] or digest.hexdigest() != body["sha256"]:
            raise ValueError("Playback source bytes changed after verification")
        device.seek(0)
        state["progress"] = time.monotonic()
        timer.start()
        player.setSourceDevice(device, QUrl.fromLocalFile(body["path"]))
        app.exec()
    except Exception as exc:
        fail(str(exc))
    finally:
        timer.stop()
        # Native cleanup may block here; the parent retains the original and
        # terminates its private Job if exit/heartbeat exceeds the 8s deadline.
        player.stop()
        player.setSource(QUrl())
        device.close()
    return 2 if state["failed"] else 0
