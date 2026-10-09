"""Real streamed playback and bounded native tree cleanup, without GUI codecs."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.app_lock import AppDataLock
    from creator_loop.isolated_decode import decode_isolated
    from creator_loop.isolated_playback import IsolatedMediaPlayer
    from creator_loop.owned_process import OwnedWindowsProcess, _api
    from creator_loop.paths import ensure_data_root
    from creator_loop.playback_protocol import HEADER, MAX_VIDEO
    from creator_loop.runtime_files import RuntimeHandle
    from creator_loop.runtime_recovery import recover_runtime_startup
    from creator_loop.windows_paths import file_io_path
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtMultimedia import QAudioBufferOutput, QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QApplication, QWidget


@unittest.skipUnless(sys.platform == "win32", "requires Windows private playback Job")
class IsolatedPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temporary, temporary)
        self.root = (Path(temporary.name) / "Playback Hue space").resolve()
        ensure_data_root(self.root)
        self.path = self.root / "storage/originals/locked.mp4"
        self.original = (
            Path(__file__).parent / "fixtures/video-with-tone.mp4"
        ).read_bytes()
        self.path.write_bytes(self.original)
        decoded, _image = decode_isolated(
            self.path, self.root, mode="video", start_ms=0
        )
        self.duration = decoded["duration_ms"]
        self.owner = QWidget()
        self.addCleanup(self.owner.close)
        self.player = IsolatedMediaPlayer(
            self.owner,
            path=self.path,
            root=self.root,
            duration_ms=self.duration,
            size=len(self.original),
            sha256=hashlib.sha256(self.original).hexdigest(),
        )
        self.video = QVideoWidget(self.owner)
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(QAudioOutput(self.owner))
        self.errors = []
        self.player.errorOccurred.connect(
            lambda code, message: self.errors.append(message)
        )
        self.addCleanup(self.cleanup_player)
        self.kernel = _api()

    def test_deep_runtime_command_child_binding_and_cleanup_keep_original(self):
        prefix = "Deep playback runtime "
        base = self.root.parent
        self.root = base / (prefix + "x" * (225 - len(str(base)) - 1 - len(prefix)))
        ensure_data_root(self.root)
        self.path = self.root / "storage/originals/locked.mp4"
        self.path.write_bytes(self.original)
        self.player = IsolatedMediaPlayer(
            self.owner,
            path=self.path,
            root=self.root,
            duration_ms=self.duration,
            size=len(self.original),
            sha256=hashlib.sha256(self.original).hexdigest(),
        )
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(QAudioOutput(self.owner))
        self.player.errorOccurred.connect(
            lambda code, message: self.errors.append(message)
        )
        buffers = []
        output = QAudioBufferOutput(self.player)
        self.player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(
            lambda buffer: (
                buffers.append(buffer.startTime()) if buffer.isValid() else None
            )
        )
        self.player.set_range(203, 607)
        self.player.play()
        self.until(lambda: self.player.process is not None or bool(self.errors))
        self.assertFalse(self.errors)
        workspace = self.player.workspace
        self.assertIsNotNone(workspace)
        self.assertGreater(len(str(workspace / "child-ownership.json")), 260)
        self.until(
            lambda: (
                bool(self.errors)
                or (
                    bool(buffers)
                    and self.player.playbackState()
                    == QMediaPlayer.PlaybackState.PausedState
                )
            )
        )
        self.assertFalse(self.errors)
        self.assertTrue(buffers)
        self.cleanup_player()
        self.assertEqual(list((self.root / "runtime").iterdir()), [])
        self.assertEqual(self.path.read_bytes(), self.original)

    def _cleanup_temporary(self, temporary):
        owned = Path(temporary.name).resolve(strict=True)
        self.assertFalse(owned.is_symlink() or owned.is_junction())
        self.assertTrue(self.root.is_relative_to(owned))
        shutil.rmtree(file_io_path(owned))
        temporary.cleanup()

    def until(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), self.errors)

    def cleanup_player(self):
        expected = self.path.read_bytes()
        process = self.player.process
        self.player.stop()
        self.until(
            lambda: (
                self.player.process is None
                and self.player.pipe is None
                and self.player.held is None
                and self.player.workspace is None
            )
        )
        if process is not None:
            self.assertIsNone(process.job)
            self.assertIsNone(process.process)
            self.assertEqual(process.streams, [])
        # Native child and parent duplicate/input leases must all be released.
        with self.path.open("r+b") as stream:
            self.assertEqual(stream.read(), expected)

    def monitor(self):
        process = self.player.process
        self.assertIsNotNone(process)
        handle = self.kernel.OpenProcess(0x00101000, False, process.record["pid"])
        self.assertTrue(handle)
        self.addCleanup(self.kernel.CloseHandle, handle)
        return handle

    def fake(self, script):
        def launch(executable, arguments, logs, **kwargs):
            return OwnedWindowsProcess(executable, ["-u", "-c", script], logs, **kwargs)

        return patch("creator_loop.isolated_playback.OwnedWindowsProcess", launch)

    def test_actual_frames_pcm_normal_exit_original_lock_and_no_media_logs(self):
        colors, samples = [], []
        self.player.videoSink().videoFrameChanged.connect(
            lambda frame: (
                colors.append(frame.toImage().pixelColor(0, 0))
                if frame.isValid()
                else None
            )
        )
        buffers = QAudioBufferOutput(self.player)
        self.player.setAudioBufferOutput(buffers)
        buffers.audioBufferReceived.connect(
            lambda buffer: (
                samples.append(buffer.frameCount()) if buffer.isValid() else None
            )
        )
        self.player.play()
        handle = self.monitor()
        with self.assertRaises(OSError):
            self.path.open("wb").close()
        self.until(lambda: self.player.process is None)
        self.assertFalse(self.errors)
        self.assertTrue(any(c.red() > c.blue() for c in colors))
        self.assertTrue(any(c.blue() > c.red() for c in colors))
        self.assertGreater(sum(samples), 0)
        self.assertGreater(self.player.position(), 700)
        self.assertEqual(self.player.mediaStatus(), QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
        self.assertEqual(self.path.read_bytes(), self.original)

        self.assertEqual(list((self.root / "runtime").iterdir()), [])
        self.assertTrue(list((self.root / "logs").glob("*/ownership.json")))
        self.assertFalse(list((self.root / "logs").glob("*/stdout.log")))
        self.assertFalse(list((self.root / "logs").glob("*/stderr.log")))
        with self.path.open("r+b") as stream:
            self.assertEqual(stream.read(), self.original)

    def test_cancel_keeps_unowned_sentinel_alive_and_proves_native_tree_exit(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"]
        )
        try:
            with self.fake("import time;time.sleep(30)"):
                self.player.play()
            handle = self.monitor()
            started = time.monotonic()
            self.player.stop()
            self.assertLess(time.monotonic() - started, 0.2)
            self.until(lambda: self.player.process is None)
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
            self.assertIsNone(sentinel.poll())
            self.assertEqual(self.path.read_bytes(), self.original)
        finally:
            sentinel.terminate()
            sentinel.wait(5)

    def test_selected_range_streams_only_actual_pixels_and_pcm_inside_bounds(self):
        self.player.set_range(203, 607)
        self.player.setPosition(203)
        frames, buffers = [], []
        self.player.videoSink().videoFrameChanged.connect(
            lambda frame: frames.append(frame.startTime()) if frame.isValid() else None
        )
        output = QAudioBufferOutput(self.player)
        self.player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(
            lambda buffer: (
                buffers.append(
                    (buffer.startTime(), buffer.startTime() + buffer.duration())
                )
                if buffer.isValid()
                else None
            )
        )
        self.player.play()
        # Boundary pause may precede Qt's queued PCM callback; require both
        # outputs within the existing deadline, without delaying the pause.
        self.until(
            lambda: (
                bool(frames)
                and bool(buffers)
                and (
                    self.player.playbackState()
                    == QMediaPlayer.PlaybackState.PausedState
                )
            )
        )
        self.assertFalse(self.errors)
        self.assertTrue(frames)
        self.assertTrue(buffers)
        self.assertTrue(all(203000 <= pts < 607000 for pts in frames), frames)
        self.assertTrue(
            all(203000 <= begin < end <= 607000 for begin, end in buffers), buffers
        )
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_short_range_keeps_queued_pcm_after_boundary_pause(self):
        self.player.set_range(0, 90)
        buffers = []
        output = QAudioBufferOutput(self.player)
        self.player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(
            lambda b: (
                buffers.append((b.startTime(), b.startTime() + b.duration()))
                if b.isValid()
                else None
            )
        )
        self.player.play()
        self.until(
            lambda: (
                bool(buffers)
                and self.player.playbackState()
                == QMediaPlayer.PlaybackState.PausedState
            )
        )
        self.assertFalse(self.errors)
        self.assertTrue(
            all(0 <= start < end <= 90000 for start, end in buffers), buffers
        )
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_range_changes_retire_previous_tree_before_starting_new_range(self):
        self.player.set_range(203, 607)
        frames = []
        self.player.videoSink().videoFrameChanged.connect(
            lambda frame: frames.append(frame.startTime()) if frame.isValid() else None
        )
        self.player.play()
        self.until(lambda: bool(frames))
        frames.clear()
        self.player.set_range(701, 901)
        self.player.setPosition(701)
        self.player.play()
        self.until(
            lambda: (
                self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            )
        )
        self.assertFalse(self.errors)
        self.assertTrue(frames)
        self.assertTrue(all(701000 <= pts < 901000 for pts in frames), frames)

    def test_native_tree_terminal_keeps_original_and_process_until_pipe_cancel_completes(
        self,
    ):
        with self.fake("import time;time.sleep(30)"):
            self.player.play()
        process, pipe, held = self.player.process, self.player.pipe, self.player.held
        self.assertIsNotNone(process)
        self.assertIsNotNone(pipe)
        self.assertIsNotNone(held)
        finished = []
        self.player.finished.connect(lambda: finished.append(True))
        handle = self.monitor()
        self.player.stop()
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 2000), 0)
        self.assertTrue(process.tree_finished())
        with patch.object(pipe, "try_close", return_value=False):
            self.player._release_if_terminal()
            self.assertIs(self.player.process, process)
            self.assertIs(self.player.held, held)
            self.player._tick()
            self.assertIs(self.player.process, process)
            self.assertEqual(finished, [])
            with self.assertRaises(OSError):
                self.path.write_bytes(b"early mutation")
            with patch(
                "creator_loop.isolated_playback.time.monotonic",
                return_value=self.player._pipe_close_deadline + 1,
            ):
                self.player._tick()
                self.player._tick()
            self.assertEqual(len(self.errors), 1)
            self.assertIn("pipe cancellation", self.errors[0])
            self.assertIs(self.player.held, held)
            self.assertIs(self.player.process, process)
            self.assertEqual(finished, [])
        self.until(lambda: self.player.process is None)
        self.assertIsNone(self.player.held)
        self.assertIsNone(self.player.pipe)
        self.assertIsNone(self.player.workspace)
        self.assertEqual(finished, [True])
        self.assertEqual(len(self.errors), 1)
        self.path.write_bytes(self.original)

    def test_no_heartbeat_real_8s_timeout_keeps_gui_dispatching(self):
        pulses = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: pulses.append(time.monotonic()))
        timer.start()
        self.addCleanup(timer.stop)
        with self.fake("import time;time.sleep(30)"):
            self.player.play()
        handle = self.monitor()
        started = time.monotonic()
        self.until(lambda: self.player.process is None, timeout=11)
        self.assertGreaterEqual(time.monotonic() - started, 7.5)
        self.assertGreater(len(pulses), 250)
        self.assertTrue(any("8 second" in message for message in self.errors))
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
        self.assertNotEqual(
            self.player.mediaStatus(), QMediaPlayer.MediaStatus.EndOfMedia
        )

    def test_child_crash_is_error_and_releases_original_only_after_exit(self):
        with self.fake("import os;os._exit(17)"):
            self.player.play()
        handle = self.monitor()
        self.until(lambda: self.player.process is None)
        self.assertTrue(any("before completion" in message for message in self.errors))
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_changed_source_digest_refuses_real_decoder_before_any_frames(self):
        self.path.write_bytes(self.original + b"changed")
        frames = []
        self.player.videoSink().videoFrameChanged.connect(
            lambda frame: frames.append(frame)
        )
        self.player.play()
        self.until(lambda: self.player.process is None)
        self.assertTrue(any("bytes changed" in message for message in self.errors))
        self.assertFalse(frames)
        self.assertEqual(self.path.read_bytes(), self.original + b"changed")

    def test_oversize_pipe_header_kills_only_owned_child_before_payload(self):
        header = HEADER.pack(b"CLP1", 1, MAX_VIDEO + 1)
        with self.fake(f"import os,time;os.write(1,{header!r});time.sleep(30)"):
            self.player.play()
        handle = self.monitor()
        self.until(lambda: self.player.process is None)
        self.assertTrue(any("record header" in message for message in self.errors))
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)

    def test_command_sharing_conflict_retries_without_blocking_gui(self):
        with self.fake("import time;time.sleep(30)"):
            self.player.play()
        self.player.pause()
        folder = self.player.workspace
        self.assertIsNotNone(folder)
        import json

        old = json.loads((folder / "control.json").read_text())
        with RuntimeHandle(folder / "control.json") as held:
            started = time.monotonic()
            self.player.play()
            self.assertLess(time.monotonic() - started, 0.2)
            self.app.processEvents()
            self.assertTrue((folder / "control.pending").exists())
            self.assertEqual(held.read_json(), old)
            self.assertFalse(self.errors)
        self.until(lambda: not (folder / "control.pending").exists())
        self.assertGreater(
            json.loads((folder / "control.json").read_text())["seq"], old["seq"]
        )

    def test_active_cleanup_preserves_unknown_files_and_binding(self):
        with self.fake("import time;time.sleep(30)"):
            self.player.play()
        folder = self.player.workspace
        foreign = folder / "foreign-file"
        foreign.write_bytes(b"keep unknown fixture")
        self.player.stop()
        self.until(lambda: self.player.process is None)
        self.assertEqual(self.player.preserved_workspace, folder)
        self.assertEqual(foreign.read_bytes(), b"keep unknown fixture")
        self.assertTrue((folder / "ownership.json").exists())
        self.assertTrue((folder / "child-ownership.json").exists())
        with AppDataLock(self.root) as lock:
            result = recover_runtime_startup(self.root, lock)
        self.assertIn(folder.name, [name for name, _reason in result.preserved])
        self.assertEqual(foreign.read_bytes(), b"keep unknown fixture")

    def test_destroyed_dialog_retains_facade_until_native_child_exit(self):
        from creator_loop.isolated_playback import _active

        owner = QWidget()
        player = IsolatedMediaPlayer(
            owner,
            path=self.path,
            root=self.root,
            duration_ms=self.duration,
            size=len(self.original),
            sha256=hashlib.sha256(self.original).hexdigest(),
        )
        video = QVideoWidget(owner)
        player.setVideoOutput(video)
        with self.fake("import time;time.sleep(30)"):
            player.play()
        handle = self.kernel.OpenProcess(
            0x00101000, False, player.process.record["pid"]
        )
        self.assertTrue(handle)
        try:
            owner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(player._disposed)
            self.until(lambda: player.process is None)
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
            self.assertNotIn(player, _active)
            self.assertEqual(self.path.read_bytes(), self.original)
        finally:
            kernel = self.kernel
            kernel.CloseHandle(handle)

    def test_actual_owner_crash_kills_playback_and_startup_recovers_bound_workspace(
        self,
    ):
        script = """import hashlib,os,sys,time
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtMultimediaWidgets import QVideoWidget
from creator_loop.isolated_playback import IsolatedMediaPlayer
root=Path(sys.argv[1]); path=root/'storage/originals/locked.mp4'
app=QApplication([]); owner=QWidget(); video=QVideoWidget(owner)
original=path.read_bytes()
player=IsolatedMediaPlayer(owner,path=path,root=root,duration_ms=int(sys.argv[2]),size=len(original),sha256=hashlib.sha256(original).hexdigest())
player.setVideoOutput(video)
def ready(frame):
    if frame.isValid():
        player.pause()
        (root/'ready').write_text('frame')
player.videoSink().videoFrameChanged.connect(ready)
player.play()
deadline=time.monotonic()+15
while time.monotonic()<deadline:
    app.processEvents()
    if (root/'crash').exists():os._exit(17)
    time.sleep(.005)
raise SystemExit(3)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        with OwnedWindowsProcess(
            Path(sys.executable),
            ["-u", "-c", script, str(self.root), str(self.duration)],
            self.root / "logs/owner-crash",
            component_version="fixture",
            environment=environment,
            capture_output=True,
        ) as owner:
            self.until(lambda: (self.root / "ready").exists(), timeout=10)
            import json

            workspace = next((self.root / "runtime").glob("playback-*"))
            record = json.loads((workspace / "child-ownership.json").read_text())
            handle = self.kernel.OpenProcess(0x00101000, False, record["pid"])
            self.assertTrue(handle)
            try:
                with self.assertRaises(OSError):
                    self.path.open("wb").close()
                (self.root / "crash").write_text("crash fixture")
                outcome = owner.wait(10)
                self.assertEqual(outcome.exit_code, 17)
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                self.kernel.CloseHandle(handle)
        with AppDataLock(self.root) as lock:
            recovered = recover_runtime_startup(self.root, lock)
        self.assertEqual(recovered.cleaned, (workspace.name,))
        self.assertEqual(recovered.preserved, ())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue((self.root / "logs/owner-crash/ownership.json").exists())
