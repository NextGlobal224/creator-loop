"""Exercise real Qt time correction dialogs, input checks and player cleanup."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtWidgets import QApplication, QDialogButtonBox
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.audio_evidence import DecodedAudioSegment
    from creator_loop.audio_evidence_ui import AudioEvidenceDialog
    from creator_loop.video_evidence import DecodedVideoFrame
    from creator_loop.video_evidence_ui import VideoEvidenceDialog


@unittest.skipUnless(QApplication is not None, "requires Windows and Qt Multimedia")
class TimeEvidenceDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.anchor = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        self.video = DecodedVideoFrame(967, 633, image, self.anchor)
        self.audio = DecodedAudioSegment(967, 185, 209, self.anchor)

    def test_video_correction_loads_exact_range_and_requires_actor_reason(self) -> None:
        dialog = VideoEvidenceDialog(
            self.video,
            correction=True,
            start_ms=601,
            end_ms=899,
            content="Quan sát cũ",
        )
        self.addCleanup(dialog.close)
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertTrue(dialog.correction)
        self.assertEqual(dialog.windowTitle(), "Sửa Evidence Video")
        self.assertEqual((dialog.start.value(), dialog.end.value()), (601, 899))
        self.assertEqual((dialog.segment.start_ms, dialog.segment.end_ms), (601, 899))
        self.assertEqual(dialog.content.text(), "Quan sát cũ")
        self.assertFalse(ok.isEnabled())
        dialog.reason.setText("Sửa lời mô tả")
        self.assertTrue(ok.isEnabled())
        dialog.actor.setText(" ")
        self.assertFalse(ok.isEnabled())
        dialog.actor.setText("reviewer")
        dialog.content.setText(" ")
        self.assertFalse(ok.isEnabled())
        dialog.content.setText("Quan sát mới")
        self.assertTrue(ok.isEnabled())
        self.assertEqual((dialog.start.value(), dialog.end.value()), (601, 899))
        dialog.reason.setText(" ")
        self.assertFalse(ok.isEnabled())
        self.app.processEvents()

    def test_audio_correction_locks_saved_type_and_keeps_exact_range(self) -> None:
        for kind in ("SPEECH", "OTHER"):
            with self.subTest(kind=kind):
                dialog = AudioEvidenceDialog(
                    self.audio,
                    correction=True,
                    start_ms=203,
                    end_ms=607,
                    content="Nội dung cũ",
                    evidence_type=kind,
                )
                self.addCleanup(dialog.close)
                ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
                self.assertEqual(dialog.windowTitle(), "Sửa Evidence Audio")
                self.assertEqual(dialog.kind.currentData(), kind)
                self.assertFalse(dialog.kind.isEnabled())
                self.assertEqual((dialog.start.value(), dialog.end.value()), (203, 607))
                self.assertEqual(
                    (dialog.segment.start_ms, dialog.segment.end_ms), (203, 607)
                )
                self.assertEqual(dialog.content.text(), "Nội dung cũ")
                self.assertFalse(ok.isEnabled())
                dialog.reason.setText("Đối chiếu lại đoạn nghe")
                self.assertTrue(ok.isEnabled())
                dialog.actor.clear()
                self.assertFalse(ok.isEnabled())
                dialog.actor.setText("reviewer")
                self.assertTrue(ok.isEnabled())
                self.assertEqual(dialog.kind.currentData(), kind)
        self.app.processEvents()

    def test_create_defaults_and_changed_ranges_keep_previous_behavior(self) -> None:
        for constructor, decoded in (
            (VideoEvidenceDialog, self.video),
            (AudioEvidenceDialog, self.audio),
        ):
            with self.subTest(constructor=constructor):
                dialog = constructor(decoded)
                self.addCleanup(dialog.close)
                ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
                self.assertFalse(dialog.correction)
                self.assertEqual((dialog.start.value(), dialog.end.value()), (0, 967))
                self.assertFalse(ok.isEnabled())
                dialog.content.setText("Quan sát mới")
                self.assertTrue(ok.isEnabled())
                dialog.start.setValue(500)
                dialog.end.setValue(500)
                self.assertFalse(ok.isEnabled())
                dialog.end.setValue(499)
                self.assertFalse(ok.isEnabled())
                dialog.end.setValue(700)
                self.assertTrue(ok.isEnabled())
                self.assertEqual(
                    (dialog.segment.start_ms, dialog.segment.end_ms), (500, 700)
                )
                self.assertEqual(dialog.start.maximum(), 966)
                self.assertEqual(dialog.end.maximum(), 967)
                self.assertEqual(dialog.reason.text(), "")
                if isinstance(dialog, AudioEvidenceDialog):
                    self.assertTrue(dialog.kind.isEnabled())
                    self.assertEqual(dialog.kind.currentData(), "SPEECH")
                    dialog.kind.setCurrentIndex(dialog.kind.findData("OTHER"))
                    self.assertEqual(dialog.kind.currentData(), "OTHER")
        self.app.processEvents()

    def test_invalid_saved_values_fail_instead_of_silently_clamping(self) -> None:
        for constructor, decoded in (
            (VideoEvidenceDialog, self.video),
            (AudioEvidenceDialog, self.audio),
        ):
            for start, end in (
                (-1, 400),
                (100, 968),
                (400, 400),
                (True, 400),
                (100, 400.5),
            ):
                with self.subTest(constructor=constructor, start=start, end=end):
                    with self.assertRaisesRegex(ValueError, "valid saved time range"):
                        constructor(
                            decoded, correction=True, start_ms=start, end_ms=end
                        )
        with self.assertRaisesRegex(ValueError, "SPEECH or OTHER"):
            AudioEvidenceDialog(self.audio, correction=True, evidence_type="METADATA")

    def test_accept_and_reject_clear_player_source_and_stop(self) -> None:
        from creator_loop.isolated_decode import decode_isolated

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "locked.mp4"
        original = self.anchor.read_bytes()
        path.write_bytes(original)
        body, _image = decode_isolated(path, root, mode="video", start_ms=0)
        video = replace(
            self.video,
            duration_ms=body["duration_ms"],
            anchor_path=path,
            data_root=root,
            expected_size=len(original),
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
        audio = replace(
            self.audio,
            duration_ms=body["duration_ms"],
            anchor_path=path,
            data_root=root,
            expected_size=len(original),
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
        for constructor, decoded in (
            (VideoEvidenceDialog, video),
            (AudioEvidenceDialog, audio),
        ):
            for result in (
                constructor.DialogCode.Accepted,
                constructor.DialogCode.Rejected,
            ):
                with self.subTest(constructor=constructor, result=result):
                    dialog = constructor(
                        decoded,
                        correction=True,
                        start_ms=200,
                        end_ms=600,
                        content="Đoạn đã kiểm",
                    )
                    self.addCleanup(dialog.close)
                    dialog.reason.setText("Đối chiếu")
                    self.assertFalse(dialog.segment.player.source().isEmpty())
                    dialog.segment.play()
                    self.assertIsNotNone(dialog.segment.player.process)
                    self.app.processEvents()
                    dialog.done(result)
                    self.assertTrue(dialog.segment.player.source().isEmpty())
                    self.assertEqual(
                        dialog.segment.player.playbackState(),
                        QMediaPlayer.PlaybackState.StoppedState,
                    )
                    deadline = time.monotonic() + 5
                    while (
                        dialog.segment.player.process is not None
                        and time.monotonic() < deadline
                    ):
                        self.app.processEvents()
                        time.sleep(0.005)
                    self.assertIsNone(dialog.segment.player.process)
                    self.assertEqual(path.read_bytes(), original)
        self.app.processEvents()
