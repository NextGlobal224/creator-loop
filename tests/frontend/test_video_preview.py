"""Real Qt preview presentation with a fake playback adapter in fast tests."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from creator_loop.shared.workflow import RenderResult

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.frontend.video_preview import VideoPreviewWidget
        from PySide6.QtCore import QObject, QUrl, Signal
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None


if QApplication is not None:

    class FakePlayer(QObject):
        positionChanged = Signal(int)
        errorOccurred = Signal(object, str)
        finished = Signal()

        def __init__(self, owner, **kwargs):
            super().__init__()
            self.arguments = kwargs
            self.started = 0
            self.stopped = 0
            self.range = None
            self._state = QMediaPlayer.PlaybackState.StoppedState
            self._status = QMediaPlayer.MediaStatus.LoadedMedia

        def setVideoOutput(self, video):
            self.video = video

        def setAudioOutput(self, output):
            self.audio = output

        def set_range(self, start, end):
            self.range = (start, end)

        def play(self):
            self.started += 1
            self._state = QMediaPlayer.PlaybackState.PlayingState

        def stop(self):
            self.stopped += 1
            self._state = QMediaPlayer.PlaybackState.StoppedState

        def setSource(self, source):
            assert isinstance(source, QUrl) and source.isEmpty()
            self.stop()

        def mediaStatus(self):
            return self._status

        def playbackState(self):
            return self._state


@unittest.skipUnless(QApplication is not None, "requires Windows Qt Multimedia")
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "nháp có dấu.mp4"
        self.path.write_bytes(b"FAKE MP4 UI ADAPTER TEST ONLY")
        self.result = RenderResult(
            "run",
            "exact-draft-v1",
            "SUCCEEDED",
            self.path,
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
            self.path.stat().st_size,
            3000,
            360,
            640,
            "ffmpeg",
            "test",
        )
        patcher = patch(
            "creator_loop.frontend.video_preview.IsolatedMediaPlayer", FakePlayer
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.widget = VideoPreviewWidget(self.root)
        self.addCleanup(self.widget.close)

    def test_set_result_does_not_start_playback_or_render_until_clicked(self):
        self.assertFalse(self.widget.play_button.isEnabled())
        self.widget.set_result(self.result)
        self.assertEqual(self.widget.player.started, 0)
        self.assertIn("exact-draft-v1", self.widget.description.text())
        self.widget.play_button.click()
        self.assertEqual(self.widget.player.started, 1)
        self.assertEqual(self.widget.player.range, (0, 3000))
        self.widget.stop_button.click()
        self.assertEqual(self.widget.player.stopped, 1)

    def test_rejects_running_cancelled_missing_and_tampered_output(self):
        for status in ("RUNNING", "CANCELLED", "FAILED"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.widget.set_result(replace(self.result, status=status))
        with self.assertRaises(ValueError):
            self.widget.set_result(replace(self.result, byte_size=999))
        self.assertIsNone(self.widget.player)
        self.assertFalse(self.widget.play_button.isEnabled())

    def test_replacing_result_disposes_old_player_and_keeps_old_file(self):
        self.widget.set_result(self.result)
        old = self.widget.player
        self.widget.play()
        self.widget.set_result(
            replace(self.result, run_id="run2", draft_version_id="exact-draft-v2")
        )
        self.assertEqual(old.stopped, 1)
        self.assertEqual(self.widget.player.started, 0)
        self.assertTrue(self.path.is_file())
        self.widget.dispose()
        self.assertIsNone(self.widget.player)

    def test_seek_updates_owned_playback_range_and_errors_are_visible(self):
        self.widget.set_result(self.result)
        self.widget.seek.setValue(1000)
        self.widget.play()
        self.assertEqual(self.widget.player.range, (1000, 3000))
        self.widget.player.errorOccurred.emit(
            QMediaPlayer.Error.ResourceError, "Synthetic failure"
        )
        self.assertIn("Synthetic failure", self.widget.status.text())


if __name__ == "__main__":
    unittest.main()
