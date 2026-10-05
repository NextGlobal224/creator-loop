"""Exercise whole-source confirmation, playback and Library version history."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtMultimedia import QAudioBufferOutput, QMediaPlayer
        from PySide6.QtWidgets import QApplication, QDialogButtonBox
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.claims import EvidenceLink, create_claim
    from creator_loop.database import _connect_write, initialize, open_readonly
    from creator_loop.evidence_reopen import reopen_evidence_version
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.media_intake import intake_image_original, intake_video_original
    from creator_loop.text_evidence_ui import TextEvidenceDialog
    from creator_loop.text_intake import intake_text_original
    from creator_loop.whole_evidence import read_verified_whole_source
    from creator_loop.whole_evidence_ui import WholeEvidenceDialog, WholeEvidenceView


@unittest.skipUnless(QApplication is not None, "requires Windows and pinned Qt")
class WholeEvidenceUiTests(unittest.TestCase):
    def test_missing_verified_text_snapshot_refuses_instead_of_empty_content(self):
        source = replace(self._source("TEXT"), text_snapshot=None)
        with self.assertRaisesRegex(ValueError, "verified snapshot"):
            WholeEvidenceDialog(source)

    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Toàn nguồn Đà Nẵng"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        text = Path(self.temp.name) / "Toàn văn.txt"
        text.write_text("  Cafe\u0301 ở Huế 🏙\nDòng hai.\n", encoding="utf-8")
        image_path = Path(self.temp.name) / "Toàn ảnh.png"
        image = QImage(8, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        image.setPixelColor(7, 3, QColor("blue"))
        self.assertTrue(image.save(str(image_path)))
        self.imported = {
            "TEXT": intake_text_original(text, root=self.root),
            "IMAGE": intake_image_original(image_path, root=self.root),
            "VIDEO": intake_video_original(
                Path(__file__).parent / "fixtures" / "video-with-tone.mp4",
                root=self.root,
            ),
        }
        self.window = LibraryWindow(self.root)
        self.addCleanup(self.window.close)
        warning_patch = patch("creator_loop.library_ui.QMessageBox.warning")
        self.warning = warning_patch.start()
        self.addCleanup(warning_patch.stop)

    def _wait(self) -> None:
        deadline = time.monotonic() + 12
        while self.window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.window._worker)

    def _source(self, kind):
        with closing(open_readonly(self.db_path)) as db:
            return read_verified_whole_source(
                db, self.imported[kind].file_id, self.root
            )

    def _select_version(self, version_id) -> None:
        from PySide6.QtCore import Qt

        for row in range(self.window.evidence_table.rowCount()):
            if (
                self.window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == version_id
            ):
                self.window.evidence_table.selectRow(row)
                return
        self.fail("Saved Evidence Version missing from Library")

    def _create(self, kind, *, alter=None, expect_warning=False):
        for row in range(self.window.table.rowCount()):
            if self.window.table.item(row, 0).text() == kind:
                self.window.table.selectRow(row)
        source = self.imported[kind]

        def confirm(dialog):
            self.assertFalse(dialog.confirmed.isChecked())
            if kind == "TEXT":
                self.assertTrue(dialog.content.isReadOnly())
                self.assertEqual(
                    dialog.content.toPlainText(), "  Café ở Huế 🏙\nDòng hai.\n"
                )
            else:
                dialog.content.setPlainText("Nguồn có vùng đỏ và xanh")
            dialog.actor.setText("creator-ui")
            dialog.confirmed.setChecked(True)
            if alter is not None:
                alter(dialog)
            dialog.accept()
            return WholeEvidenceDialog.DialogCode.Accepted

        button = next(
            b for b in self.window._buttons if b.text() == "Tạo Evidence toàn nguồn"
        )
        with patch.object(WholeEvidenceDialog, "exec", confirm):
            button.click()
            self._wait()
        if not expect_warning:
            self.warning.assert_not_called()
        with closing(open_readonly(self.db_path)) as db:
            return db.execute(
                "SELECT evidence_version_id FROM evidence_versions WHERE anchor_file_id=? ORDER BY version_no DESC",
                (source.file_id,),
            ).fetchone()

    def test_confirmation_is_explicit_and_text_cannot_be_changed(self) -> None:
        dialog = WholeEvidenceDialog(self._source("TEXT"))
        self.addCleanup(dialog.close)
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(ok.isEnabled())
        dialog.show()
        dialog.accept()
        self.assertTrue(dialog.isVisible())
        dialog.confirmed.setChecked(True)
        self.assertTrue(ok.isEnabled())
        dialog.actor.clear()
        self.assertFalse(ok.isEnabled())
        self.assertTrue(dialog.content.isReadOnly())
        self.assertEqual(
            dialog.preview.text.toPlainText(), dialog.content.toPlainText()
        )

    def _history_flow(self, kind) -> None:
        old_id = self._create(kind)[0]
        with closing(_connect_write(self.db_path)) as db:
            claim = create_claim(
                db,
                claim_type="FACTUAL",
                statement="Nhận định cần review",
                actor="editor",
                links=[EvidenceLink(old_id, "SUPPORTS")],
            )
        self._select_version(old_id)

        def correct_visual(dialog):
            self.assertFalse(dialog.confirmed.isChecked())
            self.assertEqual(dialog.content.toPlainText(), "Nguồn có vùng đỏ và xanh")
            dialog.content.setPlainText("Quan sát đã làm rõ")
            dialog.reason.setText("Mô tả chính xác hơn")
            dialog.actor.setText("editor-ui")
            dialog.confirmed.setChecked(True)
            dialog.accept()
            return WholeEvidenceDialog.DialogCode.Accepted

        def narrow_text(dialog):
            self.assertEqual(dialog.start.value(), 0)
            self.assertEqual(dialog.end.value(), len(dialog.snapshot))
            dialog.start.setValue(2)
            dialog.end.setValue(6)
            dialog.actor.setText("editor-ui")
            dialog.reason.setText("Chỉ đoạn Café liên quan")
            dialog.accept()
            return TextEvidenceDialog.DialogCode.Accepted

        with patch.object(
            TextEvidenceDialog if kind == "TEXT" else WholeEvidenceDialog,
            "exec",
            narrow_text if kind == "TEXT" else correct_visual,
        ):
            self.window.choose_whole_evidence("load-correction")
            self._wait()
        self.assertIn("1 Claim Version", self.window.status.text())
        with closing(open_readonly(self.db_path)) as db:
            rows = db.execute(
                "SELECT evidence_version_id,anchor_file_id,locator_type,content FROM evidence_versions ORDER BY version_no"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], old_id)
            self.assertEqual(rows[0][2], "WHOLE_ASSET")
            if kind == "TEXT":
                # QTextEdit displays CRLF as LF; persistence keeps the verified snapshot.
                self.assertEqual(rows[0][3], "  Café ở Huế 🏙\r\nDòng hai.\r\n")
            self.assertEqual(rows[1][1], self.imported[kind].file_id)
            self.assertEqual(
                rows[1][2], "TEXT_RANGE" if kind == "TEXT" else "WHOLE_ASSET"
            )
            self.assertEqual(
                rows[1][3], "Café" if kind == "TEXT" else "Quan sát đã làm rõ"
            )
            self.assertEqual(
                db.execute(
                    "SELECT evidence_version_id FROM claim_evidence WHERE claim_version_id=?",
                    (claim.claim_version_id,),
                ).fetchone()[0],
                old_id,
            )
            self.assertEqual(
                db.execute(
                    "SELECT evidence_version_id,action FROM review_events"
                ).fetchone(),
                (old_id, "CORRECT"),
            )
        self.window.close()
        self.window = LibraryWindow(self.root)
        self.addCleanup(self.window.close)
        reopened = []

        def inspect(view):
            reopened.append(view.preview)
            if kind == "TEXT":
                self.assertIn("Dòng hai.", view.preview.text.toPlainText())
            elif kind == "IMAGE":
                pixmap = view.preview.image.pixmap()
                self.assertEqual((pixmap.width(), pixmap.height()), (8, 4))
                self.assertEqual(pixmap.toImage().pixelColor(7, 3), QColor("blue"))
            else:
                self.assertIsNotNone(view.preview.audio)
                self.assertEqual(view.preview.segment.start_ms, 0)
                self.assertGreaterEqual(view.preview.segment.end_ms, 800)
            view.reject()
            return WholeEvidenceView.DialogCode.Rejected

        for version_id in [old_id] if kind == "TEXT" else [old_id, rows[1][0]]:
            self._select_version(version_id)
            with patch.object(WholeEvidenceView, "exec", inspect):
                self.window.choose_whole_evidence("reopen")
                self._wait()
        self.assertEqual(len(reopened), 1 if kind == "TEXT" else 2)
        if kind == "TEXT":
            with closing(open_readonly(self.db_path)) as db:
                self.assertEqual(
                    reopen_evidence_version(db, rows[1][0], self.root).text_excerpt,
                    "Café",
                )

    def test_text_create_narrow_and_reopen_old_whole_snapshot(self) -> None:
        self._history_flow("TEXT")

    def test_image_create_correct_and_reopen_exact_full_image_history(self) -> None:
        self._history_flow("IMAGE")

    def test_video_create_correct_and_reopen_full_track_history(self) -> None:
        self._history_flow("VIDEO")

    def test_changed_source_after_preview_rejects_write_without_success(self) -> None:
        def tamper(_dialog):
            path = self.root.joinpath(*self.imported["TEXT"].storage_key.split("/"))
            path.write_bytes(path.read_bytes().replace(b"Cafe", b"Bake"))

        with patch("creator_loop.library_ui.QMessageBox.warning") as warning:
            self.assertIsNone(self._create("TEXT", alter=tamper))
        warning.assert_called_once()
        self.assertIn("digest mismatch", self.window.status.text())
        self.assertTrue(all(b.isEnabled() for b in self.window._buttons))

    def test_full_video_replays_real_frames_and_audio_then_releases_source(
        self,
    ) -> None:
        view = WholeEvidenceView("Toàn video", self._source("VIDEO"))
        self.addCleanup(view.close)
        segment = view.preview.segment
        player = segment.player
        colors, samples = [], []
        player.videoSink().videoFrameChanged.connect(
            lambda frame: (
                colors.append(frame.toImage().pixelColor(0, 0))
                if frame.isValid()
                else None
            )
        )
        output = QAudioBufferOutput(player)
        player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(
            lambda buffer: (
                samples.append(buffer.frameCount()) if buffer.isValid() else None
            )
        )
        self.assertIs(player.audioOutput(), view.preview.audio)
        segment.play_button.click()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.app.processEvents()
            if player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                break
            time.sleep(0.01)
        self.assertGreater(player.position(), 700)
        self.assertTrue(any(c.red() > c.blue() for c in colors))
        self.assertTrue(any(c.blue() > c.red() for c in colors))
        self.assertGreater(sum(samples), 0)
        self.assertEqual(player.mediaStatus(), QMediaPlayer.MediaStatus.EndOfMedia)
        view.reject()
        self.assertTrue(player.source().isEmpty())
        self.assertEqual(
            player.playbackState(), QMediaPlayer.PlaybackState.StoppedState
        )

    def test_read_failure_after_commit_reports_saved_but_unavailable(self) -> None:
        with patch.object(
            self.window, "reload", side_effect=OSError("read unavailable")
        ):
            self.assertIsNotNone(self._create("IMAGE", expect_warning=True))
        self.warning.assert_called_once()
        self.assertIn("Đã ghi Evidence", self.window.status.text())
        self.assertIn("read unavailable", self.window.status.text())
        self.assertTrue(all(b.isEnabled() for b in self.window._buttons))
        self.window.reload()
        self.assertEqual(self.window.evidence_table.rowCount(), 1)

    def test_playback_error_blocks_confirmation_and_is_visible(self) -> None:
        dialog = WholeEvidenceDialog(self._source("VIDEO"))
        self.addCleanup(dialog.close)
        dialog.content.setPlainText("Quan sát")
        dialog.confirmed.setChecked(True)
        dialog.preview.segment.player.errorOccurred.emit(
            QMediaPlayer.Error.ResourceError, "Nguồn không còn sẵn"
        )
        self.assertIn("Nguồn không còn sẵn", dialog.preview.failed.text())
        self.assertFalse(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        )
