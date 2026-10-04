"""GUI facade for actual playback in a retained, memory-bounded private Job."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl, Signal, qVersion
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QAudioOutput,
    QMediaPlayer,
    QVideoFrame,
    QVideoSink,
)
from PySide6.QtMultimediaWidgets import QVideoWidget

from creator_loop import __version__
from creator_loop.isolated_decode import MAX_DECODER_MEMORY
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.playback_protocol import PlaybackPacket, PlaybackPacketReader
from creator_loop.private_pipe import PrivatePipe
from creator_loop.publication_media import _open_read_lock
from creator_loop.runtime_ownership import bind_workspace_child, create_workspace_marker
from creator_loop.runtime_recovery import discard_known_playback_workspace

_active: set[IsolatedMediaPlayer] = set()


class IsolatedMediaPlayer(QObject):
    positionChanged = Signal(int)
    errorOccurred = Signal(object, str)
    finished = Signal()

    def __init__(
        self,
        owner: QObject,
        *,
        path: Path,
        root: Path | None,
        duration_ms: int,
        size: int | None,
        sha256: str | None,
    ) -> None:
        # Retain independently of a closing dialog until the native tree is gone.
        super().__init__()
        self.path, self.root = path, root
        self.duration_ms, self.size, self.digest = duration_ms, size, sha256
        self._source = QUrl.fromLocalFile(str(path))
        self._position = 0
        self._range = (0, duration_ms)
        self._started_range = self._range
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._status = QMediaPlayer.MediaStatus.LoadedMedia
        self._sink: QVideoSink | None = None
        self._audio: QAudioOutput | None = None
        self._buffers: QAudioBufferOutput | None = None
        self.process: OwnedWindowsProcess | None = None
        self.pipe: PrivatePipe | None = None
        self.held: BinaryIO | None = None
        self.workspace: Path | None = None
        self._marker: dict[str, object] = {}
        self._child: dict[str, object] | None = None
        self.preserved_workspace: Path | None = None
        self.reader = PlaybackPacketReader()
        self._seq = 0
        self._command_deadline: float | None = None
        self._closing = False
        self._restart = False
        self._ended = False
        self._video_seen = False
        self._audio_seen = False
        self._disposed = False
        self._last_output = time.monotonic()
        self._exit_deadline: float | None = None
        self._release_pending = False
        self._pipe_close_deadline: float | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._tick)
        owner.destroyed.connect(self._owner_destroyed)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

    def source(self) -> QUrl:
        return self._source

    def setSource(self, source: QUrl) -> None:
        if not source.isEmpty():
            raise ValueError("Playback may only use its verified original")
        self.stop()
        self._source = QUrl()
        self._status = QMediaPlayer.MediaStatus.NoMedia

    def position(self) -> int:
        return self._position

    def setPosition(self, position: int) -> None:
        if type(position) is not int or not 0 <= position < self.duration_ms:
            raise ValueError("Playback position is outside verified duration")
        self._position = position

    def set_range(self, start: int, end: int) -> None:
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= self.duration_ms
        ):
            raise ValueError("Playback range is outside verified duration")
        if self.process is not None and self._range != (start, end):
            self.stop()
        self._range = (start, end)

    def playbackState(self) -> QMediaPlayer.PlaybackState:
        return self._state

    def mediaStatus(self) -> QMediaPlayer.MediaStatus:
        return self._status

    def setVideoOutput(self, widget: QVideoWidget) -> None:
        self._sink = widget.videoSink()

    def videoSink(self) -> QVideoSink | None:
        return self._sink

    def setAudioOutput(self, output: QAudioOutput) -> None:
        if self.process is not None:
            raise RuntimeError("Configure playback audio before starting")
        self._audio = output

    def audioOutput(self) -> QAudioOutput | None:
        return self._audio

    def setAudioBufferOutput(self, output: QAudioBufferOutput) -> None:
        self._buffers = output

    def _command(self, action: str) -> None:
        if self.workspace is None or self._closing:
            return
        self._seq += 1
        folder = self.workspace
        temporary = folder / "control.pending"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "format": 1,
                    "seq": self._seq,
                    "action": action,
                    "position_ms": self._position,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        self._command_deadline = time.monotonic() + 8
        self._publish_command()

    def _publish_command(self) -> None:
        if self.workspace is None or self._closing or self._command_deadline is None:
            return
        try:
            os.replace(
                self.workspace / "control.pending", self.workspace / "control.json"
            )
        except PermissionError as exc:
            # A child may briefly hold the previous command open. Retry from
            # the GUI timer; never sleep or truncate a file the child is reading.
            if getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            if time.monotonic() >= self._command_deadline:
                raise TimeoutError("Playback control stayed locked for 8 seconds")
        else:
            self._command_deadline = None

    def play(self) -> None:
        if self._source.isEmpty() or self._disposed:
            return
        if self.process is not None:
            if self._closing or self._started_range != self._range or self._ended:
                self._restart = True
                self._terminate()
            else:
                try:
                    self._command("play")
                    self._state = QMediaPlayer.PlaybackState.PlayingState
                except (OSError, ValueError) as exc:
                    self._fail(str(exc))
            return
        try:
            self._launch()
        except Exception as exc:
            self._fail(str(exc))
            self._release_if_terminal()

    def _launch(self) -> None:
        if (
            sys.platform != "win32"
            or self.root is None
            or type(self.size) is not int
            or self.size <= 0
            or not isinstance(self.digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.digest)
        ):
            raise ValueError(
                "Playback requires a verified original fingerprint and Windows"
            )
        root = self.root.resolve(strict=True)
        for name in ("runtime", "logs"):
            directory = root / name
            if directory.is_symlink() or directory.is_junction():
                raise OSError("Playback requires real runtime/log directories")
            directory.mkdir(exist_ok=True)
        self.held = _open_read_lock(self.path)
        self.workspace = Path(
            tempfile.mkdtemp(prefix="playback-", dir=root / "runtime")
        )
        folder = self.workspace
        component = f"qt-playback/{__version__}/Qt-{qVersion()}"
        marker = create_workspace_marker(
            root, folder, component, kind="QT_PLAYBACK_WORKSPACE"
        )
        self._marker = marker
        self._child = None

        def bind(child: dict[str, object]) -> None:
            bind_workspace_child(folder, marker, child)
            self._child = child.copy()

        request = folder / "request.json"
        request.write_text(
            json.dumps(
                {
                    "format": 1,
                    "path": str(self.path),
                    "start_ms": self._range[0],
                    "end_ms": self._range[1],
                    "duration_ms": self.duration_ms,
                    "size": self.size,
                    "sha256": self.digest,
                    "audio": self._audio is not None,
                }
            ),
            encoding="utf-8",
        )
        self.pipe = PrivatePipe()
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
        self.process = OwnedWindowsProcess(
            Path(sys.executable),
            [*prefix, "--play-media", str(request)],
            root / "logs" / f"playback-{uuid4().hex}",
            component_version=component,
            environment=environment,
            capture_output=False,
            stdin_source=self.held,
            stdout_sink=self.pipe.writer,
            memory_limit_bytes=MAX_DECODER_MEMORY,
            before_resume=bind,
        )
        self.pipe.writer.close()
        self.reader = PlaybackPacketReader()
        self._seq = 0
        self._command_deadline = None
        self._closing, self._ended = False, False
        self._video_seen, self._audio_seen = False, False
        self._exit_deadline = None
        self._release_pending = False
        self._pipe_close_deadline = None
        self._started_range = self._range
        self._last_output = time.monotonic()
        self._state = QMediaPlayer.PlaybackState.PlayingState
        self._status = QMediaPlayer.MediaStatus.LoadingMedia
        _active.add(self)
        self.timer.start()

    def pause(self) -> None:
        if self._state == QMediaPlayer.PlaybackState.PausedState:
            return
        try:
            self._command("pause")
            if not self._closing:
                self._state = QMediaPlayer.PlaybackState.PausedState
        except (OSError, ValueError) as exc:
            self._fail(str(exc))

    def stop(self) -> None:
        self._restart = False
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._terminate()

    def _terminate(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.process is not None:
            self.process.request_stop(0)
            self._exit_deadline = time.monotonic() + 8
            self.timer.start()

    def _fail(self, message: str) -> None:
        self._status = QMediaPlayer.MediaStatus.InvalidMedia
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._restart = False
        self._terminate()
        self.errorOccurred.emit(QMediaPlayer.Error.ResourceError, message[:512])

    def _packet(self, packet: PlaybackPacket) -> None:
        body = packet.metadata
        if body["kind"] == "video":
            if not self._range[0] * 1000 <= body["time_us"] < self._range[1] * 1000:
                raise ValueError("Playback frame is outside selected range")
            self._video_seen = True
        elif body["kind"] == "audio":
            self._audio_seen = True
        if body["kind"] == "video" and self._sink is not None:
            image = QImage(
                packet.payload,
                body["width"],
                body["height"],
                body["width"] * 4,
                QImage.Format.Format_RGBA8888,
            ).copy()
            frame = QVideoFrame(image)
            frame.setStartTime(body["time_us"])
            self._sink.setVideoFrame(frame)
        elif body["kind"] == "audio" and self._buffers is not None:
            format_ = QAudioFormat()
            format_.setSampleRate(body["rate"])
            format_.setChannelCount(body["channels"])
            format_.setSampleFormat(QAudioFormat.SampleFormat[body["sample"]])
            buffer = QAudioBuffer(packet.payload, format_, body["time_us"])
            self._buffers.audioBufferReceived.emit(buffer)
        elif body["kind"] == "status":
            self._position = body["position_ms"]
            self.positionChanged.emit(self._position)
            if body["state"] == "ERROR":
                self._fail(body.get("message", "Playback child failed"))
            elif body["state"] == "ENDED":
                if (
                    self._sink is not None
                    and not self._video_seen
                    or self._sink is None
                    and not self._audio_seen
                ):
                    raise ValueError("Playback ended without decoded media")
                self._ended = True
                self._exit_deadline = time.monotonic() + 8
            elif body["state"] == "PLAYING":
                self._status = QMediaPlayer.MediaStatus.LoadedMedia
            elif body["state"] == "PAUSED":
                self._state = QMediaPlayer.PlaybackState.PausedState

    def _tick(self) -> None:
        try:
            data = b""
            if self._release_pending:
                self._release_if_terminal()
                if (
                    self._release_pending
                    and self._pipe_close_deadline is not None
                    and time.monotonic() >= self._pipe_close_deadline
                ):
                    self._pipe_close_deadline = None
                    self.errorOccurred.emit(
                        QMediaPlayer.Error.ResourceError,
                        "Private playback pipe cancellation has not completed; original remains locked",
                    )
                return
            if self.process is None:
                self._release_if_terminal()
                return
            self._publish_command()
            if not self._closing and self.pipe is not None:
                # At most 4 MiB per event-loop turn; no blocking pipe read.
                data = self.pipe.read_available(4 * 1024 * 1024)
                if data:
                    self._last_output = time.monotonic()
                    for packet in self.reader.feed(data):
                        if self._closing:
                            break
                        self._packet(packet)
            if self.process.tree_finished():
                # Drain remaining output before accepting normal completion.
                if not self._closing and (
                    data or self.pipe is not None and self.pipe.read_pending
                ):
                    return
                if not self._closing:
                    self.reader.finish()
                    if not self._ended:
                        self._fail("Playback child exited before completion")
                self._release_if_terminal()
                return
            now = time.monotonic()
            if not self._closing and (
                now - self._last_output >= 8
                or self._exit_deadline is not None
                and now >= self._exit_deadline
            ):
                self._fail("Playback exceeded its 8 second response/exit deadline")
            elif (
                self._closing
                and self._exit_deadline is not None
                and now >= self._exit_deadline
            ):
                self._exit_deadline = None
                self.errorOccurred.emit(
                    QMediaPlayer.Error.ResourceError,
                    "Owned playback tree has not exited; original remains locked",
                )
        except (OSError, ValueError, RuntimeError, RecursionError) as exc:
            self._fail(str(exc))

    def _release_if_terminal(self) -> None:
        if self.process is not None and not self.process.tree_finished():
            return
        if not self._release_pending:
            self._release_pending = True
            self._pipe_close_deadline = time.monotonic() + 8
        if self.pipe is not None:
            if not self.pipe.try_close():
                return
            self.pipe = None
        # Keep native terminal ownership observable until cancellation releases
        # the OVERLAPPED buffer/pipe. process=None must not precede the original
        # lease release or permit a range restart to replace retained resources.
        if self.process is not None:
            self.process.close()
            self.process = None
        if self.held is not None:
            self.held.close()
            self.held = None
        if self.workspace is not None:
            if not discard_known_playback_workspace(
                self.workspace, self._marker, self._child
            ):
                self.preserved_workspace = self.workspace
            self.workspace = None
        self.timer.stop()
        self._release_pending = False
        self._pipe_close_deadline = None
        _active.discard(self)
        if self._ended and not self._closing:
            self._status = QMediaPlayer.MediaStatus.EndOfMedia
            self._state = QMediaPlayer.PlaybackState.StoppedState
        self.finished.emit()
        if self._restart and not self._disposed and not self._source.isEmpty():
            self._restart = False
            self.play()
        elif self._disposed:
            self.deleteLater()

    def _owner_destroyed(self) -> None:
        self._disposed = True
        self._sink = None
        self._buffers = None
        self._audio = None
        self.stop()
        if self.process is None and self.pipe is None and self.held is None:
            self.deleteLater()

    def _shutdown(self) -> None:
        # Event dispatch is ending: synchronously prove native cleanup using the
        # existing bounded Job close, not a media backend stop in the GUI.
        self._restart = False
        if self.process is not None:
            self.process.close()
            self.process = None
        self._release_if_terminal()
        if self.pipe is not None:
            # Event dispatch has ended; cancellation must complete before its
            # OVERLAPPED/buffer or original is released. Normal UI uses timers.
            if self.pipe.event is not None:
                self.pipe.kernel.WaitForSingleObject(self.pipe.event, 5000)
            self._release_if_terminal()
            if self.pipe is not None:
                raise TimeoutError(
                    "Private pipe cancellation has not completed; resources retained"
                )
